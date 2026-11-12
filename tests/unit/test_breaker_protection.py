"""Unit tests for closed-loop, import-power based main-breaker protection (app/control/breaker.py)."""

from __future__ import annotations

import logging
from datetime import datetime

import pytest

from app.control import breaker, time_utils
from app.control.breaker import (
    limit_phase_current,
    mean_phase_import_w,
    measured_ev_phase_powers_w,
    phase_headroom_pct,
    record_phase_samples,
)
from app.control.constants import (
    _BREAKER_CAP_RELEASE_STEP_W,
    _BREAKER_STOP_CYCLES,
    _MAX_CHARGE_W,
    _MIN_CHARGE_W,
    _PHASE_IMPORT_MEAN_WINDOW_S,
)
from app.control.mode_strategies import resolve_mode_handler
from app.log_throttle import LogThrottle
from tests.unit.breaker_helpers import P_LIM, V, make_state, run_cycle, set_inputs
from tests.unit.helpers import make_ns_loop

CYCLE_S = 10.0


@pytest.fixture(autouse=True)
def _fresh_throttle(monkeypatch, caplog):
    """Each test gets an unthrottled logger so warnings can be asserted."""
    monkeypatch.setattr(breaker, "_throttle", LogThrottle(breaker.logger, suppress_seconds=60.0))
    caplog.set_level(logging.INFO, logger=breaker.logger.name)


def _loop(**state_overrides):
    state = make_state(**state_overrides)
    return make_ns_loop(state), state


def _pinned_mode_loop(monkeypatch, mode, night, expected_handler):
    """Loop whose mode resolution is pinned (clock fixed inside/outside the discharge window)."""
    pinned = datetime(2026, 1, 15, 2, 0, 0) if night else datetime(2026, 1, 15, 12, 0, 0)  # noqa: DTZ001

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return pinned

    monkeypatch.setattr(time_utils, "datetime", _FixedDatetime)
    loop, state = _loop(
        charge_mode=mode,
        ev_connected=True,
        ev_comm_healthy=True,
        ev_soc_pct=50.0,
        ev_max_soc_pct=80.0,
        solar_battery_discharge_start="23:00",
        solar_battery_discharge_end="06:00",
    )
    # Assert the intended mode path is the one actually resolved for this case.
    assert resolve_mode_handler(loop).name == expected_handler
    return loop, state


def _warmup(loop, state, t0=0.0, cycles=4, setpoint=11000.0, p=(1500.0, -1000.0, -1000.0), ev_w=(1467.0, 0.0, 0.0)):
    """Feed benign cycles so the 30 s window is full. Returns the next timestamp."""
    set_inputs(state, p, ev_w=ev_w)
    t = t0
    for _ in range(cycles):
        assert run_cycle(loop, setpoint, t) == setpoint
        t += CYCLE_S
    return t


# ---------------------------------------------------------------------------
# Sampling and means
# ---------------------------------------------------------------------------
class TestSamplingAndMeans:
    def test_import_is_clamped_per_sample_not_on_the_mean(self):
        loop, state = _loop()
        set_inputs(state, (1000.0, 0.0, 0.0))
        record_phase_samples(loop, now=0.0)
        set_inputs(state, (-3000.0, 0.0, 0.0), i=(13.0, 0.0, 0.0))
        record_phase_samples(loop, now=10.0)
        # Export (-3000) must not offset import: mean of (1000, 0), not clamp(-1000)
        assert mean_phase_import_w(loop, 1, now=10.0) == 500.0

    def test_window_prunes_old_samples(self):
        loop, state = _loop()
        set_inputs(state, (1000.0, 0.0, 0.0))
        record_phase_samples(loop, now=0.0)
        set_inputs(state, (2000.0, 0.0, 0.0))
        record_phase_samples(loop, now=_PHASE_IMPORT_MEAN_WINDOW_S + 1)
        assert mean_phase_import_w(loop, 1) == 2000.0

    def test_no_samples_returns_none(self):
        loop, _ = _loop()
        assert mean_phase_import_w(loop, 1) is None
        assert mean_phase_import_w(loop, 4) is None
        assert phase_headroom_pct(loop, 1) is None

    def test_headroom_from_import_power(self):
        loop, state = _loop()
        set_inputs(state, (P_LIM / 2, -500.0, -500.0))
        record_phase_samples(loop, now=0.0)
        assert phase_headroom_pct(loop, 1, now=0.0) == pytest.approx(50.0)
        assert phase_headroom_pct(loop, 2, now=0.0) == 100.0  # export -> full headroom

    def test_missing_input_records_nothing_for_that_phase(self):
        loop, state = _loop()
        set_inputs(state, (None, -500.0, -500.0))
        record_phase_samples(loop, now=0.0)
        assert mean_phase_import_w(loop, 1) is None
        assert mean_phase_import_w(loop, 2) == 0.0

    def test_measured_ev_power_uses_raw_current(self):
        state = make_state(ev_current_raw_a=10.0, ev_current_raw_b=0.0, ev_current_raw_c=0.0, correction_pct=9.0)
        assert measured_ev_phase_powers_w(state) == (10.0 * V, 0.0, 0.0)
        state.ev_current_raw_b = None
        assert measured_ev_phase_powers_w(state) is None

    def test_voltage_falls_back_to_p_over_i_then_230(self):
        loop, state = _loop(victron_l1_voltage_v=None, victron_l2_voltage_v=None)
        set_inputs(state, (2300.0, 10.0, -5.0), i=(10.0, 0.5, 0.5))
        record_phase_samples(loop, now=0.0)
        # L1 P/I = 230 V -> limit uses 230 V; L2 has I<1 so falls back to 230 V
        assert phase_headroom_pct(loop, 1, now=0.0) == pytest.approx((1 - 2300.0 / (0.8 * 32 * 230.0)) * 100)


