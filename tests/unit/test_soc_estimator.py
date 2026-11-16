"""Unit tests for the energy-based EV SOC estimator (pure logic, no clock)."""

from __future__ import annotations

import time as _time

import pytest

from app.control.power_utils import max_soc_stop_reason
from app.control.soc_estimator import AnchorOrigin, SocEstimator, SocSource, effective_k
from app.state import AppState
from tests.unit.helpers import make_ns_loop

CAP = 82.0


def _obs(est: SocEstimator, soc: float | None, energy: float | None, *, fresh: bool = True) -> None:
    est.observe(soc_pct=soc, soc_fresh=fresh, energy_wh=energy)


def _deliver(est: SocEstimator, start_wh: float, delivered_wh: float) -> None:
    """Advance the energy register in 1 kWh steps (single steps above 3 kWh are discontinuities)."""
    done = 0.0
    while done < delivered_wh:
        done = min(done + 1000.0, delivered_wh)
        est.observe(soc_pct=None, soc_fresh=False, energy_wh=start_wh + done)


def _resolve(est: SocEstimator, reported: float | None, *, fresh: bool = True, k: float = 1.0):
    return est.resolve(reported_pct=reported, reported_fresh=fresh, capacity_kwh=CAP, k=k)


class TestEstimateMath:
    def test_estimate_adds_energy_over_capacity(self):
        est = SocEstimator()
        _obs(est, 40.0, 0.0)
        _deliver(est, 0.0, 4100.0)
        assert est.estimated_pct(CAP, 1.0) == pytest.approx(45.0)

    def test_k_scales_the_conversion(self):
        est = SocEstimator()
        _obs(est, 40.0, 0.0)
        _deliver(est, 0.0, 4100.0)
        assert est.estimated_pct(CAP, 1.05) == pytest.approx(40.0 + 5.0 / 1.05)

    def test_clamped_to_0_100(self):
        est = SocEstimator()
        _obs(est, 99.0, 0.0)
        _deliver(est, 0.0, 2000.0)
        assert est.estimated_pct(CAP, 1.0) == 100.0

    def test_no_anchor_gives_none(self):
        assert SocEstimator().estimated_pct(CAP, 1.0) is None

    def test_invalid_capacity_gives_none(self):
        est = SocEstimator()
        _obs(est, 40.0, 0.0)
        assert est.estimated_pct(0.0, 1.0) is None


class TestAnchoring:
    def test_first_value_creates_reported_anchor(self):
        est = SocEstimator()
        _obs(est, 40.0, 100.0)
        assert est.anchor is not None
        assert est.anchor.soc_pct == 40.0
        assert est.anchor.origin is AnchorOrigin.REPORTED

    def test_same_value_does_not_reanchor(self):
        est = SocEstimator()
        _obs(est, 40.0, 0.0)
        first = est.anchor
        _obs(est, 40.0, 500.0)
        assert est.anchor is first

    def test_change_reanchors_at_current_energy(self):
        est = SocEstimator()
        _obs(est, 40.0, 0.0)
        _obs(est, 40.0, 500.0)
        _obs(est, 40.5, 600.0)
        assert est.anchor is not None
        assert (est.anchor.soc_pct, est.anchor.energy_wh) == (40.5, 600.0)

    def test_decrease_reanchors(self):
        est = SocEstimator()
        _obs(est, 48.5, 0.0)
        _obs(est, 43.0, 0.0)
        assert est.anchor is not None
        assert est.anchor.soc_pct == 43.0

    def test_stale_or_missing_soc_does_not_anchor(self):
        est = SocEstimator()
        _obs(est, 40.0, 0.0, fresh=False)
        _obs(est, None, 0.0)
        assert est.anchor is None
        assert est.last_known_soc_pct is None


class TestEnergyAccumulation:
    def test_first_read_only_baselines(self):
        est = SocEstimator()
        _obs(est, 40.0, 6800.0)
        assert est.accumulated_wh == 0.0

    def test_increments_accumulate(self):
        est = SocEstimator()
        for energy in (100.0, 300.0, 700.0):
            _obs(est, 40.0, energy)
        assert est.accumulated_wh == 600.0

    def test_none_is_skipped_and_same_value_adds_nothing(self):
        est = SocEstimator()
        _obs(est, 40.0, 1000.0)
        _obs(est, None, None)
        _obs(est, 40.0, 1000.0)
        assert est.accumulated_wh == 0.0

    def test_energy_added_across_none_gap(self):
        est = SocEstimator()
        _obs(est, 40.0, 1000.0)
        _obs(est, 40.0, None)
        _obs(est, 40.0, 1400.0)
        assert est.accumulated_wh == 400.0

    def test_negative_jump_is_reset_and_rebaselines(self):
        est = SocEstimator()
        _obs(est, 40.0, 6700.0)
        _obs(est, 40.0, 6800.0)
        _obs(est, 40.0, 0.0)
        assert est.accumulated_wh == 100.0
        _obs(est, 40.0, 200.0)
        assert est.accumulated_wh == 300.0

    def test_large_jump_is_discontinuity(self):
        est = SocEstimator()
        _obs(est, 40.0, 0.0)
        _obs(est, 40.0, 6800.0)
        assert est.accumulated_wh == 0.0
        assert est.discontinuities == 1
        _obs(est, 40.0, 6900.0)
        assert est.accumulated_wh == 100.0

    def test_anchor_energy_survives_register_reset(self):
        est = SocEstimator()
        _obs(est, 40.0, 5000.0)
        _obs(est, 40.0, 5400.0)
        _obs(est, 40.0, 0.0)
        _obs(est, 40.0, 410.0)
        assert est.estimated_pct(CAP, 1.0) == pytest.approx(40.0 + 810.0 / 820.0)


