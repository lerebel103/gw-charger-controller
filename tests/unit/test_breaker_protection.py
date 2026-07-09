"""Unit tests for breaker protection helpers (limit_phase_current, mean_phase_current)."""

from __future__ import annotations

import time as _time

from app.control.constants import (
    _BREAKER_SAFETY_FRACTION,
    _MIN_CHARGE_W,
    _PHASE_CURRENT_MEAN_WINDOW_S,
)
from app.control.power_utils import (
    limit_phase_current,
    mean_phase_current,
    record_phase_current_samples,
)
from app.state import AppState
from tests.unit.helpers import make_ns_loop


def _make_state(
    *,
    grid_currents=(10.0, 8.0, 6.0),
    ev_currents=(5.0, 5.0, 5.0),
    voltages=(230.0, 230.0, 230.0),
    breaker_limit=32.0,
) -> AppState:
    """Create AppState with typical breaker-protection fields populated."""
    state = AppState(
        victron_l1_current_a=grid_currents[0],
        victron_l2_current_a=grid_currents[1],
        victron_l3_current_a=grid_currents[2],
        ev_current_a=ev_currents[0],
        ev_current_b=ev_currents[1],
        ev_current_c=ev_currents[2],
        victron_l1_voltage_v=voltages[0],
        victron_l2_voltage_v=voltages[1],
        victron_l3_voltage_v=voltages[2],
        grid_breaker_limit_a=breaker_limit,
    )
    return state


def _fill_phase_samples(loop, l1=10.0, l2=8.0, l3=6.0, count=5):
    """Fill phase-current rolling buffers with constant values."""
    now = _time.monotonic()
    loop._l1_current_samples = [(now - i, l1) for i in range(count)]
    loop._l2_current_samples = [(now - i, l2) for i in range(count)]
    loop._l3_current_samples = [(now - i, l3) for i in range(count)]


class TestMeanPhaseCurrent:
    def test_returns_mean_of_samples(self):
        state = AppState()
        loop = make_ns_loop(state)
        now = _time.monotonic()
        loop._l1_current_samples = [(now - 1, 10.0), (now, 20.0)]

        assert mean_phase_current(loop, 1) == 15.0

    def test_returns_none_when_empty(self):
        state = AppState()
        loop = make_ns_loop(state)

        assert mean_phase_current(loop, 1) is None
        assert mean_phase_current(loop, 2) is None
        assert mean_phase_current(loop, 3) is None

    def test_invalid_phase_returns_none(self):
        state = AppState()
        loop = make_ns_loop(state)

        assert mean_phase_current(loop, 4) is None
        assert mean_phase_current(loop, 0) is None


class TestRecordPhaseCurrentSamples:
    def test_records_non_none_values(self):
        state = AppState(
            victron_l1_current_a=5.0,
            victron_l2_current_a=3.0,
            victron_l3_current_a=None,
        )
        loop = make_ns_loop(state)

        record_phase_current_samples(loop)

        assert len(loop._l1_current_samples) == 1
        assert len(loop._l2_current_samples) == 1
        assert len(loop._l3_current_samples) == 0

    def test_prunes_samples_beyond_window(self):
        state = AppState(victron_l1_current_a=5.0, victron_l2_current_a=3.0, victron_l3_current_a=2.0)
        loop = make_ns_loop(state)
        old = _time.monotonic() - _PHASE_CURRENT_MEAN_WINDOW_S - 10.0
        loop._l1_current_samples = [(old, 99.0)]

        record_phase_current_samples(loop)

        # Old sample pruned, only new one remains
        assert len(loop._l1_current_samples) == 1
        assert loop._l1_current_samples[0][1] == 5.0


