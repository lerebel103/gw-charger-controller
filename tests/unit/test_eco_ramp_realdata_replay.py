"""PRIMARY regression tests: real recorded data from the 2026-11-15 eco-day incident, replayed through
ControlLoop.run_loop (see tests/fixtures/incidents/2026-11-15-eco-min-charge/README.md).

The EV side is replayed OPEN LOOP: the recorded car draw (~4224 W / ~6 A) is fed every cycle whatever the
controller writes. The car ignored the setpoint in both recordings, so replaying recorded site signals is valid.
Assertions are about the setpoint as a function of site signals (battery power, grid, SOC, breaker), never about
matching the car's draw.

CSV 1 (min charge ON): production output was flat 4400 W for over an hour while exporting.
CSV 2 (min charge OFF): production output stalled at ~4910 W (v0.1.13 "EV is following" guard).

The L* tests reproduce the production behaviour of main 7eb4042 and are opt-in (GW_REPLAY_LEGACY=1). They are
expected to FAIL on the fixed code. R* tests are the regression tests: they pass on the fix and fail on main.
"""

from __future__ import annotations

import os

import pytest

from app.control.constants import _BREAKER_STOP_CYCLES, _MAX_CHARGE_W, _MIN_CHARGE_W, _STOP_PRESET_W
from app.state import ChargeModeState, ChargerStatus, ChargeSessionState
from tests.unit.eco_replay_harness import CYCLE_S, Result, run_scenario, user_state
from tests.unit.replay_data import (
    CSV1_EV_SOC,
    CSV1_SITE,
    CSV2_STALL,
    CSV3_GOLDEN,
    EV_SOC,
    RECORDED_SETPOINT,
    cycle_inputs,
    cycles,
    load_replay,
    utc,
)

legacy_only = pytest.mark.skipif(
    os.environ.get("GW_REPLAY_LEGACY") != "1", reason="reproduces main 7eb4042 behaviour; set GW_REPLAY_LEGACY=1"
)

_CONNECT_AT = utc("2026-11-15T05:53:05Z")
_CSV1_START, _CSV1_END = utc("2026-11-15T05:50:00Z"), utc("2026-11-15T07:06:00Z")
_CSV2_START, _CSV2_END = utc("2026-11-15T07:19:00Z"), utc("2026-11-15T07:24:12Z")
_STEP = 40.0


def _split(row):
    inputs = cycle_inputs(row)
    site = {k: v for k, v in inputs.items() if not k.startswith("ev_")}
    ev = {k: v for k, v in inputs.items() if k.startswith("ev_")}
    return site, ev


async def run_recorded(
    monkeypatch,
    paths,
    start,
    end,
    *,
    min_charge: bool,
    connect_at=None,
    session=ChargeSessionState.CHARGING,
    eco_charging=True,
    ramp_w=_MIN_CHARGE_W,
    site_tweak=None,
    on_cycle=None,
    cycle_s=CYCLE_S,
    ev_soc_pct=26.0,
) -> tuple[Result, list[dict]]:
    series = load_replay(*paths)
    rows = cycles(series, start, end, step_s=cycle_s)

    def site(i, _car_w):
        fields, _ = _split(rows[i])
        return site_tweak(i, rows[i], fields) if site_tweak else fields

    def ev_fields(i, state):
        _, ev = _split(rows[i])
        ev.pop("ev_soc_pct", None)
        if EV_SOC in rows[i] and rows[i][EV_SOC] is not None:
            ev["ev_soc_pct"] = rows[i][EV_SOC]
        if connect_at is None:
            ev["ev_connected"] = True
            ev["ev_charger_status_enum"] = ChargerStatus.CHARGING_IN_PROGRESS
        else:
            t = rows[i]["t"]
            ev["ev_connected"] = t >= connect_at.timestamp()
            first = connect_at.timestamp() <= t < connect_at.timestamp() + cycle_s
            ev["ev_charger_status_enum"] = (
                ChargerStatus.IDLE_NO_CONNECTOR
                if not ev["ev_connected"]
                else ChargerStatus.IDLE_CONNECTOR_PLUGGED
                if first
                else ChargerStatus.CHARGING_IN_PROGRESS
            )
        return ev

    state = user_state(eco_day_min_charge_enabled=min_charge, ev_connected=connect_at is None)
    result = await run_scenario(
        monkeypatch,
        n_cycles=len(rows),
        site=site,
        ev_fields=ev_fields,
        state=state,
        ev_soc_pct=ev_soc_pct,
        session=session,
        eco_charging=eco_charging,
        ramp_w=ramp_w,
        on_cycle=on_cycle,
        cycle_s=cycle_s,
    )
    return result, rows


