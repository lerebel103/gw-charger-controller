"""Unit tests for closed-loop, signed import-current based main-breaker protection (app/control/breaker.py)."""

from __future__ import annotations

import logging
from datetime import datetime

import pytest

from app.control import breaker, time_utils
from app.control.breaker import (
    limit_phase_current,
    mean_phase_import_a,
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
from tests.unit.breaker_helpers import I_LIM, P_LIM, V, make_state, run_cycle, set_inputs
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
        set_inputs(state, (-3000.0, 0.0, 0.0))
        record_phase_samples(loop, now=10.0)
        # Export (-3000 W) must not offset import: mean of (1000 W, 0), not clamp(-1000 W)
        assert mean_phase_import_a(loop, 1, now=10.0) == pytest.approx(500.0 / V)

    def test_window_prunes_old_samples(self):
        loop, state = _loop()
        set_inputs(state, (1000.0, 0.0, 0.0))
        record_phase_samples(loop, now=0.0)
        set_inputs(state, (2000.0, 0.0, 0.0))
        record_phase_samples(loop, now=_PHASE_IMPORT_MEAN_WINDOW_S + 1)
        assert mean_phase_import_a(loop, 1) == pytest.approx(2000.0 / V)

    def test_no_samples_returns_none(self):
        loop, _ = _loop()
        assert mean_phase_import_a(loop, 1) is None
        assert mean_phase_import_a(loop, 4) is None
        assert phase_headroom_pct(loop, 1) is None

    def test_headroom_from_import_current(self):
        loop, state = _loop()
        set_inputs(state, (P_LIM / 2, -500.0, -500.0))
        record_phase_samples(loop, now=0.0)
        assert phase_headroom_pct(loop, 1, now=0.0) == pytest.approx(50.0)
        assert phase_headroom_pct(loop, 2, now=0.0) == 100.0  # export -> full headroom

    def test_missing_input_records_nothing_for_that_phase(self):
        loop, state = _loop()
        set_inputs(state, (None, -500.0, -500.0))
        record_phase_samples(loop, now=0.0)
        assert mean_phase_import_a(loop, 1) is None
        assert mean_phase_import_a(loop, 2) == 0.0

    def test_measured_ev_power_uses_raw_current(self):
        state = make_state(ev_current_raw_a=10.0, ev_current_raw_b=0.0, ev_current_raw_c=0.0, correction_pct=9.0)
        assert measured_ev_phase_powers_w(state) == (10.0 * V, 0.0, 0.0)
        state.ev_current_raw_b = None
        assert measured_ev_phase_powers_w(state) is None

    def test_headroom_is_independent_of_voltage(self):
        loop, state = _loop(victron_l1_voltage_v=None)
        set_inputs(state, (None, -5.0, -5.0), i=(I_LIM / 4, -5.0, -5.0))
        record_phase_samples(loop, now=0.0)
        assert phase_headroom_pct(loop, 1, now=0.0) == pytest.approx(75.0)

    def test_cap_uses_230_v_when_grid_voltage_missing(self):
        """Voltage only converts amps to setpoint watts; a missing reading falls back to 230 V."""
        loop, state = _loop(victron_l1_voltage_v=None)
        ev_a = 10.0
        set_inputs(state, (None, None, None), i=(I_LIM + 2.0, -1.0, -1.0), ev_w=(ev_a * V, 0.0, 0.0))
        out = [run_cycle(loop, 11000.0, k * CYCLE_S) for k in range(2)]
        assert out[-1] == pytest.approx(3 * 230.0 * (ev_a - 2.0))


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
        # Signed currents: large NEGATIVE values (export) must never count as load
        set_inputs(state, (-6000.0, -6000.0, -6000.0), i=(-26.0, -26.0, -26.0), ev_w=(3000.0, 0.0, 0.0))
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
# Missing data: fail-safe in ALL modes
# ---------------------------------------------------------------------------
class TestFailSafe:
    @pytest.mark.parametrize(
        ("mode", "night", "handler"),
        [("Manual", False, "manual"), ("Eco", False, "eco_day"), ("Eco", True, "eco_night")],
        ids=["manual", "eco-day", "eco-night"],
    )
    def test_missing_current_for_n_cycles_drops_to_minimum(self, mode, night, handler, caplog, monkeypatch):
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

    def test_inconsistent_values_are_not_missing(self, caplog):
        """Only absent values count as missing: any signed current is used as is, whatever the other registers say."""
        loop, state = _loop()
        t = _warmup(loop, state)
        set_inputs(state, (None, None, None), i=(1.0, -4.3, -4.3), ev_w=(1467.0, 0.0, 0.0))
        out = [run_cycle(loop, 11000.0, t + k * CYCLE_S) for k in range(3)]
        assert out == [11000.0] * 3
        assert loop._breaker.missing_cycles == 0
        assert not any("missing" in r.message or "implausible" in r.message for r in caplog.records)

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
# Current is authoritative (power factor < 1)
# ---------------------------------------------------------------------------
class TestCurrentIsAuthoritative:
    def test_low_power_factor_current_over_80pct_trims(self):
        """Real power is low (about 0.7 kW), but 27 A of signed import current is above 25.6 A: it trims."""
        loop, state = _loop()
        set_inputs(state, (500.0, -500.0, -500.0), ev_w=(1467.0, 0.0, 0.0))
        t = 0.0
        for _ in range(4):
            run_cycle(loop, 4400.0, t)
            t += CYCLE_S
        # Only the current is set: the limiter never looks at power, so low real power cannot hide it
        set_inputs(state, (None, None, None), i=(27.0, -3.0, -3.0), ev_w=(2000.0, 0.0, 0.0))
        out = 11000.0
        for _ in range(5):
            out = run_cycle(loop, 11000.0, t)
            t += CYCLE_S
        ev_a = 2000.0 / V
        assert out == pytest.approx(3 * V * (ev_a - (27.0 - I_LIM)), abs=1.0)
        assert out < 11000.0
        assert loop._breaker.last_binding_phase == 1

    def test_same_current_magnitude_while_exporting_does_not_trim(self):
        loop, state = _loop()
        set_inputs(state, (None, None, None), i=(-27.0, -3.0, -3.0), ev_w=(2000.0, 0.0, 0.0))
        for k in range(8):
            assert run_cycle(loop, 11000.0, k * CYCLE_S) == 11000.0
        assert loop._breaker.cap_w is None


class TestLiveSignedCurrentRegression:
    """Live log 2026-11-13: P about 0 W with signed I = -1.8 A / -1.7 A, L3 flipping between +2.0 and -2.0 A."""

    def test_signed_negative_currents_are_not_missing_and_pass_through(self, caplog):
        loop, state = _loop(victron_l1_voltage_v=239.9, victron_l2_voltage_v=238.1, victron_l3_voltage_v=239.6)
        l2 = (-1.8, -1.8, -1.7, -1.8, -1.8, -1.7)
        l3 = (2.0, 2.0, -2.0, -1.9, 2.0, -2.0)
        for k in range(12):
            set_inputs(state, (None, None, None), i=(2.0, l2[k % 6], l3[k % 6]), ev_w=(1467.0, 0.0, 0.0))
            assert run_cycle(loop, 11000.0, k * CYCLE_S) == 11000.0
            assert loop._breaker.missing_cycles == 0
            assert loop._breaker.hold_ceiling_w is None
        assert loop._breaker.cap_w is None
        assert not any(r.levelno >= logging.WARNING for r in caplog.records)
        assert not any("implausible" in r.message or "missing" in r.message for r in caplog.records)
        assert phase_headroom_pct(loop, 2, now=110.0) == 100.0  # export: full headroom, not None


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


# ---------------------------------------------------------------------------
# PR #25 review follow-ups
# ---------------------------------------------------------------------------
class TestPartialDataProtection:
    """A missing phase must not disable overload protection on the healthy phases."""

    def test_missing_l3_with_l1_over_fast_threshold_trips_then_stops(self):
        loop, state = _loop()
        t = _warmup(loop, state)
        set_inputs(state, (7000.0, -1000.0, None), ev_w=(2000.0, 0.0, 0.0))  # 7000 W > 90 % (6710 W)
        out = []
        for _ in range(_BREAKER_STOP_CYCLES):
            out.append(run_cycle(loop, 11000.0, t))
            t += CYCLE_S
        assert out == [_MIN_CHARGE_W] * (_BREAKER_STOP_CYCLES - 1) + [0.0]
        assert loop._breaker.stopped
        assert run_cycle(loop, 11000.0, t) == 0.0  # stays stopped while data is still missing

    def test_missing_l3_with_l1_over_mean_threshold_trims(self):
        loop, state = _loop()
        loop._breaker.last_applied_w = 11000.0
        set_inputs(state, (6200.0, -1000.0, None), ev_w=(3000.0, 0.0, 0.0))  # over 80 % (5965 W), under 90 %
        out = run_cycle(loop, 11000.0, 0.0)
        excess = 6200.0 - P_LIM
        assert out == pytest.approx(3.0 * (3000.0 - excess))
        assert out < 11000.0

    def test_partial_data_never_releases_a_cap(self):
        loop, state = _loop()
        loop._breaker.last_applied_w = 11000.0
        loop._breaker.cap_w = 6000.0
        set_inputs(state, (500.0, -1000.0, None), ev_w=(1467.0, 0.0, 0.0))
        assert run_cycle(loop, 11000.0, 0.0) == 6000.0
        assert loop._breaker.cap_w == 6000.0


class TestStopCounterUsesEffectiveOutput:
    def test_ev_ignoring_minimum_below_fast_trip_still_latches_stop(self):
        """Phase stays over 80 % mean but under 90 % raw while the output sits at the minimum."""
        loop, state = _loop()
        set_inputs(state, (6300.0, -1000.0, -1000.0), ev_w=(3000.0, 0.0, 0.0))
        out = []
        for k in range(_BREAKER_STOP_CYCLES):
            out.append(run_cycle(loop, _MIN_CHARGE_W, k * CYCLE_S))
        # Calculated cap (~8 kW) exceeds the minimum, yet the effective output is the minimum.
        assert out == [_MIN_CHARGE_W] * (_BREAKER_STOP_CYCLES - 1) + [0.0]
        assert loop._breaker.stopped


class TestHeadroomInvalidInputs:
    def test_failed_read_publishes_none_for_that_phase_only(self):
        loop, state = _loop()
        set_inputs(state, (3000.0, -500.0, -500.0))
        record_phase_samples(loop, now=0.0)
        assert phase_headroom_pct(loop, 1, now=1.0) is not None
        set_inputs(state, (None, -500.0, -500.0))
        assert phase_headroom_pct(loop, 1, now=1.0) is None
        assert phase_headroom_pct(loop, 2, now=1.0) == 100.0

    def test_standby_keeps_grid_only_headroom_without_ev_data(self):
        loop, state = _loop(charge_mode="Standby")
        set_inputs(state, (P_LIM / 2, -500.0, -500.0))
        state.ev_current_raw_a = state.ev_current_raw_b = state.ev_current_raw_c = None
        record_phase_samples(loop, now=0.0, ev_fresh=False)
        assert limit_phase_current(loop, 0.0, now=0.0) == 0.0  # limiter resets its state in Standby
        assert phase_headroom_pct(loop, 1, now=0.0) == pytest.approx(50.0)


class TestMonotonicHold:
    def test_hold_ceiling_tightens_during_invalid_data(self):
        loop, state = _loop()
        t = _warmup(loop, state, setpoint=6000.0)
        set_inputs(state, (None, -1000.0, -1000.0), ev_w=(1467.0, 0.0, 0.0))
        assert run_cycle(loop, 5000.0, t) == 5000.0
        assert run_cycle(loop, 11000.0, t + CYCLE_S) == 5000.0

    def test_hold_ceiling_resets_when_data_returns(self):
        loop, state = _loop()
        t = _warmup(loop, state, setpoint=6000.0)
        set_inputs(state, (None, -1000.0, -1000.0), ev_w=(1467.0, 0.0, 0.0))
        run_cycle(loop, 5000.0, t)
        set_inputs(state, (1500.0, -1000.0, -1000.0), ev_w=(1467.0, 0.0, 0.0))
        assert run_cycle(loop, 11000.0, t + CYCLE_S) == 11000.0
        assert loop._breaker.hold_ceiling_w is None

    def test_zero_output_tightens_the_ceiling_to_zero(self):
        loop, state = _loop()
        t = _warmup(loop, state, setpoint=6000.0)
        set_inputs(state, (None, -1000.0, -1000.0), ev_w=(1467.0, 0.0, 0.0))
        assert run_cycle(loop, 0.0, t) == 0.0
        assert run_cycle(loop, 11000.0, t + CYCLE_S) == 0.0


class TestStandbySampleInvalidation:
    @pytest.mark.asyncio
    async def test_stale_ev_samples_from_standby_do_not_cap_after_exit(self):
        import asyncio
        import unittest.mock
        from unittest.mock import AsyncMock, MagicMock

        from app.control.loop import ControlLoop
        from app.state import ChargerStatus

        state = make_state(ev_connected=True, ev_comm_healthy=True, manual_power_w=11000.0, charge_mode="Standby")
        state.ev_charger_status_enum = ChargerStatus.CHARGING_IN_PROGRESS
        # Stale EV currents (reads are skipped in Standby) paired with heavy grid import.
        set_inputs(state, (9000.0, -1000.0, -1000.0), ev_w=(2000.0, 0.0, 0.0))
        victron = MagicMock()
        victron.connected = True
        victron.ensure_connected = AsyncMock()
        victron.read = AsyncMock()
        ev = AsyncMock()
        ev.connected = True
        cl = ControlLoop(state, victron, ev, asyncio.Queue())
        cl._standby_write_quiet = True
        ticks = {"n": 0}

        async def _sleep(*a, **kw):
            ticks["n"] += 1
            if ticks["n"] == 3:
                # Leave Standby with the grid back to benign.
                state.charge_mode = "Manual"
                set_inputs(state, (1000.0, -1000.0, -1000.0), ev_w=(2000.0, 0.0, 0.0))
            if ticks["n"] == 4:
                raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError), unittest.mock.patch("asyncio.sleep", side_effect=_sleep):
            await cl.run_loop()
        assert state.commanded_setpoint_w == 11000.0
        assert cl._breaker.cap_w is None
        assert not cl._breaker.stopped
        assert all(s.import_a == pytest.approx(1000.0 / V) for s in cl._phase_samples[0])


