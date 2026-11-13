"""Closed-loop main-breaker protection based on SIGNED per-phase grid current.

Design summary (see docs/grid-power-scaling.md):

* The Victron grid-meter current registers are SIGNED per phase (+ import, - export). The breaker
  trips on current, not power (low power factor draws more current than real power suggests), so the
  limiter works in amps: ``I_imp = max(0, I_grid)``. Export never counts as load or headroom.
* The charger setpoint is a TOTAL across three phases split equally, and only a hint. The real EV load
  per phase is the measured raw EV current. A cap is ``3 * V * (I_ev - excess_A)``: the grid voltage only
  converts amps to the watts of the setpoint.
* Only EV-active phases can be relieved by throttling the EV. Overload on any other phase
  is logged but never capped.
* Missing data means a value that is absent (failed read, None, no fresh sample this cycle). It holds at
  the last applied output for the debounce window, then fails safe to the minimum charge power in every mode.
"""

import logging
import time as _time

from app.control.breaker_state import BreakerLimiterState, PhaseSample
from app.control.constants import (
    _BREAKER_CAP_RELEASE_STEP_W,
    _BREAKER_FALLBACK_VOLTAGE_V,
    _BREAKER_INSTANT_THRESHOLD_FRACTION,
    _BREAKER_MISSING_DATA_CYCLES,
    _BREAKER_RELEASE_MARGIN_A,
    _BREAKER_SAFETY_FRACTION,
    _BREAKER_STOP_CYCLES,
    _MAX_CHARGE_W,
    _MIN_CHARGE_W,
    _PHASE_ACTIVE_THRESHOLD_A,
    _PHASE_COUNT,
    _PHASE_IMPORT_MEAN_WINDOW_S,
)
from app.control.protocols import SamplingLoopProtocol
from app.log_throttle import LogThrottle
from app.modbus.ev import EV_MAX_PHASE_CURRENT_A
from app.state import AppState

logger = logging.getLogger(__name__)
_throttle = LogThrottle(logger, suppress_seconds=60.0)

_PHASES = range(1, _PHASE_COUNT + 1)
_EV_SUFFIX = ("a", "b", "c")
_THROTTLE_KEYS = (
    "breaker_cap_trim",
    "breaker_fast_trip",
    "breaker_failsafe",
    "breaker_stop",
    *(f"breaker_overload_non_ev_{n}" for n in _PHASES),
)


def _voltage_ok(v: float | None) -> bool:
    return v is not None and 100.0 < v < 300.0


def _grid_current(state: AppState, phase: int) -> float | None:
    """Signed grid current for the phase (+ import, - export), or None when missing."""
    return getattr(state, f"victron_l{phase}_current_a")


def _ev_current_raw(state: AppState, phase: int) -> float | None:
    """Raw EV current for the phase, or None when missing or outside the charger's supported range."""
    i = getattr(state, f"ev_current_raw_{_EV_SUFFIX[phase - 1]}")
    return i if i is not None and 0.0 <= i <= EV_MAX_PHASE_CURRENT_A else None


def _grid_voltage(state: AppState, phase: int) -> float:
    """Grid phase voltage from the meter, else 230 V. Only used to convert amps to setpoint watts."""
    v = getattr(state, f"victron_l{phase}_voltage_v")
    return float(v) if _voltage_ok(v) else _BREAKER_FALLBACK_VOLTAGE_V


def _ev_voltage(state: AppState, phase: int) -> float:
    v = getattr(state, f"ev_voltage_l{phase}_v")
    return float(v) if _voltage_ok(v) else _grid_voltage(state, phase)


def measured_ev_phase_powers_w(state: AppState, calibrated: bool = False) -> tuple[float, float, float] | None:
    """Return measured EV power per phase (V_ev * I), or None if any current is missing.

    ``calibrated=True`` uses the correction_pct-applied current (for the Eco follow guard only); the
    default is the raw charger current.
    """
    currents = [
        getattr(state, f"ev_current_{_EV_SUFFIX[n - 1]}") if calibrated else _ev_current_raw(state, n) for n in _PHASES
    ]
    if any(c is None for c in currents):
        return None
    p1, p2, p3 = (_ev_voltage(state, n) * float(currents[n - 1]) for n in _PHASES)  # type: ignore[arg-type]
    return (p1, p2, p3)