# ---------------------------------------------------------------------------
# Basic limiter behaviour
# ---------------------------------------------------------------------------
class TestLimiterBasics:
    def test_no_cap_below_threshold(self):
        loop, state = _loop()
        t = _warmup(loop, state)
        set_inputs(state, (3000.0, -1000.0, -1000.0), ev_w=(2000.0, 0.0, 0.0))
        assert run_cycle(loop, 11000.0, t) == 11000.0
        assert loop._breaker.cap_w is None

    def test_zero_setpoint_returns_zero(self):
        loop, state = _loop()
        _warmup(loop, state)
        assert run_cycle(loop, 0.0, 50.0) == 0.0

    def test_standby_is_untouched_and_resets_state(self):
        loop, state = _loop(charge_mode="Standby")
        loop._breaker.cap_w = 5000.0
        loop._breaker.stopped = True
        set_inputs(state, (None, None, None))
        assert limit_phase_current(loop, 9000.0, now=0.0) == 9000.0
        assert loop._breaker.cap_w is None
        assert loop._breaker.stopped is False

    def test_physical_clamp(self):
        loop, state = _loop()
        _warmup(loop, state)
        assert run_cycle(loop, 30000.0, 50.0) == _MAX_CHARGE_W


# ---------------------------------------------------------------------------
# Single-phase car near the limit: trim, hold, gradual release
# ---------------------------------------------------------------------------
class TestSinglePhaseTrimAndRelease:
    @staticmethod
    def _plant(loop, state, setpoint, base_w, t, applied_prev):
        """EV L1 power = applied/3 (setpoint is a TOTAL over 3 phases, car uses one)."""
        p_ev = min(applied_prev / 3.0, 7360.0)
        set_inputs(state, (base_w + p_ev, -1000.0, -1000.0), ev_w=(p_ev, 0.0, 0.0))
        return run_cycle(loop, setpoint, t)

    def test_trim_maps_x3_on_measured_ev_power_and_settles_below_limit(self):
        loop, state = _loop()
        base = 3000.0
        applied = 4400.0
        t = 0.0
        for _ in range(5):
            applied = self._plant(loop, state, 4400.0, base, t, applied)
            t += CYCLE_S
        history = []
        for _ in range(20):
            applied = self._plant(loop, state, 11000.0, base, t, applied)
            history.append(applied)
            t += CYCLE_S
        expected_cap = 3 * (P_LIM - base)
        assert loop._breaker.cap_w == pytest.approx(expected_cap, abs=1.0)
        assert history[-1] == pytest.approx(expected_cap, abs=1.0)
        # Steady-state import ~= limit, never frozen far below it
        steady_import = base + history[-1] / 3.0
        assert P_LIM - 100 <= steady_import <= P_LIM + 100
        # No oscillation: once capped, the applied setpoint never moves back up
        capped = [h for h in history if h < 11000.0]
        assert capped == sorted(capped, reverse=True)

    def test_release_holds_in_dead_band_then_steps_up_gradually(self):
        loop, state = _loop()
        base = 3000.0
        applied = 4400.0
        t = 0.0
        for _ in range(5):
            applied = self._plant(loop, state, 4400.0, base, t, applied)
            t += CYCLE_S
        for _ in range(10):
            applied = self._plant(loop, state, 11000.0, base, t, applied)
            t += CYCLE_S
        cap0 = loop._breaker.cap_w
        assert cap0 is not None
        # House load drops: plenty of margin once the 30 s window has flushed
        base = 1500.0
        caps = []
        for _ in range(30):
            applied = self._plant(loop, state, 11000.0, base, t, applied)
            caps.append(loop._breaker.cap_w)
            t += CYCLE_S
        # First cycle after the drop: mean still inside the dead band -> cap held unchanged
        assert caps[0] == cap0
        deltas = []
        prev = cap0
        for cap in caps:
            if cap is None:
                break
            deltas.append(cap - prev)
            prev = cap
        rising = [d for d in deltas if d != 0]
        assert rising, "cap never released"
        assert all(d == pytest.approx(_BREAKER_CAP_RELEASE_STEP_W) for d in rising)
        assert caps[-1] is None  # cleared once cap >= setpoint
        assert applied == 11000.0


