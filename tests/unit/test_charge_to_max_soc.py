"""'Charge to set max SoC' switch: when OFF the max-SOC target never stops or blocks a session."""

from __future__ import annotations

import asyncio
import json
import time as _time
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import ConfigManager
from app.control.mode_strategies import compute_setpoint, resolve_mode_handler
from app.control.power_utils import max_soc_stop_reason
from app.ha.client import MQTTClient
from app.state import AppState
from tests.unit.helpers import make_control_loop


def _loop(*, enabled: bool, age_s: float = 0.0, reported: float = 85.0, delivered_wh: float = 0.0, **state_kw):
    state = AppState(
        ev_connected=True,
        ev_soc_pct=reported,
        ev_max_soc_pct=80.0,
        charge_to_max_soc_enabled=enabled,
        manual_power_w=7000.0,
        **state_kw,
    )
    state.ev_soc_pct_updated_at = _time.monotonic() - age_s
    cl = make_control_loop(state)
    cl._soc_estimator.observe(soc_pct=reported, soc_fresh=True, energy_wh=0.0)
    done = 0.0
    while done < delivered_wh:
        done = min(done + 1000.0, delivered_wh)
        cl._soc_estimator.observe(soc_pct=None, soc_fresh=False, energy_wh=done)
    return cl


class TestSwitchOff:
    def test_default_is_enabled(self):
        assert AppState().charge_to_max_soc_enabled is True

    @pytest.mark.parametrize("mode", ["Manual", "Eco"])
    def test_reported_soc_over_max_does_not_stop_or_block(self, mode):
        cl = _loop(enabled=False, charge_mode=mode)
        assert max_soc_stop_reason(cl) is None
        assert resolve_mode_handler(cl).name != "max_soc_blocked"
        assert cl._state_machine.determine_stop_reason() != "max_soc_reached"

    def test_manual_keeps_charging_over_max(self):
        assert compute_setpoint(_loop(enabled=False, charge_mode="Manual")) == 7000.0

    def test_stalled_estimate_over_max_does_not_stop(self):
        cl = _loop(enabled=False, reported=70.0, delivered_wh=12_000.0, charge_mode="Manual")
        assert cl._soc_estimator.resolve(reported_pct=70.0, reported_fresh=True, capacity_kwh=82.0, k=1.0).stalled
        assert max_soc_stop_reason(cl) is None
        assert compute_setpoint(cl) == 7000.0

    def test_stale_estimate_over_max_does_not_stop(self):
        cl = _loop(enabled=False, age_s=10_000.0, reported=70.0, delivered_wh=12_000.0, charge_mode="Manual")
        assert max_soc_stop_reason(cl) is None
        assert compute_setpoint(cl) == 7000.0

    def test_min_soc_floor_still_applies(self):
        cl = _loop(
            enabled=False,
            reported=30.0,
            charge_mode="Eco",
            eco_day_min_charge_enabled=True,
            ev_min_soc_pct=50.0,
        )
        assert resolve_mode_handler(cl).name != "max_soc_blocked"


class TestSwitchOn:
    def test_reported_over_max_stops(self):
        cl = _loop(enabled=True, charge_mode="Manual")
        assert max_soc_stop_reason(cl) == "max_soc_reached"
        assert compute_setpoint(cl) == 0.0

    def test_stalled_estimate_over_max_stops(self):
        cl = _loop(enabled=True, reported=70.0, delivered_wh=12_000.0, charge_mode="Manual")
        assert max_soc_stop_reason(cl) == "max_soc_estimated"
        assert compute_setpoint(cl) == 0.0


class TestPersistence:
    async def test_round_trip_and_default_for_old_config(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("mqtt_host: h\nmqtt_port: 1883\nmqtt_username: u\nmqtt_password: p\n")
        mgr = ConfigManager(str(path))
        state = mgr.load()
        assert state.charge_to_max_soc_enabled is True
        state.charge_to_max_soc_enabled = False
        mgr.schedule_persist(state)
        await mgr._write()
        assert ConfigManager(str(path)).load().charge_to_max_soc_enabled is False


class TestHomeAssistant:
    async def test_discovery_lists_switch(self):
        client = MQTTClient(state=AppState(), config_manager=MagicMock(), publish_queue=asyncio.Queue())
        client._client = AsyncMock()
        await client._publish_discovery()
        calls = {c.args[0]: json.loads(c.args[1]) for c in client._client.publish.await_args_list if c.args[1]}
        payload = calls["homeassistant/switch/ev_charger_charge_to_max_soc/config"]
        assert payload["name"] == "Charge to set max SoC"
        assert payload["command_topic"] == "ev_charger/switch/charge_to_max_soc/set"
        assert payload["state_topic"] == "ev_charger/switch/charge_to_max_soc/state"

    @pytest.mark.parametrize(("payload", "expected"), [("OFF", False), ("ON", True)])
    async def test_command_updates_state_and_persists(self, payload, expected):
        state = AppState(charge_to_max_soc_enabled=not expected)
        cfg = MagicMock()
        client = MQTTClient(state=state, config_manager=cfg, publish_queue=asyncio.Queue())
        client._client = AsyncMock()
        await client._handle_command("ev_charger/switch/charge_to_max_soc/set", payload)
        assert state.charge_to_max_soc_enabled is expected
        cfg.schedule_persist.assert_called_once_with(state)
        client._client.publish.assert_awaited_once_with(
            "ev_charger/switch/charge_to_max_soc/state", payload, retain=True
        )

    async def test_state_republished_with_config(self):
        client = MQTTClient(
            state=AppState(charge_to_max_soc_enabled=False), config_manager=MagicMock(), publish_queue=asyncio.Queue()
        )
        client._client = AsyncMock()
        await client._publish_config_state()
        published = {c.args[0]: c.args[1] for c in client._client.publish.await_args_list}
        assert published["ev_charger/switch/charge_to_max_soc/state"] == "OFF"
