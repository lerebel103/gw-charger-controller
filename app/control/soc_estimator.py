"""Energy-based EV SOC estimator.

Pure logic: no clock, no I/O. The reported SOC arrives from a cloud API through Home Assistant and may freeze
while HA keeps republishing it. The estimator anchors on the last genuine SOC change and adds the energy the
charger has delivered since, converted with the battery capacity and the efficiency constant k.

    estimated = anchor_soc + (energy_now - energy_at_anchor) / (capacity_kwh * 1000 * k) * 100
"""

from dataclasses import dataclass
from enum import StrEnum

from app.control.constants import (
    _SOC_K_DEFAULT,
    _SOC_K_EMA_WEIGHT,
    _SOC_K_LEARN_MIN_ENERGY_KWH,
    _SOC_K_LEARN_MIN_SPAN_PCT,
    _SOC_K_MAX,
    _SOC_K_MIN,
    _SOC_MAX_ENERGY_STEP_WH,
    _SOC_STALL_TOLERANCE_PCT,
)
from app.state import AppState


class SocSource(StrEnum):
    """Where the effective SOC comes from."""

    REPORTED = "reported"
    ESTIMATED = "estimated"
    NONE = "none"


class AnchorOrigin(StrEnum):
    """How an anchor was created."""

    REPORTED = "reported"
    LAST_KNOWN = "last_known"


@dataclass(frozen=True)
class Anchor:
    """Reference point for the energy integration (energy is the accumulated delta, not the raw register)."""

    soc_pct: float
    energy_wh: float
    origin: AnchorOrigin


@dataclass(frozen=True)
class SocResolution:
    """Result of resolving the effective SOC at one instant."""

    effective_pct: float | None
    source: SocSource
    reported_pct: float | None  # fresh reported value, None when stale or missing
    estimated_pct: float | None  # None without an anchor
    stalled: bool
    stale: bool  # reported value missing or stale


@dataclass(frozen=True)
class KLearnResult:
    """Outcome of one k refinement step."""

    k_span: float
    k_new: float


def effective_k(state: AppState) -> float:
    """Return the learned k if it belongs to the current capacity, else the default."""
    if state.ev_soc_k is not None and state.ev_soc_k_capacity_kwh == state.ev_battery_capacity_kwh:
        return state.ev_soc_k
    return _SOC_K_DEFAULT