# ---------------------------------------------------------------------------
# Fast trip and stop latch (solar drop-off)
# ---------------------------------------------------------------------------
class TestFastTripAndStop:
    def _drop_off(self, loop, state, t):
        # Raw L1 import jumps to 7.0 kW (> 90 % of 6.7 kW) while EV draws 2 kW
        set_inputs(state, (7000.0, -1000.0, -1000.0), ev_w=(2000.0, 0.0, 0.0))
        return run_cycle(loop, 11000.0, t)

    def test_immediate_minimum_then_stop_after_n_cycles(self, caplog):
        loop, state = _loop()
        t = _warmup(loop, state, ev_w=(2000.0, 0.0, 0.0))
        results = []
        for _ in range(_BREAKER_STOP_CYCLES):
            results.append(self._drop_off(loop, state, t))
            t += CYCLE_S
        assert results[0] == _MIN_CHARGE_W  # immediate, no mean lag
        assert results[1] == _MIN_CHARGE_W
        assert results[-1] == 0.0
        assert loop._breaker.stopped is True
        assert any(r.levelno == logging.WARNING and "fast trip" in r.message for r in caplog.records)

    def test_recovers_from_fast_trip_without_stop_when_import_falls(self):
        loop, state = _loop()
        t = _warmup(loop, state, ev_w=(2000.0, 0.0, 0.0))
        assert self._drop_off(loop, state, t) == _MIN_CHARGE_W
        t += CYCLE_S
        set_inputs(state, (1500.0, -1000.0, -1000.0), ev_w=(1467.0, 0.0, 0.0))
        out = [run_cycle(loop, 11000.0, t + k * CYCLE_S) for k in range(6)]
        assert loop._breaker.stopped is False
        assert out[-1] > _MIN_CHARGE_W  # stepping back up
        assert all(b >= a for a, b in zip(out, out[1:], strict=False))

    def test_stop_latch_releases_only_with_margin_even_at_zero_setpoint(self):
        loop, state = _loop()
        t = _warmup(loop, state, ev_w=(2000.0, 0.0, 0.0))
        for _ in range(_BREAKER_STOP_CYCLES):
            self._drop_off(loop, state, t)
            t += CYCLE_S
        assert loop._breaker.stopped
        # Still heavily loaded: stays stopped
        set_inputs(state, (5000.0, -1000.0, -1000.0))
        assert run_cycle(loop, 11000.0, t) == 0.0
        t += CYCLE_S
        # House load gone, EV off. Setpoint is 0 (e.g. mode stopped) but the latch must still release.
        set_inputs(state, (1000.0, -1000.0, -1000.0))
        for _ in range(6):
            run_cycle(loop, 0.0, t)
            t += CYCLE_S
        assert loop._breaker.stopped is False
        assert loop._breaker.cap_w == _MIN_CHARGE_W

    def test_restart_after_release_starts_at_minimum(self):
        loop, state = _loop()
        loop._breaker.stopped = True
        set_inputs(state, (500.0, -1000.0, -1000.0))
        out = [run_cycle(loop, 11000.0, k * CYCLE_S) for k in range(3)]
        assert out[-1] == _MIN_CHARGE_W


