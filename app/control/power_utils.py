"""Power measurement utilities and setpoint calculation helpers."""

import logging
import time as _time
from datetime import datetime

from app.control.constants import (
    _EV_MAX_SOC_MARGIN_PCT,
    _EV_SOC_STALE_S,
    _MAX_CHARGE_W,
    _MIN_CHARGE_W,
    _SOC_ESTIMATE_MARGIN_PCT,
)
from app.control.protocols import SamplingLoopProtocol
from app.control.soc_estimator import SocResolution, SocSource, effective_k
from app.control.time_utils import parse_hhmm, validate_hhmm

logger = logging.getLogger(__name__)


def clamp(value: float, min_val: float, max_val: float) -> float:
    """Clamp *value* to the range [min_val, max_val]."""
    return max(min_val, min(value, max_val))


def ev_soc_is_fresh(loop: SamplingLoopProtocol) -> bool:
    """Return True if a reported SOC exists and was received within the stale window."""
    state = loop._state
    if state.ev_soc_pct is None or state.ev_soc_pct_updated_at is None:
        return False
    return (_time.monotonic() - state.ev_soc_pct_updated_at) <= _EV_SOC_STALE_S


def resolve_ev_soc(loop: SamplingLoopProtocol) -> SocResolution:
    """Resolve the effective SOC (reported when healthy, estimated when stalled or stale)."""
    state = loop._state
    return loop._soc_estimator.resolve(
        reported_pct=state.ev_soc_pct,
        reported_fresh=ev_soc_is_fresh(loop),
        capacity_kwh=state.ev_battery_capacity_kwh,
        k=effective_k(state),
    )


def get_ev_soc(loop: SamplingLoopProtocol) -> float | None:
    """Return the effective SOC: reported if healthy, estimated if stalled or stale, else None."""
    return resolve_ev_soc(loop).effective_pct


def max_soc_stop_reason(loop: SamplingLoopProtocol) -> str | None:
    """Return the stop reason when the max-SOC limit is reached, else None.

    ``max_soc_reached`` comes from the reported SOC (0.5 pp margin only at a 100 % limit). ``max_soc_estimated``
    comes from the energy estimate: no margin while the report is healthy (the estimate is only a safety net),
    ``_SOC_ESTIMATE_MARGIN_PCT`` once the estimate is the SOC source. ``ev_max_soc_pct`` is authoritative.
    Returns None when the "Charge to set max SoC" switch is off (``charge_to_max_soc_enabled`` False): the
    target then never stops or blocks a session. Only this check is disabled; the SOC itself is unchanged.
    """
    if not loop._state.charge_to_max_soc_enabled:
        return None
    limit = loop._state.ev_max_soc_pct
    resolution = resolve_ev_soc(loop)
    rep_margin = _EV_MAX_SOC_MARGIN_PCT if limit >= 100.0 else 0.0
    if resolution.reported_pct is not None and resolution.reported_pct >= limit - rep_margin:
        return "max_soc_reached"
    if resolution.estimated_pct is not None:
        est_margin = _SOC_ESTIMATE_MARGIN_PCT if resolution.source is SocSource.ESTIMATED else 0.0
        if resolution.estimated_pct >= limit - est_margin:
            return "max_soc_estimated"
    return None


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