class TestStall:
    def _est(self, delivered_wh: float) -> SocEstimator:
        est = SocEstimator()
        _obs(est, 40.0, 0.0)
        _deliver(est, 0.0, delivered_wh)
        return est

    def test_not_stalled_just_below_tolerance(self):
        assert not _resolve(self._est(1229.0), 40.0).stalled

    def test_stalled_just_above_tolerance(self):
        res = _resolve(self._est(1231.0), 40.0)
        assert res.stalled
        assert res.source is SocSource.ESTIMATED
        assert res.effective_pct == pytest.approx(40.0 + 1231.0 / 820.0)

    def test_tolerance_scales_with_k(self):
        est = self._est(1300.0)
        assert _resolve(est, 40.0, k=1.0).stalled
        assert not _resolve(est, 40.0, k=1.1).stalled

    def test_clears_on_next_soc_change(self):
        est = self._est(2000.0)
        assert _resolve(est, 40.0).stalled
        _obs(est, 42.5, 2000.0)
        res = _resolve(est, 42.5)
        assert not res.stalled
        assert res.source is SocSource.REPORTED

    def test_no_energy_flow_is_not_stalled(self):
        est = self._est(0.0)
        for _ in range(100):
            _obs(est, 40.0, 0.0)
        assert not _resolve(est, 40.0).stalled


class TestResolve:
    def test_healthy_is_reported(self):
        est = SocEstimator()
        _obs(est, 40.0, 0.0)
        _obs(est, 40.0, 300.0)
        res = _resolve(est, 40.0)
        assert (res.source, res.effective_pct, res.stale) == (SocSource.REPORTED, 40.0, False)

    def test_stale_with_anchor_is_estimated_but_not_stalled(self):
        est = SocEstimator()
        _obs(est, 40.0, 0.0)
        _obs(est, 40.0, 820.0)
        res = _resolve(est, 40.0, fresh=False)
        assert res.source is SocSource.ESTIMATED
        assert res.effective_pct == pytest.approx(41.0)
        assert res.reported_pct is None
        assert res.stale
        assert not res.stalled

    def test_stale_without_anchor_is_none(self):
        res = _resolve(SocEstimator(), 40.0, fresh=False)
        assert (res.source, res.effective_pct, res.estimated_pct) == (SocSource.NONE, None, None)

    def test_fresh_without_anchor_is_reported(self):
        res = _resolve(SocEstimator(), 40.0)
        assert (res.source, res.effective_pct) == (SocSource.REPORTED, 40.0)


class TestSessions:
    def test_reconnect_uses_last_known_anchor(self):
        est = SocEstimator()
        _obs(est, 48.5, 6800.0)
        est.end_session()
        assert est.anchor is None
        assert est.last_known_soc_pct == 48.5
        est.begin_session()
        assert est.anchor is not None
        assert est.anchor.origin is AnchorOrigin.LAST_KNOWN
        _obs(est, None, 6800.0, fresh=False)
        _obs(est, None, 7620.0, fresh=False)
        assert est.estimated_pct(CAP, 1.0) == pytest.approx(49.5)

    def test_last_known_anchor_kept_when_same_value_republished(self):
        est = SocEstimator()
        _obs(est, 48.5, 0.0)
        est.begin_session()
        _obs(est, 48.5, 0.0)
        assert est.anchor is not None
        assert est.anchor.origin is AnchorOrigin.LAST_KNOWN

    def test_new_report_replaces_last_known_anchor(self):
        est = SocEstimator()
        _obs(est, 48.5, 0.0)
        est.begin_session()
        _obs(est, 43.0, 0.0)
        assert est.anchor is not None
        assert (est.anchor.soc_pct, est.anchor.origin) == (43.0, AnchorOrigin.REPORTED)

    def test_begin_session_without_last_known_has_no_anchor(self):
        est = SocEstimator()
        est.begin_session()
        assert est.anchor is None
        assert _resolve(est, None, fresh=False).source is SocSource.NONE