# ---------------------------------------------------------------------------
# Export, non-EV phases, three-phase cars
# ---------------------------------------------------------------------------
class TestExportAndPhases:
    def test_export_only_with_large_positive_currents_never_caps(self):
        loop, state = _loop()
        # Meter currents are unsigned: large positive values while exporting must be ignored
        set_inputs(state, (-6000.0, -6000.0, -6000.0), i=(26.0, 26.0, 26.0), ev_w=(3000.0, 0.0, 0.0))
        for k in range(10):
            assert run_cycle(loop, 11000.0, k * CYCLE_S) == 11000.0
        assert loop._breaker.cap_w is None
        assert phase_headroom_pct(loop, 1, now=90.0) == 100.0

    def test_overload_on_non_ev_phase_warns_but_does_not_cap(self, caplog):
        loop, state = _loop()
        set_inputs(state, (1000.0, 7000.0, -500.0), ev_w=(1467.0, 0.0, 0.0))
        for k in range(6):
            assert run_cycle(loop, 11000.0, k * CYCLE_S) == 11000.0
        assert loop._breaker.cap_w is None
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("L2" in r.message and "cannot be relieved" in r.message for r in warnings)

    def test_non_ev_overload_warning_clears_when_resolved(self, caplog):
        loop, state = _loop()
        set_inputs(state, (1000.0, 7000.0, -500.0), ev_w=(1467.0, 0.0, 0.0))
        run_cycle(loop, 11000.0, 0.0)
        set_inputs(state, (1000.0, 1000.0, -500.0), ev_w=(1467.0, 0.0, 0.0))
        for k in range(1, 6):
            run_cycle(loop, 11000.0, k * CYCLE_S)
        assert any("condition cleared" in r.message for r in caplog.records)

    def test_three_phase_even_draw_uses_same_formula(self):
        loop, state = _loop()
        ev = (4000.0, 4000.0, 4000.0)
        set_inputs(state, (500.0, -500.0, -500.0), ev_w=(1467.0,) * 3)
        t = 0.0
        for _ in range(4):
            run_cycle(loop, 4400.0, t)
            t += CYCLE_S
        # L1 base 2500 + 4000 = 6500 (over 80 %, below 90 %)
        set_inputs(state, (2500.0 + 4000.0, -500.0, -500.0), ev_w=ev)
        out = 0.0
        for _ in range(5):
            out = run_cycle(loop, 12000.0, t)
            t += CYCLE_S
        assert out == pytest.approx(3 * (P_LIM - 2500.0), abs=1.0)
        assert loop._breaker.last_binding_phase == 1

    def test_most_restrictive_phase_wins_when_two_phases_over(self):
        loop, state = _loop()
        ev = (4000.0, 4000.0, 4000.0)
        set_inputs(state, (1000.0, 1000.0, 1000.0), ev_w=(1467.0,) * 3)
        t = 0.0
        for _ in range(4):
            run_cycle(loop, 4400.0, t)
            t += CYCLE_S
        # L1 base 2500, L2 base 2300 -> L1 binds
        set_inputs(state, (6500.0, 6300.0, 1000.0), ev_w=ev)
        out = 0.0
        for _ in range(5):
            out = run_cycle(loop, 12000.0, t)
            t += CYCLE_S
        assert out == pytest.approx(3 * (P_LIM - 2500.0), abs=1.0)
        assert out < 3 * (P_LIM - 2300.0)
        assert loop._breaker.last_binding_phase == 1


