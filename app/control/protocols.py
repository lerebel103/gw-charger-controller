"""Shared protocols for the control package.

Defining these interfaces here breaks the circular import that would otherwise
occur between loop.py and the helper modules (power_utils, mode_strategies,
state_machine, snapshot) that all receive a ControlLoop instance as a parameter.

Dependency chain (no cycles):
    app/state  ←  protocols  ←  power_utils, state_machine, mode_strategies, snapshot
                                                       ↑
                                                    loop.py
"""

import asyncio
from collections.abc import Awaitable
from typing import Protocol

from app.state import AppState, ChargeModeState, ChargeSessionState


class VictronClientProtocol(Protocol):
    """Victron client contract needed by control logic."""

    connected: bool

    def ensure_connected(self) -> Awaitable[None]: ...

    def read(self) -> Awaitable[None]: ...


class StateMachineProtocol(Protocol):
    """Interface for ChargingStateMachine as seen by mode strategies."""

    def set_mode_state(self, state: ChargeModeState) -> None: ...


class SamplingLoopProtocol(Protocol):
    """Loop fields needed for sample collection and rolling means."""

    _state: AppState
    _grid_power_samples: list[tuple[float, float]]
    _battery_power_samples: list[tuple[float, float]]


class ModeLoopProtocol(SamplingLoopProtocol, Protocol):
    """Loop fields needed by mode setpoint strategies."""

    _state_machine: StateMachineProtocol
    _eco_charging: bool
    _eco_day_setpoint_w: float
    _eco_day_battery_full: bool
    _eco_day_stopped_at: float | None
    _eco_night_stopped_at: float | None
    _victron_client: VictronClientProtocol


class SnapshotLoopProtocol(Protocol):
    """Loop fields needed when building output snapshots."""

    _state: AppState
    _start_time: float


class SessionLoopProtocol(Protocol):
    """Loop fields needed by session state machine logic."""

    _state: AppState
    _publish_queue: asyncio.Queue
    _victron_client: VictronClientProtocol
    _stopping_at: float | None
    _stopping_reason: str | None
    _stopped_at: float | None
    _last_positive_setpoint: float
    _session_origin_mode: str | None
    _external_stop_ticks: int
    _charging_session_state: ChargeSessionState
    _charge_mode_state: ChargeModeState
    _standby_write_quiet: bool


class LoopProtocol(SessionLoopProtocol, ModeLoopProtocol, SnapshotLoopProtocol, Protocol):
    """Composite protocol matching the full ControlLoop surface used by control helpers."""
