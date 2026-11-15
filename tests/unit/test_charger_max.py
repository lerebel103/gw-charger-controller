"""Charger rated-power cap (register 10058): decode, on-demand reads, fallback, cap in every mode, no ramp windup."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.control.constants import _MAX_CHARGE_W, _MIN_CHARGE_W
from app.control.mode_strategies import compute_setpoint, setpoint_eco_day, setpoint_eco_night
from app.control.power_utils import charger_max_w, clamp_charge_w
from app.modbus import EVChargerModbusClient
from app.modbus.ev import decode_rated_power_w
from app.state import AppState, ChargeSessionState
from tests.unit.eco_replay_harness import IgnoringCar, constant_site, run_scenario, surplus_site, user_state
from tests.unit.helpers import fill_battery_samples, fill_grid_samples, make_control_loop


def _resp(registers, *, error=False):
    resp = MagicMock()
    resp.isError.return_value = error
    resp.registers = registers
    return resp


# --------------------------------------------------------------------------------------------------
# Decode and read
# --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("raw", "watts"), [(0, 7000.0), (1, 11000.0), (2, 22000.0), (3, None), (0xFFFF, None)])
def test_decode_rated_power(raw, watts):
    assert decode_rated_power_w(raw) == watts


def _client(read):
    ec = EVChargerModbusClient(AppState())
    mc = AsyncMock()
    mc.connected = True
    mc.read_holding_registers = read
    ec._client = mc
    return ec, mc


async def test_read_rated_power_reads_10058_fc03_count1():
    ec, mc = _client(AsyncMock(return_value=_resp([1])))
    assert await ec.read_rated_power_w() == 11000.0
    mc.read_holding_registers.assert_awaited_once_with(address=10058, count=1, device_id=247)


async def test_read_rated_power_error_and_exception_give_none():
    ec, _ = _client(AsyncMock(return_value=_resp([], error=True)))
    assert await ec.read_rated_power_w() is None
    ec, _ = _client(AsyncMock(side_effect=OSError("boom")))
    assert await ec.read_rated_power_w() is None


async def test_read_rated_power_disconnected_gives_none():
    ec = EVChargerModbusClient(AppState())
    assert await ec.read_rated_power_w() is None


async def test_connection_epoch_increments_on_connect(monkeypatch):
    state = AppState(ev_charger_ip="1.2.3.4", ev_charger_port=502)
    ec = EVChargerModbusClient(state)
    fake = AsyncMock()
    fake.connect = AsyncMock(return_value=True)
    fake.connected = False
    monkeypatch.setattr("app.modbus.ev.AsyncModbusTcpClient", MagicMock(return_value=fake))
    monkeypatch.setattr(ec, "_read_serial_number_once", AsyncMock())
    assert ec.connection_epoch == 0
    await ec.ensure_connected()
    assert ec.connection_epoch == 1


# --------------------------------------------------------------------------------------------------
# Cap helpers and modes
# --------------------------------------------------------------------------------------------------
def test_cap_helpers():
    cl = make_control_loop(AppState())
    assert charger_max_w(cl) == _MAX_CHARGE_W
    cl._state.ev_charger_max_power_w = 11000.0
    assert charger_max_w(cl) == 11000.0
    assert clamp_charge_w(30000.0, cl) == 11000.0
    assert clamp_charge_w(1000.0, cl) == _MIN_CHARGE_W
    cl._state.ev_charger_max_power_w = 40000.0  # never above the 22 kW constant
    assert charger_max_w(cl) == _MAX_CHARGE_W
    cl._state.ev_charger_max_power_w = 3000.0  # hypothetical: the cap wins over the 4400 W floor
    assert clamp_charge_w(1000.0, cl) == 3000.0
    assert clamp_charge_w(9000.0, cl) == 3000.0


def test_manual_capped():
    state = AppState(ev_connected=True, charge_mode="Manual", manual_power_w=20000.0, ev_charger_max_power_w=11000.0)
    assert compute_setpoint(make_control_loop(state)) == 11000.0


def test_manual_below_cap_unchanged():
    state = AppState(ev_connected=True, charge_mode="Manual", manual_power_w=7000.0, ev_charger_max_power_w=11000.0)
    assert compute_setpoint(make_control_loop(state)) == 7000.0


def test_eco_night_capped():
    state = AppState(
        ev_connected=True,
        charge_mode="Eco",
        solar_battery_soc_pct=90.0,
        solar_battery_power_w=0.0,
        solar_battery_max_ev_charge_power_w=15000.0,
        solar_battery_max_discharge_w=30000.0,
        eco_day_min_solar_battery_soc_pct=20.0,
        ev_charger_max_power_w=11000.0,
    )
    cl = make_control_loop(state)
    assert setpoint_eco_night(cl) == 11000.0
    assert compute_setpoint(cl) <= 11000.0


def test_eco_day_ramp_does_not_wind_up_above_cap():
    state = AppState(
        ev_connected=True,
        charge_mode="Eco",
        solar_battery_soc_pct=100.0,
        solar_battery_power_w=2000.0,
        grid_power_w=-1500.0,
        eco_day_min_solar_battery_soc_pct=90.0,
        solar_battery_day_power_limit_w=-1500.0,
        ev_charger_max_power_w=11000.0,
    )
    cl = make_control_loop(state)
    cl._eco_charging = True
    cl._eco_day_setpoint_w = 10800.0
    fill_grid_samples(cl, -1500.0)
    fill_battery_samples(cl, 2000.0)
    for _ in range(50):
        assert setpoint_eco_day(cl) <= 11000.0
        assert cl._eco_day_setpoint_w <= 11000.0
    assert cl._eco_day_setpoint_w == 11000.0
    # Battery discharging: the very next step goes down from the cap (no accumulated excess).
    fill_battery_samples(cl, -500.0)
    state.solar_battery_power_w = -500.0
    assert setpoint_eco_day(cl) < 11000.0
    assert cl._eco_day_setpoint_w <= 11000.0 - state.eco_day_ramp_step_w + 1e-6


def test_compute_setpoint_cap_below_floor_wins():
    state = AppState(ev_connected=True, charge_mode="Manual", manual_power_w=7000.0, ev_charger_max_power_w=3000.0)
    assert compute_setpoint(make_control_loop(state)) == 3000.0


# --------------------------------------------------------------------------------------------------
# Loop: when the register is read
# --------------------------------------------------------------------------------------------------
def _manual_state(**kw):
    return user_state(charge_mode="Manual", manual_power_w=20000.0, **kw)


async def test_session_start_reads_once_logs_info_and_caps(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    result = await run_scenario(
        monkeypatch,
        n_cycles=6,
        site=constant_site(),
        state=_manual_state(),
        session=ChargeSessionState.IDLE,
        eco_charging=False,
        ramp_w=_MIN_CHARGE_W,
        charger_max_w=11000.0,
    )
    # one read after connect (epoch change) and one at the Idle -> Charging transition
    assert result.ev.read_rated_power_w.await_count == 2
    assert any("Charger configured max power: 11000 W" in r.getMessage() for r in caplog.records)
    assert result.state.ev_charger_max_power_w == 11000.0
    assert max(w for _, w in result.writes) == 11000.0


async def test_no_per_cycle_reads(monkeypatch):
    result = await run_scenario(
        monkeypatch, n_cycles=200, site=constant_site(), state=_manual_state(), charger_max_w=11000.0
    )
    assert result.ev.read_rated_power_w.await_count == 1  # connect only; session already charging
    assert max(w for _, w in result.writes) == 11000.0


async def test_no_read_in_standby(monkeypatch):
    result = await run_scenario(
        monkeypatch,
        n_cycles=50,
        site=constant_site(),
        state=user_state(charge_mode="Standby"),
        session=ChargeSessionState.IDLE,
        eco_charging=False,
        charger_max_w=11000.0,
    )
    assert result.ev.read_rated_power_w.await_count == 0
    assert result.state.ev_charger_max_power_w is None


async def test_standby_exit_reads_after_reconnect(monkeypatch):
    def on_cycle(i, state, loop):
        if i == 20:
            state.charge_mode = "Manual"
            state.manual_power_w = 20000.0
            loop._ev_client.connection_epoch = 2  # the real client reconnects on Standby exit

    result = await run_scenario(
        monkeypatch,
        n_cycles=40,
        site=constant_site(),
        state=user_state(charge_mode="Standby"),
        session=ChargeSessionState.IDLE,
        eco_charging=False,
        charger_max_w=11000.0,
        on_cycle=on_cycle,
    )
    assert result.ev.read_rated_power_w.await_count >= 1
    assert result.state.ev_charger_max_power_w == 11000.0


@pytest.mark.parametrize("failure", [AsyncMock(return_value=None), AsyncMock(side_effect=OSError("x"))])
async def test_read_failure_falls_back_to_22kw_warns_once(monkeypatch, caplog, failure):
    caplog.set_level(logging.INFO)

    def install(i, st, loop):
        if i == 0:  # the harness installs no read mock when charger_max_w is None
            loop._ev_client.connection_epoch = 1
            loop._ev_client.read_rated_power_w = failure

    result = await run_scenario(monkeypatch, n_cycles=60, site=constant_site(), state=_manual_state(), on_cycle=install)
    warnings = [r for r in caplog.records if "rated power" in r.getMessage() and r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert result.state.ev_charger_max_power_w is None
    assert charger_max_w(result.loop) == _MAX_CHARGE_W
    assert max(w for _, w in result.writes) == 20000.0  # loop kept running; manual 20000 W is under 22 kW


async def test_last_good_value_kept_on_later_failure(monkeypatch):
    def on_cycle(i, state, loop):
        if i == 10:
            loop._ev_client.read_rated_power_w = AsyncMock(return_value=None)
            loop._ev_client.connection_epoch = 2

    result = await run_scenario(
        monkeypatch,
        n_cycles=30,
        site=constant_site(),
        state=_manual_state(),
        charger_max_w=11000.0,
        on_cycle=on_cycle,
    )
    assert result.state.ev_charger_max_power_w == 11000.0
    assert all(w <= 11000.0 for _, w in result.writes)


# --------------------------------------------------------------------------------------------------
# Eco ramp through the real loop: no windup
# --------------------------------------------------------------------------------------------------
async def test_eco_ramp_capped_in_loop_and_ramp_down_unchanged(monkeypatch):
    surplus = lambda i: 30000.0 if i < 400 else 0.0  # noqa: E731
    result = await run_scenario(
        monkeypatch,
        n_cycles=480,
        site=surplus_site(surplus),
        car=IgnoringCar(),
        ramp_w=_MIN_CHARGE_W,
        charger_max_w=11000.0,
    )
    assert max(result.commanded) == 11000.0
    assert max(result.ramp) == 11000.0
    assert result.records[399].ramp_w == 11000.0
    # surplus gone: the ramp leaves the cap on the first step down, never lingers above it
    assert result.records[-1].ramp_w < 11000.0
