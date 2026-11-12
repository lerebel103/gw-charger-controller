"""Closed-loop main-breaker protection based on per-phase grid IMPORT power.

Design summary (see docs/grid-power-scaling.md):

* Per-phase grid power is signed (+ import, - export) and accurate; meter currents are
  unsigned magnitudes. Import power per phase is the proxy for breaker current:
  ``P_lim = 0.80 * grid_breaker_limit_a * V``. Export never creates load or headroom.
* The charger setpoint is a TOTAL across three phases and only a hint. The real EV load
  per phase is measured as ``V_ev * I_ev_raw``. A cap is therefore ``3 * (P_ev - excess)``,
  anchored to measured EV power rather than to the setpoint.
* Only EV-active phases can be relieved by throttling the EV. Overload on any other phase
  is logged but never capped.
* Missing, stale or implausible data holds at the last applied output for the debounce window, then
  fails safe to the minimum charge power in every mode.
"""

import logging
import time as _time

from app.control.breaker_state import BreakerLimiterState, PhaseSample
from app.control.constants import (
    _BREAKER_CAP_RELEASE_STEP_W,
    _BREAKER_FALLBACK_VOLTAGE_V,
    _BREAKER_INSTANT_THRESHOLD_FRACTION,
    _BREAKER_MISSING_DATA_CYCLES,
    _BREAKER_PLAUSIBILITY_RATIO,
    _BREAKER_PLAUSIBILITY_TOLERANCE_W,
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
    *(f"breaker_implausible_{n}" for n in _PHASES),
)


def _voltage_ok(v: float | None) -> bool:
    return v is not None and 100.0 < v < 300.0


def _grid_power(state: AppState, phase: int) -> float | None:
    return getattr(state, f"victron_l{phase}_power_w")


def _grid_current(state: AppState, phase: int) -> float | None:
    return getattr(state, f"victron_l{phase}_current_a")


def _ev_current_raw(state: AppState, phase: int) -> float | None:
    return getattr(state, f"ev_current_raw_{_EV_SUFFIX[phase - 1]}")


def _grid_voltage(state: AppState, phase: int) -> float:
    """Grid phase voltage: meter reading, else |P|/I, else 230 V."""
    v = getattr(state, f"victron_l{phase}_voltage_v")
    if _voltage_ok(v):
        return float(v)
    p, i = _grid_power(state, phase), _grid_current(state, phase)
    if p is not None and i is not None and i > 1.0 and _voltage_ok(abs(p) / i):
        return abs(p) / i
    return _BREAKER_FALLBACK_VOLTAGE_V


def _ev_voltage(state: AppState, phase: int) -> float:
    v = getattr(state, f"ev_voltage_l{phase}_v")
    return float(v) if _voltage_ok(v) else _grid_voltage(state, phase)


def measured_ev_phase_powers_w(state: AppState, calibrated: bool = False) -> tuple[float, float, float] | None:
    """Return measured EV power per phase (V_ev * I), or None if any current is missing.

    Breaker protection uses the RAW charger current. ``calibrated=True`` uses the correction_pct-applied
    current instead (for the Eco follow guard only).
    """
    currents = [
        getattr(state, f"ev_current_{_EV_SUFFIX[n - 1]}") if calibrated else _ev_current_raw(state, n) for n in _PHASES
    ]
    if any(c is None for c in currents):
        return None
    p1, p2, p3 = (_ev_voltage(state, n) * float(currents[n - 1]) for n in _PHASES)  # type: ignore[arg-type]
    return (p1, p2, p3)


def _grid_inputs_plausible(state: AppState, phase: int) -> bool:
    """True when this phase's grid power and current are present and mutually plausible."""
    p, i = _grid_power(state, phase), _grid_current(state, phase)
    if p is None or i is None:
        return False
    return abs(p) <= _BREAKER_PLAUSIBILITY_RATIO * _grid_voltage(state, phase) * i + _BREAKER_PLAUSIBILITY_TOLERANCE_W