def _csv1(monkeypatch, **kw):
    return run_recorded(
        monkeypatch,
        (CSV1_SITE, CSV1_EV_SOC),
        _CSV1_START,
        _CSV1_END,
        min_charge=True,
        connect_at=_CONNECT_AT,
        session=ChargeSessionState.IDLE,
        eco_charging=False,
        **kw,
    )


def _connected(result, rows):
    return [r for r, row in zip(result.records, rows, strict=True) if row["t"] >= _CONNECT_AT.timestamp()]


def _ramp_invariant_cycles(result, connected_from: int = 0) -> int:
    """Check: battery >= -200 W -> the ramp rises by exactly one step (or sits at the max).

    Returns the number of qualifying cycles. Cycles where the session is not charging or the ramp was pulled
    down by the breaker clamp are skipped.
    """
    n = 0
    recs = result.records
    for prev, cur in zip(recs[connected_from:], recs[connected_from + 1 :], strict=False):
        if not (prev.eco_charging and cur.eco_charging) or cur.battery_power_w is None:
            continue
        if cur.battery_power_w < -200.0:
            continue
        if cur.commanded_w < cur.ramp_w:  # breaker cut
            continue
        assert cur.ramp_w == min(prev.ramp_w + _STEP, _MAX_CHARGE_W), f"cycle {cur.i}: {prev.ramp_w} -> {cur.ramp_w}"
        n += 1
    return n


# --------------------------------------------------------------------------------------------------
# R1: CSV 1, min charge ON, SOC 25.5 -> 31.5 (synthesised), battery 98.5-100 %, exporting
# --------------------------------------------------------------------------------------------------
async def test_r1_csv1_min_charge_is_floor_ramp_rises_above_4400(monkeypatch):
    result, rows = await _csv1(monkeypatch)
    recs = _connected(result, rows)
    pre = [r for r, row in zip(result.records, rows, strict=True) if row["t"] < _CONNECT_AT.timestamp()]
    assert pre and all(r.commanded_w == 0.0 for r in pre)  # car not connected yet: nothing commanded

    assert all(r.commanded_w >= _MIN_CHARGE_W for r in recs), min(r.commanded_w for r in recs)
    assert result.ev.stop_charging.await_count == 0
    assert all(r.session_state == ChargeSessionState.CHARGING for r in recs)

    # The ramp runs on every cycle with surplus (battery >= -200 W), +40 W exactly.
    first = result.records.index(recs[0])
    qualifying = _ramp_invariant_cycles(result, first)
    assert qualifying >= 100, qualifying
    assert result.commanded[-1] > 6000.0, result.commanded[-1]

    # Sub-states: MIN_CHARGE when held at the floor, RAMPING above it.
    for r in recs:
        if r.commanded_w > _MIN_CHARGE_W:
            assert r.mode_state == ChargeModeState.ECO_DAY_RAMPING
        else:
            assert r.mode_state == ChargeModeState.ECO_DAY_MIN_CHARGE
    # Whatever is written to the charger is the commanded value.
    assert all(r.written_w == r.commanded_w for r in recs if r.written_w is not None)