class TestEffectiveK:
    def test_default_without_learned_k(self):
        assert effective_k(AppState()) == 1.0

    def test_uses_k_for_matching_capacity(self):
        assert effective_k(AppState(ev_soc_k=1.04, ev_soc_k_capacity_kwh=82.0)) == 1.04

    def test_ignores_k_for_other_capacity(self):
        assert effective_k(AppState(ev_soc_k=1.04, ev_soc_k_capacity_kwh=60.0)) == 1.0


def _loop(
    *,
    limit: float,
    reported: float | None,
    age_s: float = 0.0,
    anchor: tuple[float, float] | None = None,
    delivered_wh: float = 0.0,
):
    state = AppState(ev_max_soc_pct=limit, ev_soc_pct=reported)
    state.ev_soc_pct_updated_at = _time.monotonic() - age_s
    loop = make_ns_loop(state)
    if anchor is not None:
        loop._soc_estimator.observe(soc_pct=anchor[0], soc_fresh=True, energy_wh=anchor[1])
        _deliver(loop._soc_estimator, anchor[1], delivered_wh)
    return loop


class TestMaxSocStopReason:
    def test_reported_reached(self):
        assert max_soc_stop_reason(_loop(limit=80.0, reported=80.0)) == "max_soc_reached"

    def test_reported_below(self):
        assert max_soc_stop_reason(_loop(limit=80.0, reported=79.9)) is None

    def test_reported_margin_at_100_limit(self):
        assert max_soc_stop_reason(_loop(limit=100.0, reported=99.5)) == "max_soc_reached"
        assert max_soc_stop_reason(_loop(limit=100.0, reported=99.4)) is None

    def test_no_data_returns_none(self):
        assert max_soc_stop_reason(_loop(limit=80.0, reported=None)) is None

    def test_frozen_report_triggers_estimated_at_limit_minus_margin(self):
        # Report frozen at 70, 70 -> 79.5 needs 9.5 % x 820 Wh.
        below = _loop(limit=80.0, reported=70.0, anchor=(70.0, 0.0), delivered_wh=7780.0)
        at = _loop(limit=80.0, reported=70.0, anchor=(70.0, 0.0), delivered_wh=7790.0)
        assert max_soc_stop_reason(below) is None
        assert max_soc_stop_reason(at) == "max_soc_estimated"

    def test_stale_report_uses_estimate_with_margin(self):
        loop = _loop(limit=80.0, reported=70.0, age_s=10_000.0, anchor=(70.0, 0.0), delivered_wh=7800.0)
        assert max_soc_stop_reason(loop) == "max_soc_estimated"

    def test_healthy_estimate_has_no_margin(self):
        # Within stall tolerance: estimate 79.7 ahead of report 79.0, source REPORTED, margin 0.
        loop = _loop(limit=80.0, reported=79.0, anchor=(79.0, 0.0), delivered_wh=574.0)
        assert max_soc_stop_reason(loop) is None
        loop = _loop(limit=80.0, reported=79.0, anchor=(79.0, 0.0), delivered_wh=830.0)
        assert max_soc_stop_reason(loop) == "max_soc_estimated"

    def test_limit_100_uses_clamped_estimate(self):
        loop = _loop(limit=100.0, reported=90.0, age_s=10_000.0, anchor=(90.0, 0.0), delivered_wh=8200.0)
        assert max_soc_stop_reason(loop) == "max_soc_estimated"

    def test_no_ceiling_below_configured_limit(self):
        loop = _loop(limit=100.0, reported=90.0, age_s=10_000.0, anchor=(90.0, 0.0), delivered_wh=7000.0)
        assert max_soc_stop_reason(loop) is None


def _step(est: SocEstimator, soc: float, energy: float, k: float = 1.0, *, fresh: bool = True):
    return est.observe(soc_pct=soc, soc_fresh=fresh, energy_wh=energy, capacity_kwh=CAP, k=k)


def _climb(est: SocEstimator, soc0: float, steps: int, wh_per_step: float = 412.5, k: float = 1.0):
    """Report +0.5 pp per step with wh_per_step delivered each step; return the list of results."""
    results = []
    for i in range(steps + 1):
        results.append(_step(est, soc0 + 0.5 * i, wh_per_step * i, k))
    return results


