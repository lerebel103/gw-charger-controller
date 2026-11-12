"""Self-tests for the incident CSV loader."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.unit.incident_data import HA_HISTORY_CSV, load_ha_history, resample_cycles


def test_fixture_files_exist():
    assert HA_HISTORY_CSV.exists()
    assert (HA_HISTORY_CSV.parent / "README.md").exists()
    assert (HA_HISTORY_CSV.parent / "grafana_power_overview.csv").exists()


def test_load_ha_history_is_ordered_and_numeric():
    series = load_ha_history()
    power_l1 = series["sensor.em540_energy_meter_bridge_power_l1"]
    assert len(power_l1) > 1000
    assert [t for t, _ in power_l1] == sorted(t for t, _ in power_l1)
    assert power_l1[0][0].tzinfo is not None


def test_load_skips_non_numeric_states(tmp_path):
    path = tmp_path / "h.csv"
    path.write_text(
        "entity_id,state,last_changed\n"
        "sensor.a,1.5,2026-11-07T06:00:00.000Z\n"
        "sensor.a,unavailable,2026-11-07T06:00:05.000Z\n"
        "sensor.a,3,2026-11-07T06:00:10.000Z\n"
    )
    assert [v for _, v in load_ha_history(path)["sensor.a"]] == [1.5, 3.0]


def test_resample_forward_fills_and_none_before_first_sample(tmp_path):
    path = tmp_path / "h.csv"
    path.write_text(
        "entity_id,state,last_changed\nsensor.a,1,2026-11-07T06:00:03.000Z\nsensor.a,2,2026-11-07T06:00:14.000Z\n"
    )
    start = datetime(2026, 11, 7, 6, 0, 0, tzinfo=UTC)
    end = datetime(2026, 11, 7, 6, 0, 30, tzinfo=UTC)
    cycles = resample_cycles(load_ha_history(path), start, end, step_s=10.0)
    assert [c["sensor.a"] for c in cycles] == [None, 1.0, 2.0, 2.0]
    assert cycles[1]["t"] - cycles[0]["t"] == pytest.approx(10.0)