# --------------------------------------------------------------------------------------------------
# R2: CSV 2, min charge OFF: must climb past the old 4910 W pin, +40 W every cycle
# --------------------------------------------------------------------------------------------------
async def test_r2_csv2_ramp_climbs_through_old_stall(monkeypatch):
    """The recorded battery discharges 606 -> 247 W for the first cycles (ramp held at 4400), then stays inside or
    above the 200 W deadband (134 W discharge, then 70-97 W charge) with export on all phases: from there the ramp
    must climb +40 W every cycle, through ~4910 W and beyond."""
    result, rows = await run_recorded(monkeypatch, (CSV2_STALL,), _CSV2_START, _CSV2_END, min_charge=False)
    n = len(result.records)
    assert n >= 60
    k0 = next(k for k, r in enumerate(result.records) if r.battery_power_w >= -200.0)
    assert 3 <= k0 <= 5
    assert all(r.battery_power_w >= -200.0 for r in result.records[k0:])
    assert result.commanded[:k0] == [_MIN_CHARGE_W] * k0
    assert result.commanded[k0:] == pytest.approx([_MIN_CHARGE_W + _STEP * (k + 1) for k in range(n - k0)])
    assert max(result.commanded) > 5040.0
    assert result.commanded[-1] >= _MIN_CHARGE_W + _STEP * (n - k0)
    assert _ramp_invariant_cycles(result) >= n - k0 - 1


# --------------------------------------------------------------------------------------------------
# R3: CSV 1 breaker. Recorded L3 import 06:22-06:36 does not need a cut; a perturbed variant does.
# --------------------------------------------------------------------------------------------------
async def test_r3_csv1_recorded_l3_import_needs_no_cut(monkeypatch):
    result, rows = await _csv1(monkeypatch)
    lo, hi = utc("2026-11-15T06:22:00Z").timestamp(), utc("2026-11-15T06:36:00Z").timestamp()
    window = [r for r, row in zip(result.records, rows, strict=True) if lo <= row["t"] <= hi]
    assert len(window) > 100
    assert all(r.commanded_w == r.ramp_w for r in window)  # limiter did not reduce the request
    assert result.loop._breaker.cap_w is None


async def test_r3_csv1_perturbed_l3_import_breaker_cuts_below_floor_and_stops(monkeypatch):
    """SYNTHETIC perturbation on real data: +24 A extra L3 import 06:22-06:36 overloads the 25.6 A limit.

    The breaker cuts below the floor (the floor does not override it), stops the charger after
    _BREAKER_STOP_CYCLES cycles at minimum, and the ramp variable never falls below 4400.
    """
    lo, hi = utc("2026-11-15T06:22:00Z").timestamp(), utc("2026-11-15T06:36:00Z").timestamp()

    def tweak(i, row, fields):
        if lo <= row["t"] <= hi:
            fields = dict(fields)
            fields["victron_l3_current_a"] += 24.0
            fields["grid_power_w"] += 24.0 * 240.0
        return fields

    result, rows = await _csv1(monkeypatch, site_tweak=tweak)
    window = [r for r, row in zip(result.records, rows, strict=True) if lo <= row["t"] <= hi]
    assert any(r.commanded_w < _MIN_CHARGE_W for r in window), "breaker never cut below the floor"
    assert any(r.commanded_w == 0.0 for r in window), "breaker never stopped the charger"
    assert result.ev.stop_charging.await_count >= 1
    assert any(w == _STOP_PRESET_W for _, w in result.writes)
    assert all(r.ramp_w >= _MIN_CHARGE_W for r in result.records)
    assert _BREAKER_STOP_CYCLES >= 1


# --------------------------------------------------------------------------------------------------
# R4: CSV 3 golden: 2026-11-12, the ramp working correctly in production (EV SOC 69.5-79.5 %, floor inactive)
# --------------------------------------------------------------------------------------------------
_CSV3_START, _CSV3_END = utc("2026-11-12T07:35:41Z"), utc("2026-11-12T09:30:00Z")
_CSV3_PERIOD_S = 6.45  # recorded setpoint updates every ~6.45 s (5 s interval + Modbus time)