def record_phase_samples(loop: SamplingLoopProtocol, now: float | None = None, ev_fresh: bool = True) -> None:
    """Record one sample per phase (whenever the grid current is present) and prune the window.

    Grid current is recorded independently of EV-current availability so the import means and headroom
    always reflect fresh grid readings. A sample without a valid EV current is stored with ``ev_a`` None and
    the phase is not marked fresh, so EV-dependent decisions take the missing-data path.

    With ``ev_fresh`` False (EV reads skipped, e.g. Standby) only the grid side is recorded: the EV current is
    stored as 0 and no phase is marked fresh for the limiter, so retained EV currents never pair with new
    grid readings. The caller drops these samples when EV reads resume.
    """
    now = _time.monotonic() if now is None else now
    state = loop._state
    breaker = loop._breaker
    for n in _PHASES:
        i = _grid_current(state, n)
        i_ev = _ev_current_raw(state, n) if ev_fresh else 0.0
        breaker.phase_fresh[n - 1] = False
        if i is None:
            continue
        loop._phase_samples[n - 1].append(PhaseSample(t=now, import_a=max(0.0, i), ev_a=i_ev))
        breaker.phase_fresh[n - 1] = ev_fresh and i_ev is not None
    cutoff = now - _PHASE_IMPORT_MEAN_WINDOW_S
    loop._phase_samples = [[s for s in samples if s.t >= cutoff] for samples in loop._phase_samples]


def _window(loop: SamplingLoopProtocol, phase: int, now: float | None) -> list[PhaseSample]:
    samples = loop._phase_samples[phase - 1]
    if now is None:
        return samples
    cutoff = now - _PHASE_IMPORT_MEAN_WINDOW_S
    return [s for s in samples if s.t >= cutoff]


def mean_phase_import_a(loop: SamplingLoopProtocol, phase: int, now: float | None = None) -> float | None:
    """30 s mean of per-sample clamped import current for phase 1..3, or None without samples."""
    if phase not in _PHASES:
        return None
    samples = _window(loop, phase, now)
    if not samples:
        return None
    return sum(s.import_a for s in samples) / len(samples)


def phase_headroom_pct(loop: SamplingLoopProtocol, phase: int, now: float | None = None) -> float | None:
    """Headroom to the 80 % import-current limit as a percentage (max 100).

    None without samples or when the phase's current grid current is missing, so a failed read does not
    keep publishing an old value from the rolling buffer.
    """
    if phase in _PHASES and _grid_current(loop._state, phase) is None:
        return None
    mean_imp = mean_phase_import_a(loop, phase, now)
    if mean_imp is None:
        return None
    i_lim = _BREAKER_SAFETY_FRACTION * loop._state.grid_breaker_limit_a
    if i_lim <= 0:
        return None
    return min((i_lim - mean_imp) / i_lim * 100.0, 100.0)


class _Phase:
    """Per-cycle derived view of one phase (amps)."""

    def __init__(self, loop: SamplingLoopProtocol, n: int, now: float) -> None:
        state = loop._state
        i_brk = state.grid_breaker_limit_a
        samples = _window(loop, n, now)
        self.n = n
        self.v = _grid_voltage(state, n)
        self.i_lim = _BREAKER_SAFETY_FRACTION * i_brk
        count = len(samples)
        self.mean_a = sum(s.import_a for s in samples) / count
        # Cap inputs come from PAIRED samples only (grid and EV both present), so the excess and the mean EV
        # current describe the same observations. Grid-only samples still feed mean_a (overload detection,
        # headroom) and raw_a (fast trip). Usable phases always have at least one paired sample.
        paired = [s for s in samples if s.ev_a is not None]
        self.mean_ev_a = sum(s.ev_a for s in paired) / len(paired)  # type: ignore[misc]
        paired_mean_a = sum(s.import_a for s in paired) / len(paired)
        self.raw_a = samples[-1].import_a
        self.ev_active = (_ev_current_raw(state, n) or 0.0) > _PHASE_ACTIVE_THRESHOLD_A
        # Overload detection uses the full-window mean.
        self.excess_a = self.mean_a - self.i_lim
        self.fast_a = _BREAKER_INSTANT_THRESHOLD_FRACTION * i_brk
        self.fast = self.raw_a > self.fast_a
        self.over = self.excess_a > 0 or self.fast
        # The cap uses the paired excess, floored at 0. If the full mean is over the threshold but the paired
        # excess is <= 0, the cap stays anchored at the measured EV delivery (never uncapped, never raised).
        self.cap_excess_a = max(paired_mean_a - self.i_lim, 0.0)
        # The setpoint is a TOTAL over 3 phases split equally, so the per-phase EV current maps to 3x watts.
        self.cap_w = max(0.0, 3.0 * self.v * (self.mean_ev_a - self.cap_excess_a))

    def has_release_margin(self) -> bool:
        return self.mean_a < self.i_lim - _BREAKER_RELEASE_MARGIN_A

    def can_restart_at_minimum(self) -> bool:
        share_a = _MIN_CHARGE_W / _PHASE_COUNT / self.v
        return self.mean_a + share_a <= self.i_lim - _BREAKER_RELEASE_MARGIN_A


