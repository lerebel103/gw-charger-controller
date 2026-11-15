"""Secondary coverage: eco-day ramp through ControlLoop.run_loop with synthetic plant and car models.

The primary regression tests are the real-data replays in test_eco_ramp_realdata_replay.py. Here the car model
(whole-amp, uneven, lagging, ignoring, ideal) closes the loop on the site signals. Assertions are about the
setpoint's response to site signals (battery power), bounds and states, never about matching the car's draw.
"""

from __future__ import annotations

import pytest

from app.control.constants import _BREAKER_STOP_CYCLES, _MAX_CHARGE_W, _MIN_CHARGE_W, _STOP_PRESET_W
from app.state import ChargeModeState, ChargeSessionState
from tests.unit.eco_replay_harness import (
    CAR_MODELS,
    CycleRecord,
    IgnoringCar,
    StepAmpCar,
    constant_site,
    run_scenario,
    surplus_site,
    user_state,
)

STEP = 40.0
DEADBAND = 200.0


def assert_ramp_follows_site_signals(records: list[CycleRecord], start: int = 1) -> int:
    """Per cycle: battery >= -200 W -> ramp +1 step; below -> -1 step; clamped 4400..22000.

    Only evaluated while eco charging is active in both cycles. Returns the number of cycles checked.
    """
    checked = 0
    for prev, cur in zip(records[start - 1 :], records[start:], strict=False):
        if not (prev.eco_charging and cur.eco_charging) or cur.battery_power_w is None:
            continue
        if cur.battery_power_w < -DEADBAND:
            expected = prev.ramp_w - STEP
        else:
            expected = prev.ramp_w + STEP
        expected = min(max(expected, _MIN_CHARGE_W), _MAX_CHARGE_W)
        assert cur.ramp_w == pytest.approx(expected), f"cycle {cur.i}: {prev.ramp_w} -> {cur.ramp_w}, want {expected}"
        checked += 1
    return checked


def car(name: str):
    return CAR_MODELS[name]()


# (a) CSV 1 conditions: min charge ON, SOC 26, battery full, exporting -------------------------------------------
@pytest.mark.parametrize("model", list(CAR_MODELS))
async def test_a_floor_does_not_cap_ramp_cold_start(monkeypatch, model):
    result = await run_scenario(
        monkeypatch,
        n_cycles=150,
        site=surplus_site(9000.0, battery_soc_pct=99.0),
        car=car(model),
        ev_soc_pct=26.0,
        session=ChargeSessionState.IDLE,
        eco_charging=False,
    )
    assert result.commanded[0] in (_MIN_CHARGE_W, _MIN_CHARGE_W + STEP)
    assert result.commanded[1] > _MIN_CHARGE_W
    assert all(c >= _MIN_CHARGE_W for c in result.commanded)
    assert max(result.commanded) > 8500.0
    assert assert_ramp_follows_site_signals(result.records) > 100
    assert result.ev.stop_charging.await_count == 0
    assert all(r.session_state == ChargeSessionState.CHARGING for r in result.records)


# (b) CSV 2 conditions: min charge OFF, SOC 33: the ramp must not stall at ~4910 -----------------------------------
@pytest.mark.parametrize("model", ["step", "uneven", "lagging", "ignoring"])
async def test_b_ramp_passes_old_stall_point_whatever_the_car_does(monkeypatch, model):
    result = await run_scenario(
        monkeypatch,
        n_cycles=80,
        site=constant_site(battery_power_w=85.0, battery_soc_pct=98.4, grid_power_w=-3000.0),
        car=car(model),
        state=user_state(eco_day_min_charge_enabled=False),
        ev_soc_pct=33.0,
    )
    assert result.commanded == pytest.approx([_MIN_CHARGE_W + STEP * (k + 1) for k in range(80)])
    assert any(c > 5040.0 for c in result.commanded)
    assert max(result.commanded) == pytest.approx(7600.0)
    if model == "step":
        amps = [r.car_amps for r in result.records]
        assert 7.0 in amps and 8.0 in amps  # the whole-amp car climbs through its steps


