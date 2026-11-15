"""Self-tests of the eco replay harness (car models, clock, plant)."""

from __future__ import annotations

import pytest

from tests.unit.eco_replay_harness import (
    CAR_MODELS,
    CYCLE_S,
    IdealFollowerCar,
    IgnoringCar,
    LaggingCar,
    StepAmpCar,
    UnevenStepCar,
    constant_site,
    run_scenario,
    surplus_site,
)

V = 240.0


def test_step_car_4400_draws_4224_at_6a():
    cal, power, amps = StepAmpCar().respond(4400.0, V, 0)
    assert amps == 6.0
    assert power == pytest.approx(4224.0)
    assert all(5.7 < c < 6.1 for c in cal)  # ~6 A per phase, small spread like the recorded currents


@pytest.mark.parametrize("written_w", [4400.0, 4850.0, 4900.0, 4909.856, 5049.0])
def test_step_car_stays_at_6a_below_next_amp_step(written_w):
    car = StepAmpCar()
    assert car.amps(written_w, V) == 6
    assert car.respond(written_w, V, 0)[1] == pytest.approx(4224.0)


def test_step_car_next_steps():
    car = StepAmpCar()
    assert car.amps(5100.0, V) == 7
    assert car.amps(5800.0, V) == 8
    assert car.amps(30000.0, V) == 32  # clamped


def test_uneven_car_table():
    car = UnevenStepCar()
    assert [car.respond(w, V, 0)[2] for w in (4400, 5249, 5300, 7300, 22000)] == [6, 6, 8, 11, 32]


def test_lagging_car_follows_after_n_cycles():
    car = LaggingCar(lag=3, inner=IdealFollowerCar())
    powers = [car.respond(w, V, i)[1] for i, w in enumerate([4400, 4400, 4400, 4400, 8000, 8000, 8000, 8000, 8000])]
    assert powers == [4400, 4400, 4400, 4400, 4400, 4400, 4400, 8000, 8000]


def test_ignoring_car_stays_at_6a_then_wakes():
    car = IgnoringCar(wake_at=5)
    assert [car.respond(12000.0, V, i)[2] for i in range(4)] == [6.0] * 4
    assert car.respond(12000.0, V, 5)[1] == 12000.0


def test_surplus_site_energy_balance():
    site = surplus_site(6000.0)
    assert site(0, 4000.0)["grid_power_w"] == -2000.0
    over = site(0, 8000.0)
    assert over["solar_battery_power_w"] == -2000.0 and over["grid_power_w"] == 0.0


async def test_run_scenario_records_one_cycle_each_and_advances_clock(monkeypatch):
    result = await run_scenario(monkeypatch, n_cycles=7, site=constant_site(), ev_soc_pct=26.0)
    assert [r.i for r in result.records] == list(range(7))
    assert [b.t - a.t for a, b in zip(result.records, result.records[1:], strict=False)] == [CYCLE_S] * 6
    assert result.commanded[0] == 4440.0
    assert set(CAR_MODELS) == {"step", "uneven", "lagging", "ignoring", "ideal"}


@pytest.mark.asyncio
async def test_site_receives_current_cycle_car_power(monkeypatch):
    seen: list[tuple[int, float]] = []

    def site(i, car_power):
        seen.append((i, car_power))
        return {}

    class SetpointCar:
        def respond(self, written_w, volts, i):
            return (1.0, 1.0, 1.0), float(i + 1) * 100.0, 1.0

    await run_scenario(monkeypatch, n_cycles=4, site=site, car=SetpointCar())
    assert seen[:4] == [(0, 100.0), (1, 200.0), (2, 300.0), (3, 400.0)]