async def test_r4_csv3_golden_replay_tracks_recorded_ramp(monkeypatch):
    """Open-loop replay of a day where the ramp worked: the fix must reproduce the recorded setpoint.

    Recorded: 4400 W at 07:35:41, +40 W per cycle to a 6400 W peak at 07:41:42, then down as PV fades to 4400 W
    by ~08:12. Open loop the recorded battery signal reflects the recorded (not the replayed) setpoint, so small
    deviations are expected; the replayed ramp must follow the same shape.
    """
    result, rows = await run_recorded(
        monkeypatch,
        (CSV3_GOLDEN,),
        _CSV3_START,
        _CSV3_END,
        min_charge=True,
        eco_charging=False,
        cycle_s=_CSV3_PERIOD_S,
        ev_soc_pct=70.0,
    )
    recorded = [row[RECORDED_SETPOINT] for row in rows]
    replayed = result.commanded
    errors = [abs(a - b) for a, b in zip(replayed[1:], recorded[1:], strict=True)]
    assert sum(errors) / len(errors) < 150.0
    ramp_up = [k for k, row in enumerate(rows) if row["t"] <= utc("2026-11-12T07:41:42Z").timestamp()]
    assert all(errors[k - 1] <= 300.0 for k in ramp_up[1:])
    assert abs(max(replayed) - max(recorded)) <= 300.0
    assert max(recorded) == 6400.0  # the recorded peak (not 8.2 kW)
    late = [c for c, row in zip(replayed, rows, strict=True) if row["t"] >= utc("2026-11-12T08:30:00Z").timestamp()]
    assert late and all(c == _MIN_CHARGE_W for c in late)
    assert all(c >= _MIN_CHARGE_W for c in replayed)
    assert result.ev.stop_charging.await_count == 0


# --------------------------------------------------------------------------------------------------
# LEGACY: reproduce production on main 7eb4042 (opt-in; expected to FAIL on the fix)
# --------------------------------------------------------------------------------------------------
@legacy_only
async def test_l1_main_reproduces_flat_4400(monkeypatch):
    result, rows = await _csv1(monkeypatch)
    recs = _connected(result, rows)
    assert recs
    assert all(r.commanded_w == _MIN_CHARGE_W for r in recs)
    assert all(r.mode_state == ChargeModeState.ECO_DAY_MIN_CHARGE for r in recs)


@legacy_only
async def test_l2_main_reproduces_4720_then_4910_stall(monkeypatch):
    """Hypothesis: stale ramp state 4680 / _eco_charging True left by an earlier OFF period, then min charge
    ON until 07:21:00 and OFF afterwards (user toggled it)."""
    toggle = utc("2026-11-15T07:21:00Z").timestamp()

    def on_cycle(i, state, loop):
        state.eco_day_min_charge_enabled = rows_t[i] < toggle

    series_rows = cycles(load_replay(CSV2_STALL), _CSV2_START, _CSV2_END, step_s=CYCLE_S)
    rows_t = [r["t"] for r in series_rows]
    result, _ = await run_recorded(
        monkeypatch, (CSV2_STALL,), _CSV2_START, _CSV2_END, min_charge=True, ramp_w=4680.0, on_cycle=on_cycle
    )
    after = [c for c, t in zip(result.commanded, rows_t, strict=True) if t >= toggle + CYCLE_S]
    assert all(c == _MIN_CHARGE_W for c, t in zip(result.commanded, rows_t, strict=True) if t < toggle)
    assert after[:5] == pytest.approx([4720.0, 4760.0, 4800.0, 4840.0, 4880.0])
    assert all(4900.0 <= c <= 4930.0 for c in after[6:]), after
