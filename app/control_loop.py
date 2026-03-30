"""Control loop for EV charger power management."""

from __future__ import annotations

import asyncio
import collections
import logging
import re
import time as _time
from datetime import datetime, time

from app.config import ConfigManager
from app.modbus_ev import EVChargerModbusClient
from app.state import AppState, StateSnapshot

logger = logging.getLogger(__name__)

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# Charger hardware limits
_MIN_CHARGE_W = 4200.0
_MAX_CHARGE_W = 22000.0

# Eco outside-window thresholds (applied to rolling means)
_GRID_EXPORT_START_THRESHOLD_W = -1400.0  # mean grid_power_w <= this → start charging
_BATTERY_STOP_THRESHOLD_W = 500.0  # mean solar_battery_power_w > this → stop charging

_ECO_PAUSE_HYSTERESIS_S = 60.0  # 1 minute
_EV_SOC_STALE_S = 300.0  # 5 minutes — treat SOC as unavailable if not updated


# ---------------------------------------------------------------------------
# Task 6.1 — Time helpers
# ---------------------------------------------------------------------------


def validate_hhmm(s: str) -> bool:
    """Return True iff *s* matches HH:MM (hours 00-23, minutes 00-59)."""
    return bool(_HHMM_RE.match(s))


def _parse_hhmm(s: str) -> time:
    """Parse an HH:MM string into a :class:`datetime.time`."""
    h, m = s.split(":")
    return time(int(h), int(m))


def is_within_discharge_window(state: AppState) -> bool:
    """Return True if the current local time is within [start, end).

    Handles midnight-spanning windows (start > end):
      returns True if current_time >= start OR current_time < end.
    """
    if not (validate_hhmm(state.solar_battery_discharge_start) and validate_hhmm(state.solar_battery_discharge_end)):
        return False

    start = _parse_hhmm(state.solar_battery_discharge_start)
    end = _parse_hhmm(state.solar_battery_discharge_end)
    now = datetime.now().time()  # noqa: DTZ005 — local time is intentional

    if start <= end:
        # Non-spanning window, e.g. 06:00–18:00
        result = start <= now < end
    else:
        # Midnight-spanning window, e.g. 23:00–06:00
        result = now >= start or now < end

    logger.debug(
        "Discharge window check: now=%s start=%s end=%s → %s",
        now.strftime("%H:%M:%S"),
        state.solar_battery_discharge_start,
        state.solar_battery_discharge_end,
        result,
    )
    return result


# ---------------------------------------------------------------------------
# Task 6.4 — Helper functions
# ---------------------------------------------------------------------------


def clamp(value: float, min_val: float, max_val: float) -> float:
    """Clamp *value* to the range [min_val, max_val]."""
    return max(min_val, min(value, max_val))


# ---------------------------------------------------------------------------
# Task 6.6 & 6.9 — ControlLoop class
# ---------------------------------------------------------------------------


