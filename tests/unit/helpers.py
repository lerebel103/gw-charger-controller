"""Shared test helpers for unit tests."""

import asyncio
import time as _time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.control.constants import _MIN_CHARGE_W
from app.control.loop import ControlLoop
from app.state import AppState, ChargeModeState, ChargeSessionState


class FakeStateMachine:
    """Minimal state machine stub that records the last mode state set."""

    def __init__(self) -> None:
        self.mode_state: ChargeModeState | None = None

    def set_mode_state(self, state: ChargeModeState) -> None:
        self.mode_state = state


def make_ns_loop(state: AppState, *, victron_connected: bool = True, **overrides) -> SimpleNamespace:
    """Create a SimpleNamespace loop covering all protocol fields.

    Covers SamplingLoopProtocol, ModeLoopProtocol, SessionLoopProtocol, and
    SnapshotLoopProtocol, so it can be used in tests for power_utils,
    mode_strategies, state_machine, and snapshot helpers.
    """
    loop = SimpleNamespace(
        _state=state,
        _publish_queue=asyncio.Queue(),
        _victron_client=SimpleNamespace(connected=victron_connected),
        _state_machine=FakeStateMachine(),
        _eco_charging=False,
        _eco_day_setpoint_w=_MIN_CHARGE_W,
        _eco_day_battery_full=False,
        _eco_day_stopped_at=None,
        _eco_night_stopped_at=None,
        _charging_session_state=ChargeSessionState.IDLE,
        _charge_mode_state=ChargeModeState.IDLE,
        _stopping_at=None,
        _stopping_reason=None,
        _stopped_at=None,
        _last_positive_setpoint=3680.0,
        _session_origin_mode=None,
        _external_stop_ticks=0,
        _standby_write_quiet=False,
        _grid_power_samples=[],
        _battery_power_samples=[],
        _start_time=_time.monotonic(),
    )
    for key, value in overrides.items():
        setattr(loop, key, value)
    return loop


def make_control_loop(state: AppState, *, victron_connected: bool = True) -> ControlLoop:
    """Create a ControlLoop with mocked Victron and EV clients."""
    victron = MagicMock()
    victron.connected = victron_connected
    ev = AsyncMock()
    return ControlLoop(state, victron, ev, asyncio.Queue())


def fill_grid_samples(cl: ControlLoop, value: float, count: int = 60) -> None:
    """Fill the grid power rolling buffer with a constant value."""
    now = _time.monotonic()
    cl._grid_power_samples = [(now - i, value) for i in range(count)]


def fill_battery_samples(cl: ControlLoop, value: float, count: int = 60) -> None:
    """Fill the battery power rolling buffer with a constant value."""
    now = _time.monotonic()
    cl._battery_power_samples = [(now - i, value) for i in range(count)]
