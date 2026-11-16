"""Replay of the 2026-11-15 history-19 charge through ControlLoop.run_loop (SOC estimator).

Recorded (tests/fixtures/incidents/2026-11-15-ev-soc-estimator/): EV SOC 40.0 -> 48.5 %, session energy and capacity.
SYNTHESISED here: charger power (4224 W while the session-energy register moved in the last 120 s), the charger
status (charging in progress), and the frozen / stale SOC variants. Manual mode, so the setpoint does not depend on
site signals. Assertions never reference the car's draw.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from app.control.soc_estimator import SocSource
from tests.unit.eco_replay_harness import constant_site, run_scenario, user_state
from tests.unit.replay_data import (
    EV_SESSION_ENERGY,
    EV_SOC,
    SOC_ESTIMATOR_CSV,
    cycle_inputs,
    cycles,
    load_replay,
    utc,
)

_START, _END = utc("2026-11-15T16:25:00Z"), utc("2026-11-15T18:20:00Z")
_STEP_S = 10.0
_CAPACITY_KWH = 82.0
_POWER_W = 4224.0
_POWER_WINDOW_S = 120.0


def _prepare():
    rows = cycles(load_replay(SOC_ESTIMATOR_CSV), _START, _END, step_s=_STEP_S)
    power: list[float] = []
    last_value: float | None = None
    last_change_t: float | None = None
    for row in rows:
        value = row[EV_SESSION_ENERGY]
        if value != last_value:
            last_value, last_change_t = value, row["t"]
        power.append(_POWER_W if row["t"] - last_change_t <= _POWER_WINDOW_S else 0.0)
    return rows, power


async def _replay(
    monkeypatch,
    *,
    max_soc: float = 80.0,
    frozen_from: str | None = None,
    stale_from: str | None = None,
    blip: tuple[int, int] | None = None,
):
    """Run the replay; ``frozen_from`` freezes the reported SOC while HA keeps republishing, ``stale_from`` stops
    republishing as well (dead integration), ``blip`` sets SOC and energy to None for cycles [a, b)."""
    rows, power = _prepare()
    freeze_t = utc(frozen_from).timestamp() if frozen_from else None
    stale_t = utc(stale_from).timestamp() if stale_from else None
    frozen: dict[str, float] = {}
    samples: list[dict] = []

    def ev_fields(i, state):
        row = rows[i]
        inputs = cycle_inputs(row)
        soc = row[EV_SOC]
        hold_t = stale_t if stale_t is not None else freeze_t
        if hold_t is not None and row["t"] >= hold_t:
            soc = frozen.setdefault("soc", soc)
        energy = inputs["ev_session_energy_wh"]
        if blip is not None and blip[0] <= i < blip[1]:
            soc, energy = None, None
        return {
            "ev_soc_pct": soc,
            "ev_session_energy_wh": energy,
            "ev_active_power_w": power[i],
            "ev_connected": True,
        }

    def on_cycle(i, state, loop):
        if i == 0:
            loop._config_manager = MagicMock()
        res = loop._soc_estimator.resolve(
            reported_pct=state.ev_soc_pct,
            reported_fresh=True,
            capacity_kwh=_CAPACITY_KWH,
            k=1.0,
        )
        samples.append(
            {
                "i": i,
                "t": rows[i]["t"],
                "stalled": loop._soc_was_stalled,
                "estimate_k1": res.estimated_pct,
                "commanded_w": state.commanded_setpoint_w,
            }
        )

    state = user_state(charge_mode="Manual", ev_max_soc_pct=max_soc, ev_min_soc_pct=0.0)
    result = await run_scenario(
        monkeypatch,
        n_cycles=len(rows),
        site=constant_site(),
        ev_fields=ev_fields,
        state=state,
        ev_soc_pct=40.0,
        on_cycle=on_cycle,
        cycle_s=_STEP_S,
        soc_republish=(lambda i: rows[i]["t"] < stale_t) if stale_t is not None else None,
    )
    return result, rows, samples


def _events(result, name: str) -> list[dict]:
    queue = result.loop._publish_queue
    events = []
    while not queue.empty():
        item = queue.get_nowait()
        if isinstance(item, dict) and item.get("event") == name:
            events.append(item)
    return events


def _first_stop_index(result, rows) -> int | None:
    """Cycle index where the stopping event was queued (setpoint is 0 from then on)."""
    for rec in result.records:
        if rec.commanded_w == 0.0 and rec.t > 1000.0:
            return rec.i
    return None


async def test_healthy_history19_never_stalls_and_learns_k(monkeypatch):
    result, _rows, samples = await _replay(monkeypatch)

    assert not any(s["stalled"] for s in samples)
    assert _events(result, "stopping") == []
    est = result.loop._soc_estimator
    assert est.last_k_span is not None
    assert abs(est.last_k_span - 1.01) < 0.03
    assert 1.0 < result.state.ev_soc_k < 1.02
    assert result.state.ev_soc_k_capacity_kwh == _CAPACITY_KWH
    result.loop._config_manager.schedule_persist.assert_called()
    resolution = result.loop._soc_estimator.resolve(
        reported_pct=result.state.ev_soc_pct, reported_fresh=True, capacity_kwh=_CAPACITY_KWH, k=result.state.ev_soc_k
    )
    assert resolution.source is SocSource.REPORTED
    assert all(r.commanded_w > 0 for r in result.records)


async def test_synthetic_frozen_report_stops_on_estimate(monkeypatch):
    """SYNTHESISED: the report freezes at 42.0 from 16:53Z while HA keeps republishing; the limit is 45 %."""
    result, rows, samples = await _replay(monkeypatch, max_soc=45.0, frozen_from="2026-11-15T16:53:02Z")

    stopping = _events(result, "stopping")
    assert len(stopping) == 1
    assert stopping[0]["reason"] == "max_soc_estimated"
    stop_i = _first_stop_index(result, rows)
    assert stop_i is not None
    assert any(s["stalled"] for s in samples[:stop_i])
    # The estimate (k = 1 here, the learned k stays within 1 %) crosses 44.5 at the stop, long before the
    # recorded SOC reaches 48.5 (18:13Z).
    assert 44.3 <= samples[stop_i]["estimate_k1"] <= 44.9
    assert rows[stop_i]["t"] < utc("2026-11-15T17:40:00Z").timestamp()
    assert rows[stop_i]["t"] < utc("2026-11-15T18:13:02Z").timestamp()
    assert all(r.commanded_w == 0.0 for r in result.records[stop_i:])


async def test_stale_report_stops_on_estimate(monkeypatch):
    """SYNTHESISED: HA stops republishing after 17:00Z with the SOC frozen at 42.5; the estimate carries on."""
    result, rows, samples = await _replay(monkeypatch, max_soc=45.0, stale_from="2026-11-15T17:00:00Z")

    stopping = _events(result, "stopping")
    assert [e["reason"] for e in stopping] == ["max_soc_estimated"]
    stop_i = _first_stop_index(result, rows)
    assert stop_i is not None
    assert rows[stop_i]["t"] < utc("2026-11-15T17:40:00Z").timestamp()
    assert 44.3 <= samples[stop_i]["estimate_k1"] <= 44.9
    # The report is stale (not just stalled) by the time of the stop.
    assert result.loop._soc_estimator.resolve(
        reported_pct=result.state.ev_soc_pct, reported_fresh=False, capacity_kwh=_CAPACITY_KWH, k=1.0
    ).source is (SocSource.ESTIMATED)
    assert all(r.commanded_w == 0.0 for r in result.records[stop_i:])


async def test_three_cycle_none_blip_does_not_change_the_estimate(monkeypatch):
    """SYNTHESISED blip: SOC and session energy are None for 3 cycles at 17:20Z (between two SOC changes)."""
    base, rows, base_samples = await _replay(monkeypatch)
    blip_i = next(i for i, row in enumerate(rows) if row["t"] >= utc("2026-11-15T17:20:00Z").timestamp())
    blipped, _, blip_samples = await _replay(monkeypatch, blip=(blip_i, blip_i + 3))

    assert not any(s["stalled"] for s in blip_samples)
    assert blip_samples[-1]["estimate_k1"] == base_samples[-1]["estimate_k1"]
    assert _events(blipped, "stopping") == []
    assert _events(base, "stopping") == []