class ControlLoop:
    """Periodic control loop that computes and writes EV charge setpoints."""

    def __init__(
        self,
        state: AppState,
        ev_client: EVChargerModbusClient,
        publish_queue: asyncio.Queue,
        config_manager: ConfigManager | None = None,
    ) -> None:
        self._state = state
        self._ev_client = ev_client
        self._publish_queue = publish_queue
        self._config_manager = config_manager
        self._eco_paused_at: datetime | None = None
        self._prev_ev_connected: bool = state.ev_connected
        self._eco_charging: bool = False  # True when eco outside-window is actively charging

        # Rolling sample buffers: list of (monotonic_time, value) tuples
        self._grid_power_samples: list[tuple[float, float]] = []
        self._battery_power_samples: list[tuple[float, float]] = []

    # ------------------------------------------------------------------
    # Task 6.6 — Setpoint computation
    # ------------------------------------------------------------------

    def _get_ev_soc(self) -> float | None:
        """Return ev_soc_pct if fresh (updated within _EV_SOC_STALE_S), else None."""
        state = self._state
        if state.ev_soc_pct is None or state.ev_soc_pct_updated_at is None:
            return None
        if (_time.monotonic() - state.ev_soc_pct_updated_at) > _EV_SOC_STALE_S:
            return None
        return state.ev_soc_pct

    def _record_samples(self) -> None:
        """Record current grid and battery power readings into rolling buffers."""
        now = _time.monotonic()
        if self._state.grid_power_w is not None:
            self._grid_power_samples.append((now, self._state.grid_power_w))
        if self._state.solar_battery_power_w is not None:
            self._battery_power_samples.append((now, self._state.solar_battery_power_w))
        self._prune_samples()

    def _prune_samples(self) -> None:
        """Remove samples older than the configured window."""
        cutoff = _time.monotonic() - (self._state.eco_mean_window_minutes * 60)
        self._grid_power_samples = [
            (t, v) for t, v in self._grid_power_samples if t >= cutoff
        ]
        self._battery_power_samples = [
            (t, v) for t, v in self._battery_power_samples if t >= cutoff
        ]

    def _mean_grid_power(self) -> float | None:
        """Return the mean grid power over the rolling window, or None if no samples."""
        if not self._grid_power_samples:
            return None
        return sum(v for _, v in self._grid_power_samples) / len(self._grid_power_samples)

    def _mean_battery_power(self) -> float | None:
        """Return the mean solar battery power over the rolling window, or None if no samples."""
        if not self._battery_power_samples:
            return None
        return sum(v for _, v in self._battery_power_samples) / len(self._battery_power_samples)

    def _compute_setpoint(self) -> float | None:
        """Compute the charge power setpoint (watts), or None if no vehicle."""
        state = self._state

        if not state.ev_connected:
            self._eco_paused_at = None
            return None

        # --- Manual mode ---
        if state.charge_mode == "Manual":
            self._eco_paused_at = None
            return clamp(state.manual_power_w, _MIN_CHARGE_W, _MAX_CHARGE_W)

        # --- Standby mode ---
        if state.charge_mode == "Standby":
            self._eco_paused_at = None
            self._eco_charging = False
            return 0.0

        # --- Eco mode ---
        if is_within_discharge_window(state):
            self._eco_paused_at = None
            # Inside battery discharge window
            at_floor = (
                state.solar_battery_soc_pct is not None
                and state.solar_battery_soc_pct <= state.solar_battery_discharge_floor_pct
            )

            if at_floor:
                ev_soc = self._get_ev_soc()
                if ev_soc is not None and ev_soc < state.ev_min_soc_pct:
                    # EV hasn't reached target — keep charging (grid import will occur)
                    pass
                else:
                    # EV SOC unavailable → stop (conservative)
                    # EV SOC >= min → stop (target reached)
                    return 0.0

            setpoint = clamp(state.solar_battery_max_ev_charge_power_w, _MIN_CHARGE_W, _MAX_CHARGE_W)

            # Guard: reduce EV setpoint if battery discharge exceeds the allowed limit.
            # solar_battery_power_w is negative when discharging.
            if state.solar_battery_power_w is not None and state.solar_battery_power_w < 0:
                discharge_w = abs(state.solar_battery_power_w)
                overshoot = discharge_w - state.solar_battery_max_discharge_w
                if overshoot > 0:
                    setpoint = setpoint - overshoot
                    if setpoint < _MIN_CHARGE_W:
                        return 0.0
                    setpoint = clamp(setpoint, _MIN_CHARGE_W, _MAX_CHARGE_W)

            return setpoint

        # Outside battery discharge window: charge from grid export only
        # Use rolling means for start/stop decisions
        mean_grid = self._mean_grid_power()
        mean_battery = self._mean_battery_power()

        grid_power = state.grid_power_w if state.grid_power_w is not None else 0.0
        grid_export_w = abs(grid_power) if grid_power < 0 else 0.0

        # Start/stop logic based on rolling means
        if not self._eco_charging:
            # Not currently charging — check if we should start
            if mean_grid is not None and mean_grid <= _GRID_EXPORT_START_THRESHOLD_W:
                self._eco_charging = True
                logger.info(
                    "Eco outside-window: starting charge (mean grid=%.0f W, threshold=%.0f W)",
                    mean_grid,
                    _GRID_EXPORT_START_THRESHOLD_W,
                )
            else:
                return 0.0  # not enough sustained export

        if self._eco_charging:
            # Currently charging — check if we should stop
            if mean_battery is not None and mean_battery > _BATTERY_STOP_THRESHOLD_W:
                self._eco_charging = False
                logger.info(
                    "Eco outside-window: stopping charge (mean battery=%.0f W, threshold=%.0f W)",
                    mean_battery,
                    _BATTERY_STOP_THRESHOLD_W,
                )
                return 0.0

        # Ramp logic: setpoint tracks instantaneous grid export, clamped to hardware limits
        setpoint = clamp(grid_export_w, _MIN_CHARGE_W, _MAX_CHARGE_W)

        # Minimise battery discharge: reduce setpoint if battery is discharging
        if state.solar_battery_power_w is not None and state.solar_battery_power_w < 0:
            setpoint = setpoint + state.solar_battery_power_w  # negative
            if setpoint < _MIN_CHARGE_W:
                return 0.0

        self._eco_paused_at = None
        return setpoint

    # ------------------------------------------------------------------
    # Task 6.9 — Run loop
    # ------------------------------------------------------------------

    async def run_loop(self) -> None:
        """Periodic loop: compute setpoint, write to charger, publish state."""
        while True:
            # Record rolling samples for mean calculations
            self._record_samples()

            # Detect EV disconnect: connected → disconnected resets Manual to Eco
            if self._prev_ev_connected and not self._state.ev_connected and self._state.charge_mode == "Manual":
                logger.info("EV disconnected while in Manual mode — resetting to Eco")
                self._state.charge_mode = "Eco"
                if self._config_manager is not None:
                    self._config_manager.schedule_persist(self._state)
                self._publish_queue.put_nowait("republish_config")
            self._prev_ev_connected = self._state.ev_connected

            setpoint = self._compute_setpoint()

            if setpoint is not None:
                await self._ev_client.write_setpoint(setpoint)

            self._state.commanded_setpoint_w = setpoint

            snapshot = StateSnapshot(
                ev_connected=self._state.ev_connected,
                ev_charger_status=self._state.ev_charger_status,
                ev_active_power_w=self._state.ev_active_power_w,
                ev_session_energy_wh=self._state.ev_session_energy_wh,
                ev_voltage_l1_v=self._state.ev_voltage_l1_v,
                ev_voltage_l2_v=self._state.ev_voltage_l2_v,
                ev_voltage_l3_v=self._state.ev_voltage_l3_v,
                ev_current_a=self._state.ev_current_a,
                ev_current_b=self._state.ev_current_b,
                ev_current_c=self._state.ev_current_c,
                ev_completion_time_h=self._state.ev_completion_time_h,
                ev_total_energy_wh=self._state.ev_total_energy_wh,
                ev_soc_pct=self._get_ev_soc(),
                l1_voltage_drop_pct=self._state.l1_voltage_drop_pct,
                l2_voltage_drop_pct=self._state.l2_voltage_drop_pct,
                l3_voltage_drop_pct=self._state.l3_voltage_drop_pct,
                commanded_setpoint_w=self._state.commanded_setpoint_w,
                timestamp=datetime.now(),  # noqa: DTZ005
            )
            await self._publish_queue.put(snapshot)

            await asyncio.sleep(self._state.control_loop_interval_s)