class TestKRefinement:
    def test_k_span_value_and_single_ema_step(self):
        est = SocEstimator()
        results = _climb(est, 40.0, 16, wh_per_step=412.5)  # 6600 Wh over 8.0 pp
        learned = [r for r in results if r is not None]
        assert learned
        last = learned[-1]
        assert last.k_span == pytest.approx(6.6 / 8.0 * 100 / 82.0, abs=1e-4)
        assert last.k_span == pytest.approx(1.006, abs=1e-3)
        assert last.k_new == pytest.approx(1.0 + 0.1 * (last.k_span - 1.0))
        assert last.k_new == pytest.approx(1.0006, abs=1e-4)
        assert est.last_k_span == pytest.approx(last.k_span)

    def test_no_result_below_min_span_pp(self):
        est = SocEstimator()
        results = _climb(est, 40.0, 7, wh_per_step=600.0)  # 3.5 pp, 4.2 kWh
        assert all(r is None for r in results)

    def test_no_result_below_min_energy(self):
        est = SocEstimator()
        results = _climb(est, 40.0, 10, wh_per_step=300.0)  # 5 pp, 3.0 kWh
        assert all(r is None for r in results)

    def test_no_result_when_stale(self):
        est = SocEstimator()
        _climb(est, 40.0, 4)
        _step(est, 42.0, 1650.0, fresh=False)  # stale report discards the span
        results = [_step(est, 42.5 + 0.5 * i, 1650.0 + 412.5 * (i + 1)) for i in range(6)]
        assert all(r is None for r in results)

    def test_no_result_after_energy_gap(self):
        est = SocEstimator()
        _climb(est, 40.0, 4)
        est.observe(soc_pct=42.0, soc_fresh=True, energy_wh=None, capacity_kwh=CAP, k=1.0)
        results = [_step(est, 42.5 + 0.5 * i, 1650.0 + 412.5 * (i + 1)) for i in range(6)]
        assert all(r is None for r in results)

    def test_span_restarts_after_gap_then_refines(self):
        est = SocEstimator()
        _climb(est, 40.0, 4)
        est.observe(soc_pct=42.0, soc_fresh=True, energy_wh=None, capacity_kwh=CAP, k=1.0)
        base = 1650.0
        results = [_step(est, 42.5 + 0.5 * i, base + 412.5 * (i + 1)) for i in range(20)]
        assert any(r is not None for r in results)

    def test_no_result_after_discontinuity(self):
        est = SocEstimator()
        _climb(est, 40.0, 4)
        _step(est, 42.5, 1650.0 + 5000.0)  # energy jump above the step limit
        results = [_step(est, 43.0 + 0.5 * i, 6650.0 + 412.5 * (i + 1)) for i in range(6)]
        assert all(r is None for r in results[:6])

    def test_no_result_after_register_reset(self):
        est = SocEstimator()
        _climb(est, 40.0, 4)
        _step(est, 42.5, 0.0)  # register reset
        results = [_step(est, 43.0 + 0.5 * i, 412.5 * (i + 1)) for i in range(6)]
        assert all(r is None for r in results)

    def test_soc_decrease_restarts_span(self):
        est = SocEstimator()
        _climb(est, 40.0, 4)
        _step(est, 41.0, 1700.0)  # decrease
        results = [_step(est, 41.5 + 0.5 * i, 1700.0 + 412.5 * (i + 1)) for i in range(6)]
        assert all(r is None for r in results)

    def test_stall_discards_span(self):
        est = SocEstimator()
        _climb(est, 40.0, 4)
        # Report frozen at 42 while 3 kWh flows (3.7 pp > tolerance), then it catches up.
        for wh in (2650.0, 3650.0, 4650.0):
            _step(est, 42.0, wh)
        result = _step(est, 46.0, 4650.0)
        assert result is None

    def test_capacity_change_discards_span(self):
        est = SocEstimator()
        _climb(est, 40.0, 4)
        results = []
        for i in range(8):
            results.append(
                est.observe(
                    soc_pct=42.5 + 0.5 * i, soc_fresh=True, energy_wh=1650.0 + 412.5 * (i + 1), capacity_kwh=60.0, k=1.0
                )
            )
        assert all(r is None for r in results)

    def test_begin_and_end_session_discard_span(self):
        for edge in ("begin_session", "end_session"):
            est = SocEstimator()
            _climb(est, 40.0, 4)
            getattr(est, edge)()
            results = [_step(est, 42.5 + 0.5 * i, 1650.0 + 412.5 * (i + 1)) for i in range(6)]
            assert all(r is None for r in results)

    def test_out_of_range_k_span_ignored(self):
        est = SocEstimator()
        results = _climb(est, 40.0, 16, wh_per_step=600.0)  # k_span ~ 1.46
        assert all(r is None for r in results)
        assert est.last_k_span > 1.2

    def test_ema_step_clamped_to_range(self):
        est = SocEstimator()
        results = _climb(est, 40.0, 16, wh_per_step=412.5, k=1.2)
        learned = [r for r in results if r is not None]
        assert learned
        assert all(0.8 <= r.k_new <= 1.2 for r in learned)