def _clear_throttles() -> None:
    for key in _THROTTLE_KEYS:
        _throttle.reset(key)


def _protect(
    loop: SamplingLoopProtocol, phases: list[_Phase], setpoint: float, *, partial: bool, now: float | None = None
) -> float:
    """Overload protection (trim, fast trip, stop counter, gradual release) over the given phases.

    With ``partial`` (some phase has no usable data) protection still runs on the healthy phases, but the
    cap is never released.
    """
    b: BreakerLimiterState = loop._breaker

    # --- Overload on phases the EV does not load: cannot be relieved by throttling --------
    for ph in phases:
        key = f"breaker_overload_non_ev_{ph.n}"
        if ph.over and not ph.ev_active:
            _throttle.warning(
                key,
                "Breaker protection: L%d import %.1f A (limit %.1f A) is overloaded but the EV draws nothing on "
                "this phase; cannot be relieved by EV throttling",
                ph.n,
                ph.mean_a,
                ph.i_lim,
            )
        else:
            _throttle.clear(key)

    over = [ph for ph in phases if ph.ev_active and ph.over]
    fast = [ph for ph in over if ph.fast]
    needed: float | None = None
    if fast:
        # Same binding rule as the trim: the phase with the lowest cap.
        ph = min(fast, key=lambda x: x.cap_w)
        needed = _MIN_CHARGE_W
        b.last_binding_phase = ph.n
        _throttle.warning(
            "breaker_fast_trip",
            "Breaker fast trip: L%d raw import %.1f A exceeds 90%% of breaker (%.1f A); "
            "EV current %.1f A; dropping to minimum %.0f W",
            ph.n,
            ph.raw_a,
            ph.fast_a,
            ph.mean_ev_a,
            _MIN_CHARGE_W,
        )
    elif over:
        # Binding phase = the most restrictive cap (not the largest excess: EV current and voltage differ).
        ph = min(over, key=lambda x: x.cap_w)
        needed = ph.cap_w
        b.last_binding_phase = ph.n
        _throttle.info(
            "breaker_cap_trim",
            "Breaker cap: binding phase L%d, mean import %.1f A, limit %.1f A, EV current %.1f A, "
            "excess %.1f A, cap %.0f W (total setpoint)",
            ph.n,
            ph.mean_a,
            ph.i_lim,
            ph.mean_ev_a,
            ph.cap_excess_a,
            ph.cap_w,
        )

    b.restart_at_minimum = False
    if needed is not None:
        b.cap_w = needed if b.cap_w is None else min(b.cap_w, needed)
        result = min(setpoint, max(b.cap_w, _MIN_CHARGE_W))
        # Still overloaded while the EFFECTIVE output is at the minimum -> count towards a stop. Counting the
        # calculated cap instead would reset every cycle once the cap ratchets to the minimum.
        b.min_overload_cycles = b.min_overload_cycles + 1 if result <= _MIN_CHARGE_W else 0
        if b.min_overload_cycles >= _BREAKER_STOP_CYCLES:
            b.stopped = True
            b.min_overload_cycles = 0
            _throttle.warning(
                "breaker_stop",
                "Breaker protection: still overloaded after %d cycles at minimum power; stopping charger",
                _BREAKER_STOP_CYCLES,
            )
            b.last_applied_w = None
            return 0.0
    else:
        b.min_overload_cycles = 0
        result = setpoint
        if b.cap_w is not None:
            active = [ph for ph in phases if ph.ev_active]
            # Only release while the EV is actually drawing, so the margin is observed under load.
            # Never release on partial data.
            if not partial and active and all(ph.has_release_margin() for ph in active):
                b.cap_w += _BREAKER_CAP_RELEASE_STEP_W
                if b.cap_w >= setpoint:
                    b.cap_w = None
                    _throttle.clear("breaker_cap_trim")
                    _throttle.clear("breaker_fast_trip")
            if b.cap_w is not None:
                result = min(setpoint, max(b.cap_w, _MIN_CHARGE_W))

    return min(result, _MAX_CHARGE_W)