class TestLimitPhaseCurrentNoCap:
    """Cases where the cap should NOT reduce the setpoint."""

    def test_noop_when_all_phases_under_limit(self):
        state = _make_state(grid_currents=(10.0, 8.0, 6.0), ev_currents=(5.0, 5.0, 5.0))
        loop = make_ns_loop(state)
        _fill_phase_samples(loop, 10.0, 8.0, 6.0)

        # Safety limit = 0.80 * 32 = 25.6A
        # I_base_L1 = 10-5=5A, headroom_L1=25.6-5=20.6A
        # P_cap = 20.6 * 690 = 14214W → no cap on 7000W
        result = limit_phase_current(loop, 7000.0)
        assert result == 7000.0

    def test_noop_when_setpoint_zero(self):
        state = _make_state()
        loop = make_ns_loop(state)
        _fill_phase_samples(loop)

        assert limit_phase_current(loop, 0.0) == 0.0

    def test_noop_when_grid_current_none(self):
        state = _make_state()
        state.victron_l1_current_a = None
        loop = make_ns_loop(state)
        _fill_phase_samples(loop)

        assert limit_phase_current(loop, 7000.0) == 7000.0

    def test_noop_when_ev_current_none(self):
        state = _make_state()
        state.ev_current_a = None
        loop = make_ns_loop(state)
        _fill_phase_samples(loop)

        assert limit_phase_current(loop, 7000.0) == 7000.0

    def test_noop_when_voltage_none(self):
        state = _make_state()
        state.victron_l1_voltage_v = None
        loop = make_ns_loop(state)
        _fill_phase_samples(loop)

        assert limit_phase_current(loop, 7000.0) == 7000.0

    def test_noop_when_mean_buffer_empty(self):
        state = _make_state()
        loop = make_ns_loop(state)
        # No samples in buffers

        assert limit_phase_current(loop, 7000.0) == 7000.0

    def test_noop_when_solar_exporting(self):
        state = _make_state(grid_currents=(-5.0, -3.0, -2.0), ev_currents=(8.0, 8.0, 8.0))
        loop = make_ns_loop(state)
        _fill_phase_samples(loop, -5.0, -3.0, -2.0)

        # All baselines negative → huge headroom → no cap
        assert limit_phase_current(loop, 11000.0) == 11000.0


class TestLimitPhaseCurrentBindingPhase:
    """Cases where the cap reduces setpoint based on binding phase."""

    def test_caps_to_binding_phase_headroom(self):
        # L1 heavily loaded by household
        state = _make_state(grid_currents=(22.0, 12.0, 10.0), ev_currents=(8.0, 8.0, 8.0))
        loop = make_ns_loop(state)
        _fill_phase_samples(loop, 22.0, 12.0, 10.0)

        # I_base_L1 = 22-8=14A, headroom_L1 = 25.6-14 = 11.6A
        # P_cap = 11.6 * 690 = 8004W
        result = limit_phase_current(loop, 9000.0)
        assert abs(result - 8004.0) < 1.0

    def test_single_phase_uses_only_active(self):
        # Only L1 active (L2, L3 below threshold)
        state = _make_state(
            grid_currents=(20.0, 5.0, 5.0),
            ev_currents=(15.0, 0.3, 0.2),
            voltages=(230.0, 230.0, 230.0),
        )
        loop = make_ns_loop(state)
        _fill_phase_samples(loop, 20.0, 5.0, 5.0)

        # Only L1 active. I_base_L1=20-15=5A, headroom=25.6-5=20.6A
        # P_cap = 20.6 * 230 = 4738W
        result = limit_phase_current(loop, 7000.0)
        assert abs(result - 4738.0) < 1.0


class TestLimitPhaseCurrentBelowMinimum:
    """FR-4: P_cap below _MIN_CHARGE_W forces stop."""

    def test_returns_zero_when_pcap_below_min(self):
        # L1 extremely loaded
        state = _make_state(grid_currents=(24.0, 5.0, 5.0), ev_currents=(3.0, 3.0, 3.0))
        loop = make_ns_loop(state)
        _fill_phase_samples(loop, 24.0, 5.0, 5.0)

        # I_base_L1 = 24-3=21A, headroom_L1=25.6-21=4.6A
        # P_cap = 4.6 * 690 = 3174W < _MIN_CHARGE_W → stop
        result = limit_phase_current(loop, 7000.0)
        assert result == 0.0
        assert loop._breaker_cap_tripped is True

    def test_exactly_at_min_is_not_tripped(self):
        # headroom_A that yields exactly _MIN_CHARGE_W at 690V total
        # 4400/690 = 6.377A headroom needed
        # I_base = 25.6 - 6.377 = 19.22A → grid = 19.22 + ev
        headroom_needed = _MIN_CHARGE_W / 690.0  # ~6.377A
        i_base = _BREAKER_SAFETY_FRACTION * 32.0 - headroom_needed
        grid_l1 = i_base + 5.0  # ev=5A on each phase

        state = _make_state(grid_currents=(grid_l1, 5.0, 5.0), ev_currents=(5.0, 5.0, 5.0))
        loop = make_ns_loop(state)
        _fill_phase_samples(loop, grid_l1, 5.0, 5.0)

        result = limit_phase_current(loop, 7000.0)
        # P_cap should be very close to _MIN_CHARGE_W
        assert abs(result - _MIN_CHARGE_W) < 1.0
        assert loop._breaker_cap_tripped is False


