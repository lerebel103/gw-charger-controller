"""Power measurement utilities and setpoint calculation helpers."""

import logging
import time as _time
from datetime import datetime

from app.control.constants import (
    _BREAKER_CAP_RESTART_MARGIN_W,
    _BREAKER_INSTANT_THRESHOLD_FRACTION,
    _BREAKER_SAFETY_FRACTION,
    _EV_SOC_STALE_S,
    _MAX_CHARGE_W,
    _MIN_CHARGE_W,
    _PHASE_ACTIVE_THRESHOLD_A,
    _PHASE_CURRENT_MEAN_WINDOW_S,
)
from app.control.protocols import SamplingLoopProtocol
from app.control.time_utils import parse_hhmm, validate_hhmm
from app.log_throttle import LogThrottle

logger = logging.getLogger(__name__)
_breaker_throttle = LogThrottle(logger, suppress_seconds=60.0)


def clamp(value: float, min_val: float, max_val: float) -> float:
    """Clamp *value* to the range [min_val, max_val]."""
    return max(min_val, min(value, max_val))


def get_ev_soc(loop: SamplingLoopProtocol) -> float | None:
    """Return ev_soc_pct if fresh, else None."""
    state = loop._state
    if state.ev_soc_pct is None or state.ev_soc_pct_updated_at is None:
        return None
    if (_time.monotonic() - state.ev_soc_pct_updated_at) > _EV_SOC_STALE_S:
        return None
    return state.ev_soc_pct


def record_samples(loop: SamplingLoopProtocol) -> None:
    """Record current grid and battery power readings into rolling buffers."""
    now = _time.monotonic()
    if loop._state.grid_power_w is not None:
        loop._grid_power_samples.append((now, loop._state.grid_power_w))
    if loop._state.solar_battery_power_w is not None:
        loop._battery_power_samples.append((now, loop._state.solar_battery_power_w))
    prune_samples(loop)


def prune_samples(loop: SamplingLoopProtocol) -> None:
    """Remove samples older than the configured window."""
    cutoff = _time.monotonic() - (loop._state.eco_mean_window_minutes * 60)
    loop._grid_power_samples = [(t, v) for t, v in loop._grid_power_samples if t >= cutoff]
    loop._battery_power_samples = [(t, v) for t, v in loop._battery_power_samples if t >= cutoff]


def mean_grid_power(loop: SamplingLoopProtocol) -> float | None:
    """Return the mean grid power over the rolling window."""
    if not loop._grid_power_samples:
        return None
    return sum(v for _, v in loop._grid_power_samples) / len(loop._grid_power_samples)


def mean_battery_power(loop: SamplingLoopProtocol) -> float | None:
    """Return the mean battery power over the rolling window."""
    if not loop._battery_power_samples:
        return None
    return sum(v for _, v in loop._battery_power_samples) / len(loop._battery_power_samples)


def compute_grid_fallback_setpoint(loop: SamplingLoopProtocol, ev_soc: float) -> float:
    """Calculate grid power needed to reach ev_min_soc_pct by discharge-window end."""
    state = loop._state

    soc_gap = state.ev_min_soc_pct - ev_soc
    if soc_gap <= 0:
        return 0.0
    energy_needed_kwh = soc_gap / 100.0 * state.ev_battery_capacity_kwh

    if not validate_hhmm(state.solar_battery_discharge_end):
        return _MIN_CHARGE_W

    end_time = parse_hhmm(state.solar_battery_discharge_end)
    now = datetime.now().time()  # noqa: DTZ005

    now_s = now.hour * 3600 + now.minute * 60 + now.second
    end_s = end_time.hour * 3600 + end_time.minute * 60
    remaining_s = end_s - now_s
    if remaining_s <= 0:
        remaining_s += 86400

    if remaining_s < 60:
        return 0.0

    remaining_h = remaining_s / 3600.0
    required_w = (energy_needed_kwh / remaining_h) * 1000.0

    logger.debug(
        "Eco night grid fallback: EV SOC %.0f%% -> %.0f%%, need %.1f kWh in %.1f h, required %.0f W",
        ev_soc,
        state.ev_min_soc_pct,
        energy_needed_kwh,
        remaining_h,
        required_w,
    )

    return clamp(required_w, _MIN_CHARGE_W, _MAX_CHARGE_W)


def limit_battery_discharge(loop: SamplingLoopProtocol, setpoint: float, max_discharge_w: float) -> float:
    """Reduce setpoint if home battery discharge exceeds the allowed limit."""
    battery_power = loop._state.solar_battery_power_w
    if battery_power is not None and battery_power < 0:
        overshoot = abs(battery_power) - max_discharge_w
        if overshoot > 0:
            setpoint -= overshoot
            if setpoint < _MIN_CHARGE_W:
                return 0.0
            setpoint = clamp(setpoint, _MIN_CHARGE_W, _MAX_CHARGE_W)
    return setpoint


def record_phase_current_samples(loop: SamplingLoopProtocol) -> None:
    """Record per-phase grid current readings into fixed 30 s rolling buffers."""
    now = _time.monotonic()
    state = loop._state
    if state.victron_l1_current_a is not None:
        loop._l1_current_samples.append((now, state.victron_l1_current_a))
    if state.victron_l2_current_a is not None:
        loop._l2_current_samples.append((now, state.victron_l2_current_a))
    if state.victron_l3_current_a is not None:
        loop._l3_current_samples.append((now, state.victron_l3_current_a))
    _prune_phase_current_samples(loop)