def confirm_applied(loop: SamplingLoopProtocol, applied_w: float) -> None:
    """Record the output as the hold baseline. Call only after the charger write was confirmed."""
    if applied_w > 0 and not loop._breaker.stopped:
        loop._breaker.last_applied_w = applied_w


def _limit_with_missing_data(loop: SamplingLoopProtocol, setpoint: float, now: float, usable: list[int]) -> float:
    """Fail-safe when some phase has no usable data, still protecting the healthy phases.

    The result is the stricter of the missing-data fallback (hold, then minimum) and overload protection
    evaluated on the usable phases.
    """
    b: BreakerLimiterState = loop._breaker
    b.missing_cycles += 1
    if b.stopped:
        return 0.0
    if b.hold_ceiling_w is None:
        b.hold_ceiling_w = _MIN_CHARGE_W if b.last_applied_w is None else b.last_applied_w
    if setpoint <= 0:
        b.hold_ceiling_w = 0.0
        return setpoint
    if b.missing_cycles >= _BREAKER_MISSING_DATA_CYCLES:
        b.cap_w = _MIN_CHARGE_W
        _throttle.warning(
            "breaker_failsafe",
            "Breaker protection: grid/EV phase data missing for %d cycles; limiting to minimum %.0f W",
            b.missing_cycles,
            _MIN_CHARGE_W,
        )
        result = min(_MIN_CHARGE_W, b.hold_ceiling_w)
    else:
        # Debounce window: hold. Never ramp above the tightest output seen since data became invalid
        # (the last valid output, or the minimum if there is none).
        result = min(setpoint, b.hold_ceiling_w)
        if b.cap_w is not None:
            result = min(result, max(b.cap_w, _MIN_CHARGE_W))
    if usable:
        result = min(result, _protect(loop, [_Phase(loop, n, now) for n in usable], result, partial=True))
    b.hold_ceiling_w = min(b.hold_ceiling_w, result)
    return result


def limit_phase_current(loop: SamplingLoopProtocol, setpoint: float, now: float | None = None) -> float:
    """Return the TOTAL setpoint after main-breaker protection (only ever reduces it)."""
    now = _time.monotonic() if now is None else now
    state = loop._state
    b: BreakerLimiterState = loop._breaker

    # Standby: no EV Modbus activity, limiter irrelevant.
    if state.charge_mode == "Standby":
        loop._breaker = BreakerLimiterState()
        _clear_throttles()
        return setpoint

    # --- Fail-safe on missing / stale data ---------------------------------
    # A phase needs at least one paired (grid + EV) sample in the window for the cap; otherwise it is missing.
    usable = [n for n in _PHASES if b.phase_fresh[n - 1] and any(s.ev_a is not None for s in _window(loop, n, now))]
    if len(usable) < _PHASE_COUNT:
        return _limit_with_missing_data(loop, setpoint, now, usable)
    if b.missing_cycles:
        b.missing_cycles = 0
        _throttle.clear("breaker_failsafe")
    b.hold_ceiling_w = None

    phases = [_Phase(loop, n, now) for n in _PHASES]

    # --- Stop latch (evaluated before the setpoint<=0 early return so it can release) ----
    if b.stopped:
        if not all(ph.can_restart_at_minimum() for ph in phases):
            b.last_applied_w = None
            return 0.0
        b.stopped = False
        b.cap_w = _MIN_CHARGE_W
        b.restart_at_minimum = True
        b.min_overload_cycles = 0
        _throttle.clear("breaker_stop")
        logger.info("Breaker protection: grid import recovered, releasing stop latch (restart capped at minimum)")

    if setpoint <= 0:
        b.last_applied_w = None
        if not b.stopped:
            if not b.restart_at_minimum:
                b.cap_w = None
            b.min_overload_cycles = 0
        return setpoint

    return _protect(loop, phases, setpoint, partial=False, now=now)