class TestHysteresis:
    """FR-13: Once tripped, require P_cap > _MIN_CHARGE_W + margin to release."""

    def test_holds_at_zero_when_tripped_and_pcap_below_threshold(self):
        state = _make_state(grid_currents=(10.0, 8.0, 6.0), ev_currents=(5.0, 5.0, 5.0))
        loop = make_ns_loop(state, _breaker_cap_tripped=True)
        _fill_phase_samples(loop, 10.0, 8.0, 6.0)

        # P_cap = 20.6 * 690 = 14214W, well above threshold → should release
        result = limit_phase_current(loop, 7000.0)
        assert result == 7000.0
        assert loop._breaker_cap_tripped is False

    def test_stays_zero_when_tripped_and_pcap_below_restart_threshold(self):
        # Need P_cap between _MIN_CHARGE_W and _MIN_CHARGE_W + margin
        # _MIN_CHARGE_W=4400, margin=500, so P_cap needs to be e.g. 4600W
        # 4600/690 = 6.67A headroom → I_base=25.6-6.67=18.93A → grid=18.93+5=23.93A
        target_pcap = _MIN_CHARGE_W + 200  # 4600W, below restart threshold (4900W)
        headroom_a = target_pcap / 690.0
        i_base = _BREAKER_SAFETY_FRACTION * 32.0 - headroom_a
        grid_l1 = i_base + 5.0

        state = _make_state(grid_currents=(grid_l1, 5.0, 5.0), ev_currents=(5.0, 5.0, 5.0))
        loop = make_ns_loop(state, _breaker_cap_tripped=True)
        _fill_phase_samples(loop, grid_l1, 5.0, 5.0)

        result = limit_phase_current(loop, 7000.0)
        assert result == 0.0
        # Still tripped
        assert loop._breaker_cap_tripped is True


class TestInstantaneousOverride:
    """FR-12: Raw reading > 0.90 * I_brk overrides the rolling mean."""

    def test_uses_raw_when_above_threshold(self):
        # Mean is comfortable (15A), but raw spikes to 29A (> 0.90*32=28.8A)
        state = _make_state(grid_currents=(29.0, 10.0, 10.0), ev_currents=(8.0, 8.0, 8.0))
        loop = make_ns_loop(state)
        # Mean is 15A (comfortable), but raw is 29A (above threshold)
        _fill_phase_samples(loop, 15.0, 10.0, 10.0)

        # With mean: I_base_L1=15-8=7, headroom=25.6-7=18.6A, P_cap=12834W → no cap
        # With raw override: I_base_L1=29-8=21, headroom=25.6-21=4.6A, P_cap=3174W → stop
        result = limit_phase_current(loop, 9000.0)
        assert result == 0.0  # forced to zero because P_cap < _MIN_CHARGE_W

    def test_no_override_when_below_threshold(self):
        # Raw is 28A (below 28.8A threshold), mean is 20A
        state = _make_state(grid_currents=(28.0, 10.0, 10.0), ev_currents=(8.0, 8.0, 8.0))
        loop = make_ns_loop(state)
        _fill_phase_samples(loop, 20.0, 10.0, 10.0)

        # Should use mean (20A): I_base=20-8=12, headroom=25.6-12=13.6A, P_cap=9384W
        result = limit_phase_current(loop, 9000.0)
        assert result == 9000.0  # P_cap=9384 > 9000 → no cap


class TestStartupFallback:
    """EC-10: When n_ph=0 (EV idle), use all 3 phases as fallback."""

    def test_uses_all_phases_when_ev_not_drawing(self):
        state = _make_state(
            grid_currents=(20.0, 10.0, 10.0),
            ev_currents=(0.2, 0.1, 0.1),  # all below _PHASE_ACTIVE_THRESHOLD_A
            voltages=(230.0, 230.0, 230.0),
        )
        loop = make_ns_loop(state)
        _fill_phase_samples(loop, 20.0, 10.0, 10.0)

        # All phases < 0.5A → startup fallback uses all 3
        # I_base_L1=20-0.2=19.8, headroom_L1=25.6-19.8=5.8A → binding
        # P_cap = 5.8 * 690 = 4002W < _MIN_CHARGE_W → stop
        result = limit_phase_current(loop, 7000.0)
        assert result == 0.0
