"""Data types for the main-breaker limiter (kept free of control imports to avoid cycles)."""

from dataclasses import dataclass, field
from typing import NamedTuple


class PhaseSample(NamedTuple):
    """One control-cycle observation for a single grid phase."""

    t: float  # monotonic seconds
    import_w: float  # max(0, signed grid power): per-sample clamp, export never offsets import
    import_a: float  # unsigned meter current if the phase is importing, else 0.0
    ev_w: float  # V_ev * I_ev_raw measured on this phase


@dataclass
class BreakerLimiterState:
    """Mutable limiter state held on the control loop."""

    cap_w: float | None = None  # active TOTAL-setpoint cap; None = no cap
    stopped: bool = False  # stop latch: charger held at 0 until the grid has recovered
    missing_cycles: int = 0
    last_applied_w: float | None = None  # last output applied on a valid cycle; held during the data debounce
    hold_ceiling_w: float | None = None  # tightest output since data became invalid; the hold never exceeds it
    min_overload_cycles: int = 0
    last_binding_phase: int | None = None
    restart_at_minimum: bool = False  # stop latch released: next charge start is held at the minimum
    phase_fresh: list[bool] = field(default_factory=lambda: [False, False, False])
