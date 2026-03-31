"""Control loop for EV charger power management."""

from __future__ import annotations

import asyncio
import logging
import re
import time as _time
from datetime import datetime, time

from app.config import ConfigManager
from app.modbus_ev import EVChargerModbusClient
from app.state import AppState, StateSnapshot

logger = logging.getLogger(__name__)

_HHMM_RE = re.compile(r"^(\d{1,2}):([0-5]\d)$")

# Charger hardware limits
_MIN_CHARGE_W = 4200.0
_MAX_CHARGE_W = 22000.0

# Eco outside-window thresholds (applied to rolling means)
_GRID_EXPORT_START_THRESHOLD_W = -1400.0  # mean grid_power_w <= this → start charging
_ECO_DAY_RAMP_STEP_W = 500.0  # ramp step per control loop iteration

_EV_SOC_STALE_S = 300.0  # 5 minutes — treat SOC as unavailable if not updated


# ---------------------------------------------------------------------------
# Task 6.1 — Time helpers
# ---------------------------------------------------------------------------


def validate_hhmm(s: str) -> bool:
    """Return True iff *s* is a valid time in H:MM or HH:MM format (hours 0-23, minutes 0-59)."""
    m = _HHMM_RE.match(s)
    if not m:
        return False
    hour, minute = int(m.group(1)), int(m.group(2))
    return 0 <= hour <= 23 and 0 <= minute <= 59


def normalise_hhmm(s: str) -> str:
    """Normalise a valid H:MM or HH:MM string to HH:MM (zero-padded)."""
    h, m = s.split(":")
    return f"{int(h):02d}:{m}"


