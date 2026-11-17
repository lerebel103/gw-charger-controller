"""Persistence and loop wiring of the learned SOC efficiency constant k."""

from __future__ import annotations

import time as _time
from unittest.mock import MagicMock

import pytest

from app.config import ConfigManager
from app.control.soc_estimator import KLearnResult, effective_k
from app.state import AppState
from tests.unit.helpers import make_control_loop


def _write_base(path):
    path.write_text("mqtt_host: h\nmqtt_port: 1883\nmqtt_username: u\nmqtt_password: p\n")


class TestRoundTrip:
    async def test_k_and_capacity_survive_round_trip(self, tmp_path):
        path = tmp_path / "config.yaml"
        _write_base(path)
        mgr = ConfigManager(str(path))
        state = mgr.load()
        state.ev_soc_k = 1.0123
        state.ev_soc_k_capacity_kwh = state.ev_battery_capacity_kwh
        mgr.schedule_persist(state)
        await mgr._write()
        loaded = ConfigManager(str(path)).load()
        assert loaded.ev_soc_k == pytest.approx(1.0123)
        assert loaded.ev_soc_k_capacity_kwh == state.ev_battery_capacity_kwh
        assert effective_k(loaded) == pytest.approx(1.0123)

    async def test_capacity_change_resets_effective_k(self, tmp_path):
        path = tmp_path / "config.yaml"
        _write_base(path)
        mgr = ConfigManager(str(path))
        state = mgr.load()
        state.ev_soc_k = 1.05
        state.ev_soc_k_capacity_kwh = state.ev_battery_capacity_kwh
        mgr.schedule_persist(state)
        await mgr._write()
        loaded = ConfigManager(str(path)).load()
        loaded.ev_battery_capacity_kwh = 60.0
        assert effective_k(loaded) == 1.0

    def test_unset_k_defaults_to_one(self):
        assert effective_k(AppState()) == 1.0


class TestLoopApply:
    def test_applies_result_and_schedules_persist(self):
        state = AppState(ev_battery_capacity_kwh=82.0)
        cl = make_control_loop(state)
        cl._config_manager = MagicMock()
        cl._apply_k_result(1.0, KLearnResult(k_span=1.006, k_new=1.0006))
        assert state.ev_soc_k == pytest.approx(1.0006)
        assert state.ev_soc_k_capacity_kwh == 82.0
        cl._config_manager.schedule_persist.assert_called_once_with(state)

    def test_tiny_change_skipped(self):
        state = AppState(ev_soc_k=1.0, ev_soc_k_capacity_kwh=82.0)
        cl = make_control_loop(state)
        cl._config_manager = MagicMock()
        cl._apply_k_result(1.0, KLearnResult(k_span=1.0, k_new=1.00001))
        assert state.ev_soc_k == 1.0
        cl._config_manager.schedule_persist.assert_not_called()

    def test_no_config_manager_is_fine(self):
        state = AppState()
        cl = make_control_loop(state)
        cl._config_manager = None
        cl._apply_k_result(1.0, KLearnResult(k_span=1.1, k_new=1.01))
        assert state.ev_soc_k == pytest.approx(1.01)

    def test_update_estimator_refines_k_end_to_end(self):
        state = AppState(ev_connected=True, ev_battery_capacity_kwh=82.0)
        cl = make_control_loop(state)
        cl._config_manager = MagicMock()
        for i in range(17):
            state.ev_soc_pct = 40.0 + 0.5 * i
            state.ev_soc_pct_updated_at = _time.monotonic()
            state.ev_session_energy_wh = 412.5 * i
            cl._update_soc_estimator()
        assert state.ev_soc_k is not None
        assert 1.0 < state.ev_soc_k < 1.006
        assert state.ev_soc_k_capacity_kwh == 82.0
        cl._config_manager.schedule_persist.assert_called()
