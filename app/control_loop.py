"""Control loop for EV charger power management."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, time

from app.config import ConfigManager
from app.modbus_ev import EVChargerModbusClient
from app.state import AppState, StateSnapshot

logger = logging.getLogger(__name__)

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# Charger hardware limits
_MIN_CHARGE_W = 1380.0
_MAX_CHARGE_W = 11000.0
_GRID_EXPORT_START_THRESHOLD_W = 1400.0
_ECO_PAUSE_HYSTERESIS_S = 300.0  # 5 minutes


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
        return start <= now < end
    # Midnight-spanning window, e.g. 23:00–06:00
    return now >= start or now < end


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
        interval_s: float = 10.0,
        config_manager: ConfigManager | None = None,
    ) -> None:
        self._state = state
        self._ev_client = ev_client
        self._publish_queue = publish_queue
        self._interval_s = interval_s
        self._config_manager = config_manager
        self._eco_paused_at: datetime | None = None  # hysteresis: when pause condition first detected
        self._prev_ev_connected: bool = state.ev_connected

    # ------------------------------------------------------------------
    # Task 6.6 — Setpoint computation
    # ------------------------------------------------------------------

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

        # --- Eco mode ---
        if is_within_discharge_window(state):
            self._eco_paused_at = None
            # Inside battery discharge window — three-part logic
            at_floor = (
                state.solar_battery_soc_pct is not None
                and state.solar_battery_soc_pct <= state.solar_battery_discharge_floor_pct
            )

            if at_floor:
                ev_soc = getattr(state, "ev_soc_pct", None)  # may be None if unavailable
                if ev_soc is not None and ev_soc >= state.ev_min_soc_pct:
                    return 0.0  # EV has reached minimum SOC — stop charging
                # else: EV SOC unknown or below minimum — continue charging

            return clamp(state.solar_battery_max_charge_power_w, _MIN_CHARGE_W, _MAX_CHARGE_W)

        # Outside battery discharge window: charge from grid export only
        grid_power = state.grid_power_w if state.grid_power_w is not None else 0.0
        grid_export_w = abs(grid_power) if grid_power < 0 else 0.0

        # Determine if conditions say we should pause
        should_pause = False

        if grid_export_w < _GRID_EXPORT_START_THRESHOLD_W:
            should_pause = True

        setpoint = clamp(grid_export_w, _MIN_CHARGE_W, _MAX_CHARGE_W) if not should_pause else 0.0

        if not should_pause and state.solar_battery_power_w is not None and state.solar_battery_power_w < 0:
            # Minimise battery discharge: reduce setpoint if battery is discharging
            setpoint = setpoint + state.solar_battery_power_w  # negative
            if setpoint < _MIN_CHARGE_W:
                should_pause = True

        if should_pause:
            # Conditions want us to stop — but keep charging for up to 5 minutes
            # (hysteresis) to ride through transient dips / cloud cover.
            now = datetime.now()  # noqa: DTZ005
            if self._eco_paused_at is None:
                self._eco_paused_at = now
                logger.info("Eco outside-window: pause condition detected, starting hysteresis hold")

            elapsed = (now - self._eco_paused_at).total_seconds()
            if elapsed < _ECO_PAUSE_HYSTERESIS_S:
                # Still within grace period — keep charging at minimum
                logger.info(
                    "Eco outside-window: holding charge during hysteresis (%.0f s of %.0f s)",
                    elapsed,
                    _ECO_PAUSE_HYSTERESIS_S,
                )
                return _MIN_CHARGE_W

            # Grace period expired — actually stop
            logger.info("Eco outside-window: hysteresis expired, stopping charge")
            return 0.0

        # Conditions are good — clear any pending hysteresis and charge
        self._eco_paused_at = None
        return setpoint

    # ------------------------------------------------------------------
    # Task 6.9 — Run loop
    # ------------------------------------------------------------------

    async def run_loop(self) -> None:
        """Periodic loop: compute setpoint, write to charger, publish state."""
        while True:
            # logger.info(self._state)

            # Detect EV disconnect: connected → disconnected resets Manual to Eco
            if self._prev_ev_connected and not self._state.ev_connected and self._state.charge_mode == "Manual":
                logger.info("EV disconnected while in Manual mode — resetting to Eco")
                self._state.charge_mode = "Eco"
                if self._config_manager is not None:
                    self._config_manager.schedule_persist(self._state)
            self._prev_ev_connected = self._state.ev_connected

            setpoint = self._compute_setpoint()

            if setpoint is not None and setpoint > 0:
                await self._ev_client.write_enable(True)
                await self._ev_client.write_setpoint(setpoint)
            else:
                await self._ev_client.write_enable(False)

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
                l1_voltage_drop_pct=self._state.l1_voltage_drop_pct,
                l2_voltage_drop_pct=self._state.l2_voltage_drop_pct,
                l3_voltage_drop_pct=self._state.l3_voltage_drop_pct,
                commanded_setpoint_w=self._state.commanded_setpoint_w,
                timestamp=datetime.now(),  # noqa: DTZ005
            )
            await self._publish_queue.put(snapshot)

            await asyncio.sleep(self._interval_s)