# ---------------------------------------------------------------------------
# PR #25 round 2
# ---------------------------------------------------------------------------
class TestEvCurrentRange:
    def test_impossible_raw_current_is_missing_not_inflating(self):
        loop, state = _loop()
        t = _warmup(loop, state, setpoint=6000.0)
        set_inputs(state, (1500.0, -1000.0, -1000.0), ev_w=(1467.0, 0.0, 0.0))
        state.ev_current_raw_a = 6553.5  # 0xFFFF register
        assert run_cycle(loop, 11000.0, t) == 6000.0  # hold
        assert run_cycle(loop, 11000.0, t + CYCLE_S) == 6000.0
        assert run_cycle(loop, 11000.0, t + 2 * CYCLE_S) == _MIN_CHARGE_W
        assert loop._breaker.missing_cycles == 3

    def test_negative_raw_current_is_missing(self):
        loop, state = _loop()
        set_inputs(state, (1500.0, -1000.0, -1000.0))
        state.ev_current_raw_b = -1.0
        record_phase_samples(loop, now=0.0)
        assert loop._breaker.phase_fresh == [True, False, True]


class TestConfirmedWriteBaseline:
    def test_failed_increase_does_not_raise_hold_ceiling(self):
        loop, state = _loop()
        t = _warmup(loop, state, setpoint=6000.0)
        # Output rises to 11000 W but the Modbus write fails: no confirmation.
        record_phase_samples(loop, now=t)
        assert limit_phase_current(loop, 11000.0, now=t) == 11000.0
        assert loop._breaker.last_applied_w == 6000.0
        set_inputs(state, (None, -1000.0, -1000.0), ev_w=(1467.0, 0.0, 0.0))
        assert run_cycle(loop, 11000.0, t + CYCLE_S) == 6000.0
        assert loop._breaker.hold_ceiling_w == 6000.0

    @pytest.mark.asyncio
    async def test_loop_confirms_only_after_successful_write(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from app.control.loop import ControlLoop

        state = make_state()
        ev = AsyncMock()
        cl = ControlLoop(state, MagicMock(), ev, asyncio.Queue())
        ev.write_setpoint = AsyncMock(return_value=False)
        await cl._apply_ev_output(9000.0, False)
        assert cl._breaker.last_applied_w is None
        ev.write_setpoint = AsyncMock(return_value=True)
        await cl._apply_ev_output(9000.0, False)
        assert cl._breaker.last_applied_w == 9000.0


class TestLowestCapBinds:
    def test_lowest_cap_phase_binds_not_largest_excess(self, caplog):
        loop, state = _loop()
        set_inputs(state, (None, None, None), i=(27.0, 26.0, 0.0), ev_w=(16.0 * V, 8.0 * V, 0.0))
        out = run_cycle(loop, 11000.0, 0.0)
        assert out == pytest.approx(3 * V * (8.0 - 0.4))  # L2 cap 5312 W, not L1's 10205 W
        assert loop._breaker.last_binding_phase == 2
        assert "binding phase L2" in caplog.text

    def test_fast_trip_logs_lowest_cap_phase_not_highest_raw(self, caplog):
        loop, state = _loop()
        # L1 has the higher raw import but the looser cap; both exceed 90% of the breaker (28.8 A).
        set_inputs(state, (None, None, None), i=(30.0, 29.0, 0.0), ev_w=(16.0 * V, 8.0 * V, 0.0))
        out = run_cycle(loop, 11000.0, 0.0)
        assert out == 4400.0
        assert loop._breaker.last_binding_phase == 2
        assert "Breaker fast trip: L2 " in caplog.text
        assert "Breaker fast trip: L1 " not in caplog.text


class TestGridRecordedIndependentlyOfEv:
    def test_ev_read_failure_keeps_headroom_fresh_and_ev_missing(self):
        loop, state = _loop()
        t = _warmup(loop, state, setpoint=6000.0)
        set_inputs(state, (P_LIM / 2, -1000.0, -1000.0))
        state.ev_current_raw_a = state.ev_current_raw_b = state.ev_current_raw_c = None  # EV read failed
        record_phase_samples(loop, now=t)
        assert loop._breaker.phase_fresh == [False, False, False]
        # Headroom moves towards the fresh grid reading instead of the old buffered value.
        assert phase_headroom_pct(loop, 1, now=t) < 100.0 - 1.0
        assert limit_phase_current(loop, 11000.0, now=t) == 6000.0  # EV data missing -> hold
        assert loop._breaker.missing_cycles == 1


class TestCapUsesPairedSamples:
    """PR #25 round 3: the cap combines grid and EV means over the same (paired) samples only."""

    @staticmethod
    def _grid_only(loop, state, amps, t):
        set_inputs(state, (None, None, None), i=(amps, 0.0, 0.0))
        state.ev_current_raw_a = state.ev_current_raw_b = state.ev_current_raw_c = None  # EV read failed
        record_phase_samples(loop, now=t)

    def test_copilot_scenario_cap_from_paired_samples_is_safe(self):
        loop, state = _loop()
        self._grid_only(loop, state, 24.0, 0.0)
        self._grid_only(loop, state, 24.0, 10.0)
        set_inputs(state, (None, None, None), i=(28.0, 0.0, 0.0), ev_w=(12.0 * V, 0.0, 0.0))
        assert run_cycle(loop, 11000.0, 20.0) == 11000.0  # window mean 25.3 A: below the threshold
        out = run_cycle(loop, 11000.0, 30.0)  # full mean 26 A: over
        assert out == pytest.approx(3 * V * (12.0 - 2.4))  # about 6710 W, not 8108 W
        assert out < 3 * V * (12.0 - 0.4)
        household_a = 16.0
        assert household_a + out / 3 / V <= I_LIM + 1e-9

    def test_overload_without_any_paired_sample_takes_missing_data_path(self):
        loop, state = _loop()
        set_inputs(state, (None, None, None), i=(27.0, 0.0, 0.0), ev_w=(10.0 * V, 0.0, 0.0))
        for t in (0.0, 10.0, 20.0):
            self._grid_only(loop, state, 27.0, t)
        set_inputs(state, (None, None, None), i=(27.0, 0.0, 0.0), ev_w=(10.0 * V, 0.0, 0.0))
        loop._breaker.phase_fresh = [True, True, True]  # even if flagged fresh, no paired sample -> missing
        assert limit_phase_current(loop, 11000.0, now=20.0) == _MIN_CHARGE_W
        assert loop._breaker.missing_cycles == 1

    def test_full_mean_over_but_paired_excess_not_positive_stays_anchored_at_ev_delivery(self):
        loop, state = _loop()
        self._grid_only(loop, state, 28.0, 0.0)
        self._grid_only(loop, state, 28.0, 10.0)
        set_inputs(state, (None, None, None), i=(24.0, 0.0, 0.0), ev_w=(12.0 * V, 0.0, 0.0))
        run_cycle(loop, 11000.0, 20.0)
        out = run_cycle(loop, 11000.0, 30.0)  # full mean 26 A over, paired mean 24 A under
        assert loop._breaker.cap_w == pytest.approx(3 * V * 12.0)  # capped at measured delivery, not uncapped
        assert out == pytest.approx(3 * V * 12.0)