def _prune_phase_current_samples(loop: SamplingLoopProtocol) -> None:
    """Remove phase-current samples older than the fixed 30 s window."""
    cutoff = _time.monotonic() - _PHASE_CURRENT_MEAN_WINDOW_S
    loop._l1_current_samples = [(t, v) for t, v in loop._l1_current_samples if t >= cutoff]
    loop._l2_current_samples = [(t, v) for t, v in loop._l2_current_samples if t >= cutoff]
    loop._l3_current_samples = [(t, v) for t, v in loop._l3_current_samples if t >= cutoff]


def mean_phase_current(loop: SamplingLoopProtocol, phase: int) -> float | None:
    """Return the 30 s rolling mean of grid current for the given phase (1, 2, or 3).

    Returns None if no samples are available for the requested phase.
    """
    if phase == 1:
        samples = loop._l1_current_samples
    elif phase == 2:
        samples = loop._l2_current_samples
    elif phase == 3:
        samples = loop._l3_current_samples
    else:
        return None
    if not samples:
        return None
    return sum(v for _, v in samples) / len(samples)


def limit_phase_current(loop: SamplingLoopProtocol, setpoint: float) -> float:
    """Clamp setpoint to protect the main breaker based on per-phase current headroom.

    This is the final safety clamp applied after mode-specific setpoint computation
    and state-machine processing. It only ever reduces the setpoint.

    Returns setpoint unchanged when required data is unavailable (EC-1).
    """
    if setpoint <= 0:
        return setpoint

    state = loop._state
    i_brk = state.grid_breaker_limit_a
    safety_limit = _BREAKER_SAFETY_FRACTION * i_brk
    instant_threshold = _BREAKER_INSTANT_THRESHOLD_FRACTION * i_brk

    # Gather per-phase EV current for active phase detection and baseline isolation
    ev_currents = (state.ev_current_a, state.ev_current_b, state.ev_current_c)
    voltages = (state.victron_l1_voltage_v, state.victron_l2_voltage_v, state.victron_l3_voltage_v)
    raw_grid_currents = (state.victron_l1_current_a, state.victron_l2_current_a, state.victron_l3_current_a)

    # EC-1: skip if any essential data is missing
    if any(v is None for v in voltages):
        return setpoint
    if any(v is None for v in raw_grid_currents):
        return setpoint
    if any(v is None for v in ev_currents):
        return setpoint

    # Get rolling means per phase
    means = (mean_phase_current(loop, 1), mean_phase_current(loop, 2), mean_phase_current(loop, 3))
    if any(m is None for m in means):
        return setpoint

    # FR-8: Detect active phases from measured EV current
    active_phases: list[int] = []
    for idx, ev_i in enumerate(ev_currents):
        if ev_i is not None and ev_i > _PHASE_ACTIVE_THRESHOLD_A:
            active_phases.append(idx)

    # EC-10: If charger not drawing, use startup fallback (all 3 phases)
    if not active_phases:
        active_phases = [0, 1, 2]

    # Assign typed locals for the loop — None cases already filtered above
    mean_vals: tuple[float, float, float] = (means[0], means[1], means[2])  # type: ignore[assignment]
    raw_vals: tuple[float, float, float] = (
        raw_grid_currents[0],  # type: ignore[assignment]
        raw_grid_currents[1],  # type: ignore[assignment]
        raw_grid_currents[2],  # type: ignore[assignment]
    )
    ev_vals: tuple[float, float, float] = (
        ev_currents[0] if ev_currents[0] is not None else 0.0,
        ev_currents[1] if ev_currents[1] is not None else 0.0,
        ev_currents[2] if ev_currents[2] is not None else 0.0,
    )
    volt_vals: tuple[float, float, float] = (voltages[0], voltages[1], voltages[2])  # type: ignore[assignment]

    # Compute per-phase headroom
    binding_phase = -1
    binding_phase_current = 0.0
    min_headroom_a = float("inf")

    for idx in active_phases:
        # FR-12: Instantaneous override — use raw if above 0.90 * I_brk
        i_grid = raw_vals[idx] if raw_vals[idx] > instant_threshold else mean_vals[idx]
        i_base = i_grid - ev_vals[idx]
        i_ev_max = safety_limit - i_base

        if i_ev_max < min_headroom_a:
            min_headroom_a = i_ev_max
            binding_phase = idx
            binding_phase_current = i_grid

    headroom_a = max(0.0, min_headroom_a)

    # Compute P_cap from headroom and active phase voltages
    voltage_sum = sum(volt_vals[idx] for idx in active_phases)
    p_cap = headroom_a * voltage_sum

    # FR-13: Restart hysteresis — if previously tripped, require extra margin
    if loop._breaker_cap_tripped:
        if p_cap < _MIN_CHARGE_W + _BREAKER_CAP_RESTART_MARGIN_W:
            return 0.0
        else:
            loop._breaker_cap_tripped = False

    # Apply cap
    if p_cap >= setpoint:
        return setpoint

    capped_setpoint = p_cap

    # FR-4: If below charger minimum, force stop
    if capped_setpoint < _MIN_CHARGE_W:
        capped_setpoint = 0.0
        loop._breaker_cap_tripped = True

    # FR-16: Throttled log when cap actively reduces setpoint
    phase_label = f"L{binding_phase + 1}" if binding_phase >= 0 else "?"
    _breaker_throttle.warning(
        "breaker_cap_active",
        "Breaker cap active: setpoint %.0f W → %.0f W (binding phase %s at %.1f A, limit %.1f A)",
        setpoint,
        capped_setpoint,
        phase_label,
        binding_phase_current,
        safety_limit,
    )

    return capped_setpoint
