"""History-22 replay and cloud-outage simulation for the EV SOC estimator.

Recorded (tests/fixtures/incidents/2026-11-01-ev-soc-history-22/): EV SOC, session energy and capacity of six
sessions, three of them real charges (S2 8.4 kWh at 4 kW, S4 11.1 kWh at 7.2 kW, S6 7.4 kWh at 4.3 kW).
SIMULATED (tests/unit/soc_sim.py): the outage start, the freeze (the report stops changing while HA keeps
republishing it) or stale (no messages at all) mode, the limit, the pre-learned k and the tail past the last
recorded SOC step. "True SOC" is the recorded SOC interpolated between steps, so the envelopes below are a lower
bound on real-world spread (add about +-0.25 to 0.4 pp for the 0.5 pp quantisation and cloud lag).
Error = true SOC at the stop minus the limit (negative = stopped short).

Known limitations, by design for now (the user accepted a small overshoot, no app change):
- F1, margin applied late: the 0.5 pp margin is only applied once the estimate is the SOC source (stalled or
  stale). An outage that starts within about 1.5 pp of the limit stops at estimate >= limit with no margin, so every
  overshoot happens there (worst observed +0.15 pp warm, +0.28 pp with a badly learned cold k).
- F2, cold-start k span: ``observe()`` takes the first SOC it ever sees as the start of the k learning span. When the
  app starts with the car already plugged in, k is learned slightly high (0.998 / 1.087 / 1.026 for S2 / S4 / S6
  against a true 1.055 / 1.036 / 1.019), which makes the estimate low (the unsafe direction). The cold variants
  below only assert the same overshoot bound, they do not assert that k is right.
"""

from __future__ import annotations

import pytest

from app.control.constants import _SOC_K_MAX, _SOC_K_MIN
from tests.unit import soc_sim as sim

REAL = {"S2": 2, "S4": 4, "S6": 6}  # indices into segment_sessions(); 0 is a stray 5900 -> 1000 Wh drop
LIMIT_ENVELOPE = (-1.0, 0.35)  # true SOC at the stop minus the limit (pp)
PRELEARNED_K = 1.045  # about the k the estimator learns on these sessions
K_BAD = 1.0


@pytest.fixture(autouse=True)
def _fake_clock(monkeypatch):
    sim.install_clock(monkeypatch)


@pytest.fixture(scope="module")
def timelines() -> dict[str, sim.Timeline]:
    sessions = sim.history22()[2]
    return {name: sim.Timeline(sessions[i]) for name, i in REAL.items()}


def _limits(tl: sim.Timeline) -> list[float]:
    """80 plus the limits at 50 % and 75 % of the recorded span (rounded to 0.5), kept inside the session."""
    lo, hi = tl.rep_v[0], tl.rep_v[-1]
    inner = {round((lo + f * (hi - lo)) * 2) / 2 for f in (0.5, 0.75)}
    return sorted({80.0} | {x for x in inner if lo + 1.5 < x < 80.0})


# 1. Segmentation ----------------------------------------------------------------------------------


def test_segmentation_finds_three_real_charges_and_resets_energy():
    soc, energy, sessions = sim.history22()
    assert [round(s.end_wh) for s in sessions] == [1000, 300, 8400, 700, 11100, 300, 7400]
    assert all(b.start_t > a.end_t for a, b in zip(sessions, sessions[1:], strict=False))
    real = [sessions[i] for i in REAL.values()]
    assert [round(s.end_wh / 1000, 1) for s in real] == [8.4, 11.1, 7.4]
    mean_kw = [s.end_wh / 1000 / ((s.end_t - s.start_t) / 3600) for s in real]
    assert mean_kw == pytest.approx([4.0, 7.2, 4.3], abs=0.15)
    # one SOC series covering the three charges, 0.5 pp steps, 82 kWh capacity
    steps = {round(abs(b[1] - a[1]), 2) for a, b in zip(soc, soc[1:], strict=False) if a[1] != b[1]}
    assert 0.5 in steps