# ---------------------------------------------------------------------------
# Missing / implausible data: fail-safe in ALL modes
# ---------------------------------------------------------------------------
class TestFailSafe:
    @pytest.mark.parametrize(
        ("mode", "night", "handler"),
        [("Manual", False, "manual"), ("Eco", False, "eco_day"), ("Eco", True, "eco_night")],
        ids=["manual", "eco-day", "eco-night"],
    )
    def test_missing_power_for_n_cycles_drops_to_minimum(self, mode, night, handler, caplog, monkeypatch):
        loop, state = _pinned_mode_loop(monkeypatch, mode, night, handler)
        t = _warmup(loop, state)
        set_inputs(state, (None, -1000.0, -1000.0), ev_w=(1467.0, 0.0, 0.0))
        out = []
        for _ in range(3):
            out.append(run_cycle(loop, 11000.0, t))
            t += CYCLE_S
        # Debounce holds at the last applied value (11000 W), then drops to the minimum on the 3rd cycle.
        assert out == [11000.0, 11000.0, _MIN_CHARGE_W]
        assert any(r.levelno == logging.WARNING and "missing" in r.message for r in caplog.records)

    @pytest.mark.parametrize(
        ("mode", "night", "handler"),
        [("Manual", False, "manual"), ("Eco", False, "eco_day"), ("Eco", True, "eco_night")],
        ids=["manual", "eco-day", "eco-night"],
    )
    def test_debounce_holds_last_applied_and_never_ramps_up(self, mode, night, handler, monkeypatch):
        loop, state = _pinned_mode_loop(monkeypatch, mode, night, handler)
        t = _warmup(loop, state, setpoint=6000.0)
        set_inputs(state, (None, -1000.0, -1000.0), ev_w=(1467.0, 0.0, 0.0))
        assert run_cycle(loop, 11000.0, t) == 6000.0  # (a) higher request held at last applied
        assert run_cycle(loop, 5000.0, t + CYCLE_S) == 5000.0  # (b) lower request passes through
        assert run_cycle(loop, 11000.0, t + 2 * CYCLE_S) == _MIN_CHARGE_W  # (d) 3rd cycle: minimum

    def test_debounce_without_prior_applied_value_uses_minimum(self):
        loop, state = _loop()
        set_inputs(state, (None, -1000.0, -1000.0), ev_w=(1467.0, 0.0, 0.0))
        assert loop._breaker.last_applied_w is None
        assert run_cycle(loop, 11000.0, 0.0) == _MIN_CHARGE_W  # (c)
        assert run_cycle(loop, 11000.0, CYCLE_S) == _MIN_CHARGE_W

    def test_valid_data_resumes_normal_pass_through_after_debounce(self):
        loop, state = _loop()
        t = _warmup(loop, state, setpoint=6000.0)
        set_inputs(state, (None, -1000.0, -1000.0), ev_w=(1467.0, 0.0, 0.0))
        assert run_cycle(loop, 11000.0, t) == 6000.0
        set_inputs(state, (1500.0, -1000.0, -1000.0), ev_w=(1467.0, 0.0, 0.0))
        assert run_cycle(loop, 11000.0, t + CYCLE_S) == 11000.0  # (e) no cap active: straight pass-through
        assert loop._breaker.missing_cycles == 0

    def test_debounce_respects_active_cap(self):
        loop, state = _loop()
        t = _warmup(loop, state)
        loop._breaker.cap_w = 5000.0
        loop._breaker.last_applied_w = 8000.0
        set_inputs(state, (None, -1000.0, -1000.0), ev_w=(1467.0, 0.0, 0.0))
        assert run_cycle(loop, 11000.0, t) == 5000.0

    def test_recovery_is_gradual(self):
        loop, state = _loop()
        t = _warmup(loop, state)
        set_inputs(state, (None, -1000.0, -1000.0), ev_w=(1467.0, 0.0, 0.0))
        for _ in range(3):
            run_cycle(loop, 11000.0, t)
            t += CYCLE_S
        set_inputs(state, (1500.0, -1000.0, -1000.0), ev_w=(1467.0, 0.0, 0.0))
        first = run_cycle(loop, 11000.0, t)
        second = run_cycle(loop, 11000.0, t + CYCLE_S)
        assert loop._breaker.missing_cycles == 0
        assert _MIN_CHARGE_W <= first < second < 11000.0

    def test_missing_ev_current_is_also_failsafe(self):
        loop, state = _loop()
        t = _warmup(loop, state)
        set_inputs(state, (1500.0, -1000.0, -1000.0))
        state.ev_current_raw_a = None
        out = [run_cycle(loop, 11000.0, t + k * CYCLE_S) for k in range(3)]
        assert out[-1] == _MIN_CHARGE_W

    def test_implausible_power_counts_as_missing(self, caplog):
        loop, state = _loop()
        t = _warmup(loop, state)
        # +5 kW import with only 1 A: physically impossible
        set_inputs(state, (5000.0, -1000.0, -1000.0), i=(1.0, 4.3, 4.3), ev_w=(1467.0, 0.0, 0.0))
        out = [run_cycle(loop, 11000.0, t + k * CYCLE_S) for k in range(3)]
        assert out[-1] == _MIN_CHARGE_W
        assert any("implausible" in r.message for r in caplog.records)

    def test_stop_latch_stays_stopped_while_data_missing(self):
        loop, state = _loop()
        loop._breaker.stopped = True
        set_inputs(state, (None, None, None))
        for k in range(5):
            assert run_cycle(loop, 11000.0, k * CYCLE_S) == 0.0
        assert loop._breaker.stopped

    def test_no_failsafe_when_not_charging(self):
        loop, state = _loop()
        set_inputs(state, (None, None, None))
        for k in range(5):
            assert run_cycle(loop, 0.0, k * CYCLE_S) == 0.0