# (c) SOC 26 without surplus: floor holds exactly 4400 W, never 0 ------------------------------------------------
@pytest.mark.parametrize(
    "site",
    [
        pytest.param(constant_site(-500.0, 90.0, 1000.0), id="home_battery_below_gate"),
        pytest.param(constant_site(-500.0, 93.0, 1000.0), id="home_battery_not_full"),
        pytest.param(constant_site(-500.0, 99.0, 1000.0), id="importing_battery_discharging"),
        pytest.param(constant_site(-3500.0, 99.0, 1000.0), id="battery_limit_exceeded"),
    ],
)
async def test_c_no_surplus_holds_floor_never_zero(monkeypatch, site):
    result = await run_scenario(
        monkeypatch,
        n_cycles=100,
        site=site,
        car=StepAmpCar(),
        ev_soc_pct=26.0,
        session=ChargeSessionState.CHARGING,
        eco_charging=True,
    )
    assert result.commanded == [_MIN_CHARGE_W] * 100
    assert {r.mode_state for r in result.records} == {ChargeModeState.ECO_DAY_MIN_CHARGE}
    assert all(r.session_state == ChargeSessionState.CHARGING for r in result.records)
    assert result.ev.stop_charging.await_count == 0
    assert all(w >= _MIN_CHARGE_W for _, w in result.writes)


# (d) SOC crosses ev_min_soc_pct mid-session: no drop, no reset -----------------------------------------------
@pytest.mark.parametrize("model", ["step", "ideal", "lagging"])
async def test_d_soc_crossing_min_keeps_ramping(monkeypatch, model):
    def on_cycle(i, state, loop):
        if i == 39:
            state.ev_soc_pct = 50.0  # below 50 up to and including cycle 39

    result = await run_scenario(
        monkeypatch,
        n_cycles=80,
        site=surplus_site(30000.0),
        car=car(model),
        ev_soc_pct=49.5,
        on_cycle=on_cycle,
    )
    assert result.commanded == pytest.approx([_MIN_CHARGE_W + STEP * (k + 1) for k in range(80)])
    assert {r.mode_state for r in result.records} == {ChargeModeState.ECO_DAY_RAMPING}
    assert all(r.eco_charging for r in result.records)


# (e) surplus falls: ramps down, never below 4400 while SOC < 50 ------------------------------------------------
@pytest.mark.parametrize("model", ["step", "uneven", "lagging", "ideal"])
async def test_e_surplus_falls_ramps_down_to_floor_and_holds(monkeypatch, model):
    result = await run_scenario(
        monkeypatch,
        n_cycles=150,
        site=surplus_site(lambda i: 12000.0 if i < 60 else 0.0),
        car=car(model),
        ev_soc_pct=26.0,
        ramp_w=8000.0,
    )
    assert all(c >= _MIN_CHARGE_W for c in result.commanded)
    assert min(result.commanded[60:]) == _MIN_CHARGE_W
    assert result.commanded[-40:] == [_MIN_CHARGE_W] * 40
    assert result.records[-1].mode_state == ChargeModeState.ECO_DAY_MIN_CHARGE
    assert all(r.session_state == ChargeSessionState.CHARGING for r in result.records)
    assert result.ev.stop_charging.await_count == 0
    # While the 5 min mean battery gate has not tripped the ramp moves one step per cycle with the battery signal.
    assert assert_ramp_follows_site_signals(result.records[:90]) > 50


# (f) breaker still cuts below the floor and stops the charger --------------------------------------------------
async def test_f_breaker_cuts_below_floor_and_stops(monkeypatch):
    result = await run_scenario(
        monkeypatch,
        n_cycles=60,
        site=constant_site(85.0, 99.0, -3000.0, extra_import_a=(30.0, 0.0, 0.0)),
        car=StepAmpCar(),
        ev_soc_pct=26.0,
    )
    assert any(c < _MIN_CHARGE_W for c in result.commanded)
    assert 0.0 in result.commanded
    assert result.ev.stop_charging.await_count >= 1
    assert any(w == _STOP_PRESET_W for _, w in result.writes)
    assert all(r.ramp_w >= _MIN_CHARGE_W for r in result.records)
    assert _BREAKER_STOP_CYCLES >= 1