def _parse_hhmm(s: str) -> time:
    """Parse an H:MM or HH:MM string into a :class:`datetime.time`."""
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
    """Periodic control loop that computes and writes EV charge setpoints.

    This is the master sync for all Modbus I/O. Each iteration:
    1. Ensure connections to Victron GX and EV charger
    2. Read registers from both devices
    3. Compute the charge power setpoint
    4. Ensure charger is enabled, write setpoint
    5. Publish state snapshot to MQTT queue
    """

    def __init__(
        self,
        state: AppState,
        victron_client,
        ev_client: EVChargerModbusClient,
        publish_queue: asyncio.Queue,
        config_manager: ConfigManager | None = None,
    ) -> None:
        self._state = state
        self._victron_client = victron_client
        self._ev_client = ev_client
        self._publish_queue = publish_queue
        self._config_manager = config_manager
        self._prev_ev_connected: bool | None = None  # None triggers initial state log
        self._eco_charging: bool = False
        self._eco_day_setpoint_w: float = _MIN_CHARGE_W

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
        if not self._state.ev_connected:
            return 0.0

        mode = self._state.charge_mode
        if mode == "Manual":
            return self._setpoint_manual()
        if mode == "Standby":
            return self._setpoint_standby()

        # Eco mode — requires Victron data to operate safely
        if not self._victron_client.connected:
            logger.warning("Eco mode: Victron comms down — pausing EV charging")
            self._eco_charging = False
            return 0.0
        if is_within_discharge_window(self._state):
            return self._setpoint_eco_night()
        return self._setpoint_eco_day()

    # --- Mode handlers (called by _compute_setpoint) ---

    def _setpoint_manual(self) -> float:
        """Manual: charge at a fixed user-configured power."""
        return clamp(self._state.manual_power_w, _MIN_CHARGE_W, _MAX_CHARGE_W)

    def _setpoint_standby(self) -> float:
        """Standby: no charging."""
        self._eco_charging = False
        self._eco_day_setpoint_w = _MIN_CHARGE_W
        return 0.0

    def _setpoint_eco_night(self) -> float:
        """Eco inside discharge window: draw from solar battery at a fixed rate.

        Stops when the battery hits the discharge floor and the EV has
        reached its minimum SOC target (or SOC is unknown).
        Reduces the setpoint if battery discharge exceeds the allowed limit.
        """
        state = self._state

        # Check if battery has reached the discharge floor
        at_floor = (
            state.solar_battery_soc_pct is not None
            and state.solar_battery_soc_pct <= state.solar_battery_discharge_floor_pct
        )
        if at_floor:
            ev_soc = self._get_ev_soc()
            ev_needs_charge = ev_soc is not None and ev_soc < state.ev_min_soc_pct
            if not ev_needs_charge:
                return 0.0

        setpoint = clamp(state.solar_battery_max_ev_charge_power_w, _MIN_CHARGE_W, _MAX_CHARGE_W)

        # Reduce setpoint if battery discharge exceeds the allowed max rate
        setpoint = self._limit_battery_discharge(setpoint, state.solar_battery_max_discharge_w)
        return setpoint

    def _setpoint_eco_day(self) -> float:
        """Eco outside discharge window: charge from excess solar.

        Uses rolling means to decide when to start/stop charging.
        Ramps the setpoint up from minimum, using battery power as feedback:
        - Battery not discharging (>= 0): ramp up by step
        - Battery discharging (< 0): reduce setpoint by discharge amount
        - Below minimum: pause
        """
        state = self._state
        mean_grid = self._mean_grid_power()
        mean_battery = self._mean_battery_power()

        # Start/stop decisions based on rolling means
        if not self._eco_charging:
            if mean_grid is not None and mean_grid <= _GRID_EXPORT_START_THRESHOLD_W:
                self._eco_charging = True
                self._eco_day_setpoint_w = _MIN_CHARGE_W
                logger.info(
                    "Eco day: starting charge at %.0f W (mean grid=%.0f W)",
                    self._eco_day_setpoint_w, mean_grid,
                )
            else:
                return 0.0

        if mean_battery is not None and mean_battery < state.solar_battery_day_power_limit_w:
            self._eco_charging = False
            logger.info(
                "Eco day: stopping charge (mean battery=%.0f W, limit=%.0f W)",
                mean_battery, state.solar_battery_day_power_limit_w,
            )
            return 0.0

        # Ramp: probe available solar capacity using battery feedback
        battery_power = state.solar_battery_power_w
        if battery_power is not None and battery_power < 0:
            self._eco_day_setpoint_w += battery_power  # negative, reduces
        else:
            self._eco_day_setpoint_w += _ECO_DAY_RAMP_STEP_W

        self._eco_day_setpoint_w = clamp(self._eco_day_setpoint_w, _MIN_CHARGE_W, _MAX_CHARGE_W)

        # If at minimum and battery still discharging, pause
        if self._eco_day_setpoint_w <= _MIN_CHARGE_W and battery_power is not None and battery_power < 0:
            return 0.0

        return self._eco_day_setpoint_w

    # --- Shared helpers ---

    def _limit_battery_discharge(self, setpoint: float, max_discharge_w: float) -> float:
        """Reduce setpoint if battery discharge exceeds the allowed limit.

        Returns 0.0 if the adjusted setpoint falls below _MIN_CHARGE_W.
        """
        battery_power = self._state.solar_battery_power_w
        if battery_power is not None and battery_power < 0:
            overshoot = abs(battery_power) - max_discharge_w
            if overshoot > 0:
                setpoint -= overshoot
                if setpoint < _MIN_CHARGE_W:
                    return 0.0
                setpoint = clamp(setpoint, _MIN_CHARGE_W, _MAX_CHARGE_W)
        return setpoint

    # ------------------------------------------------------------------
    # Task 6.9 — Run loop
    # ------------------------------------------------------------------

    async def run_loop(self) -> None:
        """Master control loop: read → compute → write → publish."""
        while True:
            # 1. Ensure Modbus connections
            await self._victron_client.ensure_connected()
            await self._ev_client.ensure_connected()

            # 2. Read registers from both devices
            await self._victron_client.read()
            await self._ev_client.read()

            # 3. Record rolling samples for mean calculations
            self._record_samples()

            # Detect EV connect/disconnect edges (None on first iteration)
            if self._state.ev_connected and self._prev_ev_connected is not True:
                logger.info("EV vehicle connected")
            elif not self._state.ev_connected and self._prev_ev_connected is not False:
                logger.info("EV vehicle disconnected")
                if self._state.charge_mode == "Manual":
                    logger.info("Resetting charge mode from Manual to Eco")
                    self._state.charge_mode = "Eco"
                    if self._config_manager is not None:
                        self._config_manager.schedule_persist(self._state)
                    self._publish_queue.put_nowait("republish_config")
            self._prev_ev_connected = self._state.ev_connected

            # 4. Compute setpoint, ensure charger enabled, write
            setpoint = self._compute_setpoint()
            if self._state.ev_connected:
                await self._ev_client.ensure_enabled()
            await self._ev_client.write_setpoint(setpoint)
            self._state.commanded_setpoint_w = setpoint

            # 5. Publish state snapshot
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