def record_phase_samples(loop: SamplingLoopProtocol, now: float | None = None, ev_fresh: bool = True) -> None:
    """Record one sample per phase (when inputs are present and plausible) and prune the window.

    With ``ev_fresh`` False (EV reads skipped, e.g. Standby) only the grid side is recorded: the EV power is
    stored as 0 and no phase is marked fresh for the limiter, so retained EV currents never pair with new
    grid readings. The caller drops these samples when EV reads resume.
    """
    now = _time.monotonic() if now is None else now
    state = loop._state
    breaker = loop._breaker
    for n in _PHASES:
        p = _grid_power(state, n)
        i = _grid_current(state, n)
        i_ev = _ev_current_raw(state, n) if ev_fresh else 0.0
        breaker.phase_fresh[n - 1] = False
        if p is None or i is None or i_ev is None:
            continue
        v = _grid_voltage(state, n)
        key = f"breaker_implausible_{n}"
        if not _grid_inputs_plausible(state, n):
            _throttle.warning(
                key,
                "Breaker protection: L%d grid data implausible (P=%.0f W, I=%.1f A, V=%.0f V); treating as missing",
                n,
                p,
                i,
                v,
            )
            continue
        _throttle.clear(key)
        loop._phase_samples[n - 1].append(
            PhaseSample(
                t=now,
                import_w=max(0.0, p),
                import_a=i if p > 0 else 0.0,
                ev_w=_ev_voltage(state, n) * i_ev,
            )
        )
        breaker.phase_fresh[n - 1] = ev_fresh
    cutoff = now - _PHASE_IMPORT_MEAN_WINDOW_S
    loop._phase_samples = [[s for s in samples if s.t >= cutoff] for samples in loop._phase_samples]


def _window(loop: SamplingLoopProtocol, phase: int, now: float | None) -> list[PhaseSample]:
    samples = loop._phase_samples[phase - 1]
    if now is None:
        return samples
    cutoff = now - _PHASE_IMPORT_MEAN_WINDOW_S
    return [s for s in samples if s.t >= cutoff]


def mean_phase_import_w(loop: SamplingLoopProtocol, phase: int, now: float | None = None) -> float | None:
    """30 s mean of per-sample clamped import power for phase 1..3, or None without samples."""
    if phase not in _PHASES:
        return None
    samples = _window(loop, phase, now)
    if not samples:
        return None
    return sum(s.import_w for s in samples) / len(samples)


def phase_headroom_pct(loop: SamplingLoopProtocol, phase: int, now: float | None = None) -> float | None:
    """Headroom to the 80 % import-power limit as a percentage (max 100).

    None without samples or when the phase's current grid inputs are missing / implausible, so a failed read
    does not keep publishing an old value from the rolling buffer.
    """
    if phase in _PHASES and not _grid_inputs_plausible(loop._state, phase):
        return None
    mean_imp = mean_phase_import_w(loop, phase, now)
    if mean_imp is None:
        return None
    p_lim = _BREAKER_SAFETY_FRACTION * loop._state.grid_breaker_limit_a * _grid_voltage(loop._state, phase)
    if p_lim <= 0:
        return None
    return min((p_lim - mean_imp) / p_lim * 100.0, 100.0)


