"""Control loop orchestration for EV charger power management."""

import asyncio
import logging
import time as _time

from app.config import ConfigManager
from app.control.breaker import confirm_applied, limit_phase_current, phase_headroom_pct, record_phase_samples
from app.control.breaker_state import BreakerLimiterState, PhaseSample
from app.control.constants import (
    _EV_MAX_SOC_DEFAULT,
    _MIN_CHARGE_W,
    _STOP_PRESET_W,
)
from app.control.mode_strategies import clamp_eco_day_to_applied, compute_setpoint
from app.control.power_utils import ev_soc_is_fresh, record_samples, resolve_ev_soc
from app.control.protocols import VictronClientProtocol
from app.control.snapshot import build_snapshot
from app.control.soc_estimator import AnchorOrigin, KLearnResult, SocEstimator, SocSource, effective_k
from app.control.state_machine import (
    ChargingStateMachine,
)
from app.log_throttle import LogThrottle
from app.modbus import EVChargerModbusClient
from app.state import AppState, ChargeModeState, ChargeSessionState

logger = logging.getLogger(__name__)
_throttle = LogThrottle(logger, suppress_seconds=60.0)


class ControlLoop:
    """Periodic control loop that computes and writes EV charge setpoints."""

    def __init__(
        self,
        state: AppState,
        victron_client: VictronClientProtocol,
        ev_client: EVChargerModbusClient,
        publish_queue: asyncio.Queue,
        config_manager: ConfigManager | None = None,
    ) -> None:
        self._state = state
        self._victron_client = victron_client
        self._ev_client = ev_client
        self._publish_queue = publish_queue
        self._config_manager = config_manager
        self._prev_ev_connected: bool | None = None
        self._eco_charging: bool = False
        self._eco_day_setpoint_w: float = _MIN_CHARGE_W
        self._eco_day_battery_full: bool = False
        self._charging_session_state: ChargeSessionState = ChargeSessionState.IDLE
        self._charge_mode_state: ChargeModeState = ChargeModeState.IDLE
        self._stopping_at: float | None = None
        self._stopping_reason: str | None = None
        self._stopped_at: float | None = None
        self._last_positive_setpoint: float = _MIN_CHARGE_W
        self._session_origin_mode: str | None = None
        self._external_stop_ticks: int = 0
        self._start_time: float = _time.monotonic()
        self._eco_day_stopped_at: float | None = None
        self._eco_night_stopped_at: float | None = None
        self._standby_write_quiet: bool = False
        self._grid_power_samples: list[tuple[float, float]] = []
        self._battery_power_samples: list[tuple[float, float]] = []
        self._phase_samples: list[list[PhaseSample]] = [[], [], []]
        self._prev_ev_fresh: bool = True
        self._breaker = BreakerLimiterState()
        self._soc_estimator = SocEstimator()
        self._soc_was_stalled: bool = False
        self._soc_discontinuities_seen: int = 0
        self._state_machine = ChargingStateMachine(self)

    async def _apply_ev_output(self, setpoint: float, suppress_ev_writes: bool) -> None:
        if suppress_ev_writes:
            return

        if setpoint > 0:
            # Only a confirmed write becomes the breaker hold baseline.
            if await self._ev_client.write_setpoint(setpoint) is True:
                confirm_applied(self, setpoint)
            if self._state_machine.should_send_start_command(setpoint):
                await self._ev_client.start_charging()
            return

        await self._ev_client.write_setpoint(_STOP_PRESET_W)
        if self._state_machine.should_send_stop_command(setpoint):
            await self._ev_client.stop_charging()

    def _apply_k_result(self, k_prev: float, result: KLearnResult) -> None:
        """Store a refined k with its capacity and schedule persistence."""
        state = self._state
        if abs(result.k_new - k_prev) < 1e-4 and state.ev_soc_k_capacity_kwh == state.ev_battery_capacity_kwh:
            return
        state.ev_soc_k = result.k_new
        state.ev_soc_k_capacity_kwh = state.ev_battery_capacity_kwh
        logger.debug("EV SOC k refined: span %.4f, k %.4f -> %.4f", result.k_span, k_prev, result.k_new)
        if self._config_manager is not None:
            self._config_manager.schedule_persist(state)

    def _update_soc_estimator(self) -> None:
        """Feed the SOC estimator and log stall, estimate-used and missing-SOC transitions."""
        state = self._state
        estimator = self._soc_estimator
        k_prev = effective_k(state)
        k_result = estimator.observe(
            soc_pct=state.ev_soc_pct,
            soc_fresh=ev_soc_is_fresh(self),
            energy_wh=state.ev_session_energy_wh
            if state.ev_comm_healthy and state.charge_mode != "Standby" and not self._standby_write_quiet
            else None,
            capacity_kwh=state.ev_battery_capacity_kwh,
            k=k_prev,
            now_s=_time.monotonic(),
        )
        if k_result is not None:
            self._apply_k_result(k_prev, k_result)
        if estimator.discontinuities != self._soc_discontinuities_seen:
            self._soc_discontinuities_seen = estimator.discontinuities
            _throttle.warning(
                "soc_energy_discontinuity", "EV session energy jumped; ignoring the step for SOC estimate"
            )

        resolution = resolve_ev_soc(self)
        if resolution.stalled and not self._soc_was_stalled:
            _throttle.warning(
                "soc_stalled",
                "EV SOC report looks stalled (reported %.1f%%, estimated %.1f%%); using the estimate",
                resolution.reported_pct,
                resolution.estimated_pct,
            )
        elif not resolution.stalled and self._soc_was_stalled:
            _throttle.clear("soc_stalled")
            logger.info("EV SOC report updated again; stall cleared")
        self._soc_was_stalled = resolution.stalled

        if resolution.source is SocSource.ESTIMATED:
            _throttle.warning(
                "soc_estimate_used", "EV SOC estimated from session energy: %.1f%%", resolution.effective_pct
            )
            anchor = estimator.anchor
            if anchor is not None and anchor.origin is AnchorOrigin.LAST_KNOWN:
                _throttle.warning(
                    "soc_last_known_anchor",
                    "EV SOC estimate anchored on last known SOC %.1f%% (no SOC reported this plug-in)",
                    anchor.soc_pct,
                )
        else:
            _throttle.clear("soc_estimate_used")

        if resolution.source is SocSource.NONE and (state.ev_active_power_w or 0) > 0:
            _throttle.warning(
                "soc_unavailable_charging", "EV is charging but no SOC is available (reported or estimated)"
            )
        else:
            _throttle.clear("soc_unavailable_charging")

    async def run_loop(self) -> None:
        """Master control loop: read -> compute -> write -> publish."""
        while True:
            # Detect standby exit: if suppression is active but mode is no longer
            # Standby, force a clean EV reconnect before reads resume.
            if self._standby_write_quiet and self._state.charge_mode != "Standby":
                logger.info("Exiting standby suppression, forcing EV charger reconnect")
                self._standby_write_quiet = False
                await self._ev_client.reconnect()

            await self._victron_client.ensure_connected()
            if not self._standby_write_quiet:
                await self._ev_client.ensure_connected()

            await self._victron_client.read()
            if not self._standby_write_quiet:
                await self._ev_client.read()

            record_samples(self)
            # EV-dependent history is only valid while EV readings are fresh. Drop it whenever EV reads
            # stop or resume (Standby) so retained EV currents never pair with new grid readings.
            ev_fresh = not self._standby_write_quiet and self._state.charge_mode != "Standby"
            if ev_fresh != self._prev_ev_fresh:
                self._phase_samples = [[], [], []]
                self._prev_ev_fresh = ev_fresh
            record_phase_samples(self, ev_fresh=ev_fresh)

            if self._state.ev_connected and self._prev_ev_connected is not True:
                logger.info("EV vehicle connected")
                self._soc_estimator.begin_session()
            elif not self._state.ev_connected and self._prev_ev_connected is not False:
                logger.info("EV vehicle disconnected")
                self._soc_estimator.end_session()
                self._eco_charging = False
                self._eco_day_battery_full = False
                if self._state.ev_max_soc_pct != _EV_MAX_SOC_DEFAULT:
                    self._state.ev_max_soc_pct = _EV_MAX_SOC_DEFAULT
                    logger.info("Reset max EV SOC to %.0f%%", _EV_MAX_SOC_DEFAULT)
                if self._state.charge_mode == "Manual":
                    logger.info("Resetting charge mode from Manual to Eco")
                    self._state.charge_mode = "Eco"
                if self._config_manager is not None:
                    self._config_manager.schedule_persist(self._state)
                self._publish_queue.put_nowait("republish_config")
            self._prev_ev_connected = self._state.ev_connected

            self._update_soc_estimator()

            setpoint = compute_setpoint(self)
            setpoint = self._state_machine.apply_charging_events(setpoint)
            requested = setpoint
            setpoint = limit_phase_current(self, setpoint)
            clamp_eco_day_to_applied(self, requested, setpoint)
            suppress_ev_writes = self._state_machine.should_suppress_ev_writes(setpoint)
            await self._apply_ev_output(setpoint, suppress_ev_writes)
            self._state.commanded_setpoint_w = setpoint

            # Breaker headroom diagnostics (from 30 s mean import current vs 80 % limit)
            self._state.l1_breaker_headroom_pct = phase_headroom_pct(self, 1)
            self._state.l2_breaker_headroom_pct = phase_headroom_pct(self, 2)
            self._state.l3_breaker_headroom_pct = phase_headroom_pct(self, 3)

            await self._publish_queue.put(build_snapshot(self))
            await asyncio.sleep(self._state.control_loop_interval_s)


__all__ = [
    "ControlLoop",
    "ChargeModeState",
    "ChargeSessionState",
]