class SocEstimator:
    """Tracks session energy and the last genuine SOC change to estimate the battery SOC."""

    def __init__(self) -> None:
        self._anchor: Anchor | None = None
        self._last_known_soc_pct: float | None = None
        self._prev_energy_wh: float | None = None
        self._acc_wh: float = 0.0
        self._discontinuities: int = 0
        self._span_first: Anchor | None = None
        self._span_capacity_kwh: float | None = None
        self._last_k_span: float | None = None

    @property
    def anchor(self) -> Anchor | None:
        return self._anchor

    @property
    def last_known_soc_pct(self) -> float | None:
        return self._last_known_soc_pct

    @property
    def accumulated_wh(self) -> float:
        return self._acc_wh

    @property
    def discontinuities(self) -> int:
        """Count of energy-register jumps treated as discontinuities (glitch guard)."""
        return self._discontinuities

    @property
    def last_k_span(self) -> float | None:
        """Most recent k_span computed from a refinement span (diagnostics and tests)."""
        return self._last_k_span

    def observe(
        self,
        *,
        soc_pct: float | None,
        soc_fresh: bool,
        energy_wh: float | None,
        capacity_kwh: float | None = None,
        k: float = _SOC_K_DEFAULT,
    ) -> KLearnResult | None:
        """Feed one cycle of inputs: accumulate energy, anchor on genuine SOC changes, refine k.

        Returns a KLearnResult when a new genuine anchor completed a valid learning span, else None.
        """
        if capacity_kwh != self._span_capacity_kwh:
            self._span_first = None
            self._span_capacity_kwh = capacity_kwh
        if not self._accumulate(energy_wh):
            self._span_first = None
        if soc_pct is None or not soc_fresh:
            self._span_first = None
            return None
        self._last_known_soc_pct = soc_pct
        anchor = self._anchor
        stalled = False
        if anchor is not None and capacity_kwh is not None and capacity_kwh > 0 and k > 0:
            delivered_wh = max(self._acc_wh - anchor.energy_wh, 0.0)
            estimate = anchor.soc_pct + delivered_wh / (capacity_kwh * 1000.0 * k) * 100.0
            stalled = (estimate - soc_pct) > _SOC_STALL_TOLERANCE_PCT
        if stalled:
            self._span_first = None
        if anchor is not None and soc_pct == anchor.soc_pct:
            return None
        decreased = anchor is not None and soc_pct < anchor.soc_pct
        new_anchor = Anchor(soc_pct, self._acc_wh, AnchorOrigin.REPORTED)
        self._anchor = new_anchor
        if decreased:
            self._span_first = None
        first = self._span_first
        if first is None:
            self._span_first = new_anchor
            return None
        return self._refine(first, new_anchor, capacity_kwh, k)

    def _refine(self, first: Anchor, last: Anchor, capacity_kwh: float | None, k: float) -> KLearnResult | None:
        """Compute k_span over the span and take one EMA step toward it."""
        delta_soc = last.soc_pct - first.soc_pct
        delta_kwh = (last.energy_wh - first.energy_wh) / 1000.0
        if capacity_kwh is None or capacity_kwh <= 0:
            return None
        if delta_soc < _SOC_K_LEARN_MIN_SPAN_PCT or delta_kwh < _SOC_K_LEARN_MIN_ENERGY_KWH:
            return None
        k_span = delta_kwh / delta_soc * 100.0 / capacity_kwh
        self._last_k_span = k_span
        if not _SOC_K_MIN <= k_span <= _SOC_K_MAX:
            return None
        k_new = max(_SOC_K_MIN, min(k + _SOC_K_EMA_WEIGHT * (k_span - k), _SOC_K_MAX))
        return KLearnResult(k_span=k_span, k_new=k_new)

    def _accumulate(self, energy_wh: float | None) -> bool:
        """Add the energy step; return False when the step is a gap (missing, reset or discontinuity)."""
        if energy_wh is None:
            return False
        prev = self._prev_energy_wh
        self._prev_energy_wh = energy_wh
        if prev is None:
            return True
        delta = energy_wh - prev
        if delta < 0:
            return False  # register reset at a new session
        if delta > _SOC_MAX_ENERGY_STEP_WH:
            self._discontinuities += 1
            return False
        self._acc_wh += delta
        return True

    def begin_session(self) -> None:
        """Plug-in edge: drop the anchor, fall back to the last known reported SOC if any."""
        self._anchor = None
        self._span_first = None
        if self._last_known_soc_pct is not None:
            self._anchor = Anchor(self._last_known_soc_pct, self._acc_wh, AnchorOrigin.LAST_KNOWN)

    def end_session(self) -> None:
        """Unplug edge: clear the anchor, keep the last known SOC."""
        self._anchor = None
        self._span_first = None

    def estimated_pct(self, capacity_kwh: float, k: float) -> float | None:
        """Return the energy-based SOC estimate clamped to [0, 100], or None without an anchor."""
        anchor = self._anchor
        if anchor is None or capacity_kwh <= 0 or k <= 0:
            return None
        delivered_wh = max(self._acc_wh - anchor.energy_wh, 0.0)
        estimate = anchor.soc_pct + delivered_wh / (capacity_kwh * 1000.0 * k) * 100.0
        return max(0.0, min(estimate, 100.0))

    def resolve(
        self,
        *,
        reported_pct: float | None,
        reported_fresh: bool,
        capacity_kwh: float,
        k: float,
    ) -> SocResolution:
        """Pick the effective SOC: reported when healthy, estimated when stalled or stale."""
        reported = reported_pct if reported_fresh else None
        estimated = self.estimated_pct(capacity_kwh, k)
        if estimated is None:
            return SocResolution(
                reported,
                SocSource.REPORTED if reported is not None else SocSource.NONE,
                reported,
                None,
                stalled=False,
                stale=reported is None,
            )
        stalled = reported is not None and (estimated - reported) > _SOC_STALL_TOLERANCE_PCT
        if stalled or reported is None:
            return SocResolution(
                estimated, SocSource.ESTIMATED, reported, estimated, stalled=stalled, stale=reported is None
            )
        return SocResolution(reported, SocSource.REPORTED, reported, estimated, stalled=False, stale=False)