class _Phase:
    """Per-cycle derived view of one phase."""

    def __init__(self, loop: SamplingLoopProtocol, n: int, now: float) -> None:
        state = loop._state
        i_brk = state.grid_breaker_limit_a
        samples = _window(loop, n, now)
        self.n = n
        self.v = _grid_voltage(state, n)
        self.p_lim = _BREAKER_SAFETY_FRACTION * i_brk * self.v
        self.i_lim = _BREAKER_SAFETY_FRACTION * i_brk
        count = len(samples)
        self.mean_imp = sum(s.import_w for s in samples) / count
        self.mean_imp_a = sum(s.import_a for s in samples) / count
        self.mean_ev = sum(s.ev_w for s in samples) / count
        self.raw_w = samples[-1].import_w
        self.raw_a = samples[-1].import_a
        self.ev_active = (_ev_current_raw(state, n) or 0.0) > _PHASE_ACTIVE_THRESHOLD_A
        # Current guard covers power factor < 1: import current may exceed 80 % while power does not.
        self.excess_w = max(self.mean_imp - self.p_lim, (self.mean_imp_a - self.i_lim) * self.v)
        fast_a = _BREAKER_INSTANT_THRESHOLD_FRACTION * i_brk
        self.fast = self.raw_w > fast_a * self.v or self.raw_a > fast_a
        self.over = self.excess_w > 0 or self.fast
        self.cap_w = max(0.0, 3.0 * (self.mean_ev - max(self.excess_w, 0.0)))

    def has_release_margin(self) -> bool:
        return (
            self.mean_imp < self.p_lim - _BREAKER_RELEASE_MARGIN_A * self.v
            and self.mean_imp_a < self.i_lim - _BREAKER_RELEASE_MARGIN_A
        )

    def can_restart_at_minimum(self) -> bool:
        share_w = _MIN_CHARGE_W / _PHASE_COUNT
        return (
            self.mean_imp + share_w <= self.p_lim - _BREAKER_RELEASE_MARGIN_A * self.v
            and self.mean_imp_a + share_w / self.v <= self.i_lim - _BREAKER_RELEASE_MARGIN_A
        )


def _clear_throttles() -> None:
    for key in _THROTTLE_KEYS:
        _throttle.reset(key)


def _protect(
    loop: SamplingLoopProtocol, phases: list[_Phase], setpoint: float, *, partial: bool, now: float | None = None
) -> float:
    """Overload protection (trim, fast trip, stop counter, gradual release) over the given phases.

    With ``partial`` (some phase has no usable data) protection still runs on the healthy phases, but the
    cap is never released and ``last_applied_w`` is left untouched.
    """
    state = loop._state
    b: BreakerLimiterState = loop._breaker

    # --- Overload on phases the EV does not load: cannot be relieved by throttling --------
    for ph in phases:
        key = f"breaker_overload_non_ev_{ph.n}"
        if ph.over and not ph.ev_active:
            _throttle.warning(
                key,
                "Breaker protection: L%d import %.0f W (limit %.0f W) is overloaded but the EV draws nothing on "
                "this phase; cannot be relieved by EV throttling",
                ph.n,
                ph.mean_imp,
                ph.p_lim,
            )
        else:
            _throttle.clear(key)

    over = [ph for ph in phases if ph.ev_active and ph.over]
    fast = [ph for ph in over if ph.fast]
    needed: float | None = None
    if fast:
        ph = max(fast, key=lambda x: max(x.raw_w, x.raw_a * x.v))
        needed = _MIN_CHARGE_W
        b.last_binding_phase = ph.n
        _throttle.warning(
            "breaker_fast_trip",
            "Breaker fast trip: L%d raw import %.0f W / %.1f A exceeds 90%% of breaker (%.0f W); "
            "EV phase power %.0f W; dropping to minimum %.0f W",
            ph.n,
            ph.raw_w,
            ph.raw_a,
            _BREAKER_INSTANT_THRESHOLD_FRACTION * state.grid_breaker_limit_a * ph.v,
            ph.mean_ev,
            _MIN_CHARGE_W,
        )
    elif over:
        ph = min(over, key=lambda x: x.cap_w)
        needed = ph.cap_w
        b.last_binding_phase = ph.n
        _throttle.info(
            "breaker_cap_trim",
            "Breaker cap: binding phase L%d, mean import %.0f W, limit %.0f W, EV phase power %.0f W, "
            "excess %.0f W, cap %.0f W (total setpoint)",
            ph.n,
            ph.mean_imp,
            ph.p_lim,
            ph.mean_ev,
            max(ph.excess_w, 0.0),
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

    result = min(result, _MAX_CHARGE_W)
    if not partial:
        b.last_applied_w = result
    return result


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

    # --- Fail-safe on missing / stale / implausible data ---------------------------------
    usable = [n for n in _PHASES if b.phase_fresh[n - 1] and _window(loop, n, now)]
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