# ---------------------------------------------------------------------------
# Current guard (power factor < 1)
# ---------------------------------------------------------------------------
class TestCurrentGuard:
    def test_importing_current_over_80pct_trims_even_if_power_is_under_limit(self):
        loop, state = _loop()
        # 4.5 kW is below the 5.96 kW power limit, but 27 A importing is above 25.6 A (PF ~0.72)
        set_inputs(state, (500.0, -500.0, -500.0), i=(3.0, 3.0, 3.0), ev_w=(1467.0, 0.0, 0.0))
        t = 0.0
        for _ in range(4):
            run_cycle(loop, 4400.0, t)
            t += CYCLE_S
        set_inputs(state, (4500.0, -500.0, -500.0), i=(27.0, 3.0, 3.0), ev_w=(2000.0, 0.0, 0.0))
        out = 11000.0
        for _ in range(5):
            out = run_cycle(loop, 11000.0, t)
            t += CYCLE_S
        excess_w = (27.0 - 0.8 * 32.0) * V
        assert 4500.0 < P_LIM
        assert out == pytest.approx(3 * (2000.0 - excess_w), abs=1.0)

    def test_same_current_while_exporting_does_not_trim(self):
        loop, state = _loop()
        set_inputs(state, (-4500.0, -500.0, -500.0), i=(27.0, 3.0, 3.0), ev_w=(2000.0, 0.0, 0.0))
        for k in range(8):
            assert run_cycle(loop, 11000.0, k * CYCLE_S) == 11000.0
        assert loop._breaker.cap_w is None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------
class TestEdges:
    def test_never_returns_positive_below_minimum_from_a_cap(self):
        loop, state = _loop()
        # Even the minimum cannot fit: cap computes below 4400 but output floors at the minimum
        t = _warmup(loop, state, ev_w=(1467.0, 0.0, 0.0))
        set_inputs(state, (6300.0, -500.0, -500.0), ev_w=(1467.0, 0.0, 0.0))
        out = run_cycle(loop, 11000.0, t)
        assert out == 0.0 or out >= _MIN_CHARGE_W

    def test_first_cycle_without_history_is_not_failsafe(self):
        loop, state = _loop()
        set_inputs(state, (1000.0, -500.0, -500.0), ev_w=(1467.0, 0.0, 0.0))
        assert run_cycle(loop, 11000.0, 0.0) == 11000.0


# ---------------------------------------------------------------------------
# ControlLoop integration (one real run_loop iteration)
# ---------------------------------------------------------------------------
class TestRunLoopIntegration:
    @pytest.mark.asyncio
    async def test_fast_trip_writes_minimum_setpoint_and_publishes_headroom(self):
        import asyncio
        import unittest.mock
        from unittest.mock import AsyncMock, MagicMock

        from app.control.loop import ControlLoop
        from app.state import ChargerStatus

        state = make_state(ev_connected=True, ev_comm_healthy=True, manual_power_w=11000.0)
        state.ev_charger_status_enum = ChargerStatus.CHARGING_IN_PROGRESS
        set_inputs(state, (7000.0, -1000.0, -1000.0), ev_w=(2000.0, 0.0, 0.0))
        victron = MagicMock()
        victron.connected = True
        victron.ensure_connected = AsyncMock()
        victron.read = AsyncMock()
        ev = AsyncMock()
        ev.connected = True
        cl = ControlLoop(state, victron, ev, asyncio.Queue())

        async def _stop(*a, **kw):
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError), unittest.mock.patch("asyncio.sleep", side_effect=_stop):
            await cl.run_loop()

        assert state.commanded_setpoint_w == _MIN_CHARGE_W
        ev.write_setpoint.assert_awaited_with(_MIN_CHARGE_W)
        assert state.l1_breaker_headroom_pct is not None
        assert state.l1_breaker_headroom_pct < 0  # raw import above the 80 % limit
        assert state.l2_breaker_headroom_pct == 100.0