# (g) car ignores the setpoint: bounded windup, then it takes the full setpoint with insufficient solar ----------
@pytest.mark.parametrize("min_charge", [True, False], ids=["min_charge_on_soc26", "min_charge_off"])
async def test_g_ignoring_car_bounded_windup_then_ramp_down(monkeypatch, min_charge):
    """Ramp-down speed is deliberately unchanged (one step per cycle; user decision D1 pending).

    Phase 1: the ramp climbs one step per cycle, bounded by the charger maximum. Phase 2: the car suddenly takes
    the whole setpoint from a 6 kW surplus, the battery discharges and the ramp steps down every cycle. The 5 min
    mean battery gate (-3000 W) is the backstop; with the floor active it holds 4400 W instead of stopping.
    """
    wake, surplus = 150, 6000.0
    result = await run_scenario(
        monkeypatch,
        n_cycles=wake + 200,
        site=surplus_site(surplus),
        car=IgnoringCar(wake_at=wake),
        state=user_state(eco_day_min_charge_enabled=min_charge),
        ev_soc_pct=26.0 if min_charge else 60.0,
    )
    phase1 = result.records[:wake]
    assert all(_MIN_CHARGE_W <= r.commanded_w <= _MAX_CHARGE_W for r in phase1)
    assert [r.ramp_w for r in phase1] == pytest.approx([_MIN_CHARGE_W + STEP * (k + 1) for k in range(wake)])

    phase2 = result.records[wake + 1 :]
    assert -phase2[0].battery_power_w > 3000.0
    assert assert_ramp_follows_site_signals(result.records, start=wake + 1) >= 20
    # Until the mean battery gate trips (~50 cycles) the setpoint only goes down.
    assert all(b.commanded_w <= a.commanded_w for a, b in zip(phase2[:45], phase2[1:45], strict=False))
    gate = result.records[wake + 60]
    if min_charge:
        assert all(r.commanded_w >= _MIN_CHARGE_W for r in phase2)
        assert gate.commanded_w == _MIN_CHARGE_W  # gate tripped: floor held, session not ended
    else:
        assert gate.commanded_w == 0.0  # gate tripped: stop + 300 s cooldown
    # After recovery the ramp settles around the 6 kW surplus, driven by the battery signal alone.
    tail = [r.commanded_w for r in result.records[-30:]]
    assert all(surplus - 400.0 <= c <= surplus + 400.0 for c in tail), tail
    assert all(r.commanded_w <= _MAX_CHARGE_W for r in result.records)


# (h) min charge disabled behaves like an EV above the minimum SOC; gates unchanged ----------------------------------
async def test_h_min_charge_disabled_ramp_unchanged(monkeypatch):
    kwargs = {"n_cycles": 100, "site": surplus_site(30000.0), "car": IgnoringCar()}
    off = await run_scenario(monkeypatch, state=user_state(eco_day_min_charge_enabled=False), ev_soc_pct=26.0, **kwargs)
    above = await run_scenario(monkeypatch, state=user_state(), ev_soc_pct=60.0, **kwargs)
    assert off.commanded == above.commanded == pytest.approx([_MIN_CHARGE_W + STEP * (k + 1) for k in range(100)])


@pytest.mark.parametrize("min_charge,soc", [(False, 26.0), (True, 60.0)])
async def test_h_gates_still_stop_without_floor(monkeypatch, min_charge, soc):
    result = await run_scenario(
        monkeypatch,
        n_cycles=20,
        site=constant_site(85.0, 80.0, -3000.0),  # home battery below the 92 % gate
        state=user_state(eco_day_min_charge_enabled=min_charge),
        ev_soc_pct=soc,
    )
    # The handler returns 0; the session state machine holds the last setpoint for its 10 s stopping delay.
    assert result.commanded[-1] == 0.0
    assert all(r.mode_state == ChargeModeState.ECO_DAY_SOC_GATE for r in result.records)


# (k) the ramp does not depend on the car ---------------------------------------------------------------------------
async def test_k_ramp_independent_of_car(monkeypatch):
    series = []
    for model in ("ideal", "ignoring", "step", "lagging"):
        result = await run_scenario(
            monkeypatch,
            n_cycles=60,
            site=constant_site(85.0, 98.4, -3000.0),
            car=car(model),
            state=user_state(eco_day_min_charge_enabled=False),
            ev_soc_pct=33.0,
        )
        series.append(result.ramp)
    assert all(s == series[0] for s in series)


# (n) Eco night with the floor: discharge-limit trip holds 4400 W instead of stopping ---------------------------
@pytest.mark.parametrize("min_charge", [True, False])
async def test_n_night_discharge_limit_trip_holds_floor(monkeypatch, min_charge):
    result = await run_scenario(
        monkeypatch,
        n_cycles=30,
        site=constant_site(battery_power_w=-17000.0, battery_soc_pct=60.0, grid_power_w=0.0),
        state=user_state(eco_day_min_charge_enabled=min_charge, solar_battery_max_discharge_w=5000.0),
        ev_soc_pct=26.0,
        night=True,
    )
    if min_charge:
        assert result.commanded == [_MIN_CHARGE_W] * 30
        assert {r.mode_state for r in result.records} == {ChargeModeState.ECO_NIGHT_BATTERY}
        assert result.ev.stop_charging.await_count == 0
    else:
        assert result.commanded[-1] == 0.0
        assert result.ev.stop_charging.await_count >= 1