def test_timeline_matches_recorded_soc(timelines):
    expected = {"S2": (70.5, 80.0), "S4": (68.0, 80.0), "S6": (70.0, 78.0)}
    for name, tl in timelines.items():
        assert (tl.rep_v[0], tl.rep_v[-1]) == expected[name]
        assert all(b > a for a, b in zip(tl.rep_t, tl.rep_t[1:], strict=False))


# 2. Healthy replays -------------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(REAL))
def test_healthy_replay_never_stalls_and_estimate_tracks_the_report(timelines, name):
    tl = timelines[name]
    out = sim.run(tl, 100.0, 1.0, until=tl.end)
    assert out.reason is None
    assert not any(stalled for *_, stalled in out.trace)
    leads = [est - rep for t, rep, est, _ in out.trace if est is not None and tl.rep_t[0] <= t <= tl.rep_t[-1]]
    assert leads
    assert min(leads) >= -0.01  # the estimate is never behind the reported step
    assert max(leads) <= 1.0  # measured 0.61 here, 0.98 pp worst case at 7 kW with a coarser step cadence


@pytest.mark.parametrize("name", list(REAL))
def test_k_learned_from_default_converges_within_clamp(timelines, name):
    tl = timelines[name]
    out = sim.run(tl, 100.0, 1.0, until=tl.end)
    assert 1.01 <= out.k_final <= 1.06
    assert _SOC_K_MIN <= out.k_final <= _SOC_K_MAX


def test_k_carries_over_warm_between_sessions(timelines):
    k = 1.0
    finals = []
    for name in ("S2", "S4", "S6"):
        tl = timelines[name]
        out = sim.run(tl, 100.0, k, until=tl.end)
        assert not any(stalled for *_, stalled in out.trace)
        finals.append(out.k_final)
        k = out.k_final
    assert all(1.01 <= f <= 1.06 for f in finals)
    assert max(finals) - min(finals) <= 0.04


# 3. Outage envelopes ------------------------------------------------------------------------------


def _sweep(tl: sim.Timeline, *, mode: str, k0: float | None, cold: bool, limits: list[float]):
    rows = []
    for limit in limits:
        for outage_t in tl.outage_starts(step_every=2):
            out = sim.run(tl, limit, k0, mode, outage_t, cold=cold)
            if out.stop_t is not None and out.stop_t < outage_t:
                continue  # the healthy session had already stopped at this limit before the outage began
            rows.append((limit, outage_t, out))
    return rows


@pytest.mark.parametrize("name", list(REAL))
@pytest.mark.parametrize("mode", ["freeze", "stale"])
@pytest.mark.parametrize("k0", [K_BAD, PRELEARNED_K], ids=["k1.0", "k_prelearned"])
@pytest.mark.parametrize("cold", [False, True], ids=["warm", "cold"])
def test_outage_stops_close_to_the_limit(timelines, name, mode, k0, cold):
    tl = timelines[name]
    rows = _sweep(tl, mode=mode, k0=k0, cold=cold, limits=_limits(tl))
    assert len(rows) >= 10
    low, high = LIMIT_ENVELOPE
    for limit, outage_t, out in rows:
        where = f"{name} {mode} limit={limit} outage at +{outage_t - tl.start:.0f}s"
        assert out.reason == "max_soc_estimated", where
        err = out.true_soc - limit
        assert low <= err <= high, f"{where}: error {err:+.2f} pp"
        if err > 0.02:
            # F1: overshoot only when the outage starts within ~1.5 pp of the limit (margin not yet applied)
            assert limit - tl.true(outage_t) <= 1.6, f"{where}: overshoot {err:+.2f} pp far from the limit"


