"""Loader tests for the 2026-11-15 incident fixtures."""

from __future__ import annotations

import pytest

from tests.unit.replay_data import (
    CSV1_EV_SOC,
    CSV1_SITE,
    CSV2_STALL,
    EV_SOC,
    cycle_inputs,
    cycles,
    load_replay,
    utc,
)

START = utc("2026-11-15T05:50:00Z")
END = utc("2026-11-15T07:06:00Z")


def test_forward_fill_holds_value_between_rows():
    series = load_replay(CSV2_STALL)
    rows = cycles(series, utc("2026-11-15T07:19:00Z"), utc("2026-11-15T07:19:30Z"), step_s=5.0)
    battery = [r["sensor.reverse_battery_power"] for r in rows]
    # recorded: 606 @07:19:00, 526 @07:19:08.5, 403 @07:19:13.5, 247 @07:19:16.2, 134 @07:19:19.5, 97 @07:19:24.5
    assert battery == [606.0, 606.0, 526.0, 403.0, 134.0, 97.0, 97.0]


def test_csv2_comment_rows_are_skipped():
    assert "sensor.ev_charger_charge_setpoint" in load_replay(CSV2_STALL)


def test_grid_voltage_defaults_to_240_when_omitted():
    row = cycles(load_replay(CSV2_STALL), utc("2026-11-15T07:19:00Z"), utc("2026-11-15T07:19:00Z"))[0]
    inputs = cycle_inputs(row)
    assert inputs["victron_l1_voltage_v"] == 240.0
    assert inputs["ev_current_c"] == pytest.approx(6.1248)
    assert inputs["ev_current_raw_a"] == pytest.approx(5.7)


def test_synthesised_ev_soc_series():
    series = load_replay(CSV1_SITE, CSV1_EV_SOC)
    rows = {r["t"]: r for r in cycles(series, START, END)}
    at = lambda iso: rows[utc(iso).timestamp()][EV_SOC]  # noqa: E731
    assert at("2026-11-15T05:51:00Z") == 26.0
    assert at("2026-11-15T05:52:05Z") == 25.5
    assert at("2026-11-15T07:03:05Z") == 31.5


def test_grid_power_sign_negative_when_exporting():
    series = load_replay(CSV1_SITE, CSV1_EV_SOC)
    row = cycles(series, utc("2026-11-15T06:00:00Z"), utc("2026-11-15T06:00:00Z"))[0]
    inputs = cycle_inputs(row)
    assert row["sensor.victron_grid_l1_current_30"] < 0
    assert inputs["grid_power_w"] < -1400.0  # exporting, meets the eco start threshold