def test_limit_80_example_stops_short_of_80_when_warm(timelines):
    """The user's example (limit 80 %): measured 79.2 to 80.1 % true SOC at the stop, median about 0.4 pp short."""
    errors = []
    for tl in timelines.values():
        for mode in ("freeze", "stale"):
            for limit, _t, out in _sweep(tl, mode=mode, k0=PRELEARNED_K, cold=False, limits=[80.0]):
                errors.append(out.true_soc - limit)
    assert len(errors) >= 40
    assert -0.9 <= min(errors)
    assert max(errors) <= 0.2
    assert sorted(errors)[len(errors) // 2] < -0.1


def test_overshoot_only_near_the_limit_for_freeze(timelines):
    """F1: far from the limit (outage > 1.6 pp below it) the margin always holds the stop short of the limit."""
    far = []
    for tl in timelines.values():
        for limit, outage_t, out in _sweep(tl, mode="freeze", k0=K_BAD, cold=False, limits=[80.0]):
            if limit - tl.true(outage_t) > 1.6:
                far.append(out.true_soc - limit)
    assert far
    assert max(far) < 0.0


# 4. Freeze detection ------------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(REAL))
def test_freeze_is_flagged_after_about_1_3_kwh(timelines, name):
    tl = timelines[name]
    checked = 0
    for outage_t in tl.outage_starts(step_every=2)[::2]:
        out = sim.run(tl, 100.0, PRELEARNED_K, "freeze", outage_t)
        assert out.first_stall_t is not None, f"{name}: freeze at +{outage_t - tl.start:.0f}s never flagged"
        kwh = (tl.energy(out.first_stall_t) - tl.energy(tl.last_change_before(outage_t))) / 1000
        assert 1.0 <= kwh <= 1.7, f"{name}: stall after {kwh:.2f} kWh"
        checked += 1
    assert checked >= 4


# 5. Cross-check through the real ControlLoop -----------------------------------------------------------------

LOOP_CASES = [
    ("S2", "freeze", 80.0, None, 0.55),
    ("S2", "stale", 77.5, PRELEARNED_K, 0.45),
    ("S4", "freeze", 80.0, PRELEARNED_K, 0.5),
    ("S4", "stale", 80.0, None, 0.4),
    ("S6", "freeze", 77.5, None, 0.6),
    ("S6", "stale", 75.0, PRELEARNED_K, 0.5),
]


@pytest.mark.parametrize(("name", "mode", "limit", "k0", "frac"), LOOP_CASES)
async def test_control_loop_stops_on_the_estimate_like_the_driver(monkeypatch, timelines, name, mode, limit, k0, frac):
    from tests.unit.eco_replay_harness import constant_site, run_scenario, user_state

    tl = timelines[name]
    outage_t = tl.start + frac * (tl.end - tl.start)
    t0 = tl.start + 10
    n = int((tl.all_t[-1] - t0) / sim.CYCLE_S)
    frozen = tl.reported(outage_t)
    ref = sim.run(tl, limit, k0, mode, outage_t, cold=True)  # the harness starts without a SOC anchor (cold)

    def ev_fields(i, state):
        t = t0 + i * sim.CYCLE_S
        return {
            "ev_soc_pct": frozen if t >= outage_t else tl.reported(t),
            "ev_session_energy_wh": tl.energy(t),
            "ev_active_power_w": 4000.0,
            "ev_connected": True,
        }

    def on_cycle(i, state, loop):
        if i == 0:
            loop._config_manager = None

    state = user_state(charge_mode="Manual", ev_max_soc_pct=limit, ev_min_soc_pct=0.0, ev_battery_capacity_kwh=82.0)
    if k0:
        state.ev_soc_k = k0
        state.ev_soc_k_capacity_kwh = 82.0
    result = await run_scenario(
        monkeypatch,
        n_cycles=n,
        site=constant_site(),
        ev_fields=ev_fields,
        state=state,
        ev_soc_pct=tl.reported(t0),
        on_cycle=on_cycle,
        cycle_s=sim.CYCLE_S,
        soc_republish=(lambda i: t0 + i * sim.CYCLE_S < outage_t) if mode == "stale" else None,
    )
    queue = result.loop._publish_queue
    stopping = []
    while not queue.empty():
        item = queue.get_nowait()
        if isinstance(item, dict) and item.get("event") == "stopping":
            stopping.append(item)
    assert [e["reason"] for e in stopping] == ["max_soc_estimated"]
    stop_i = next(r.i for r in result.records if r.commanded_w == 0.0)
    t_stop = t0 + stop_i * sim.CYCLE_S
    assert ref.reason == "max_soc_estimated"
    assert abs(t_stop - ref.stop_t) <= 2 * sim.CYCLE_S
    low, high = LIMIT_ENVELOPE
    assert low <= tl.true(t_stop) - limit <= high
    assert all(r.commanded_w == 0.0 for r in result.records[stop_i:])
