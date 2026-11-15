"""Loader for the 2026-11-15 eco-day ramp incident fixtures (Home Assistant history exports).

HA rows only appear when a value changes, so every entity is forward-filled (zero-order hold) onto the
5 s control cycles. See tests/fixtures/incidents/2026-11-15-eco-min-charge/README.md.
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

from tests.unit.incident_data import Series, resample_cycles

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "incidents" / "2026-11-15-eco-min-charge"
CSV1_SITE = FIXTURE_DIR / "min_charge_on_flat_4400.csv"
CSV1_EV_SOC = FIXTURE_DIR / "min_charge_on_ev_soc_synthesised.csv"
CSV2_STALL = FIXTURE_DIR / "min_charge_off_stall_4910.csv"
CSV3_GOLDEN = FIXTURE_DIR / "golden_correct_ramp_2026-11-12.csv"

GRID_VOLTAGE_DEFAULT_V = 240.0  # CSV 2 omits the grid voltages (239.1-241.6 V throughout)
CORRECTION_FACTOR = 1.056  # correction_pct 5.6: HA EV current sensors are the calibrated currents

BATTERY_POWER = "sensor.reverse_battery_power"
BATTERY_SOC = "sensor.victronbattery_soc229"
EV_POWER = "sensor.ev_charger_power"
EV_SOC = "sensor.ev_charger_ev_soc"
RECORDED_SETPOINT = "sensor.ev_charger_charge_setpoint"


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def load_replay(*paths: Path) -> Series:
    """Merge fixture files into time-ordered numeric series. '#' comment rows and non-numeric states are skipped."""
    series: Series = {}
    for path in paths:
        with path.open(newline="") as fh:
            lines = (line for line in fh if not line.startswith("#"))
            for row in csv.DictReader(lines):
                try:
                    value = float(row["state"])
                except TypeError, ValueError:
                    continue
                series.setdefault(row["entity_id"], []).append((utc(row["last_changed"]), value))
    for points in series.values():
        points.sort(key=lambda p: p[0])
    return series


def cycles(series: Series, start: datetime, end: datetime, step_s: float = 5.0) -> list[dict[str, float | None]]:
    """Forward-filled per-cycle values. An entity is None before its first row."""
    rows = resample_cycles(series, start, end, step_s=step_s)
    # Before an entity's first row use its first recorded value (the exports start with a row at the window start).
    first = {e: pts[0][1] for e, pts in series.items()}
    for row in rows:
        for entity, value in first.items():
            if row.get(entity) is None:
                row[entity] = value
    return rows


def _v(row: dict, entity: str, default: float | None = None) -> float | None:
    value = row.get(entity)
    return default if value is None else value


def cycle_inputs(row: dict[str, float | None]) -> dict[str, float | None]:
    """Map one forward-filled cycle row to AppState site fields (signed grid current, + import / - export).

    ``sensor.reverse_battery_power`` is positive while the battery DISCHARGES (CSV 3: +2.5 kW while the home
    battery SOC falls 99.9 -> 93.8 %), so it is negated into ``solar_battery_power_w`` (+ charging).
    """
    grid_v = {}
    grid_i = {}
    ev_v = {}
    ev_i = {}
    for n, key in enumerate(("l1", "l2", "l3"), start=1):
        grid_v[n] = _v(row, f"sensor.victron_grid_{key}_voltage_30", GRID_VOLTAGE_DEFAULT_V)
        grid_i[n] = _v(row, f"sensor.victron_grid_{key}_current_30", 0.0)
        ev_v[n] = _v(row, f"sensor.ev_charger_ev_voltage_{key}", GRID_VOLTAGE_DEFAULT_V)
        ev_i[n] = _v(row, f"sensor.ev_charger_ev_current_{key}", 0.0)
    return {
        "grid_power_w": sum(grid_v[n] * grid_i[n] for n in (1, 2, 3)),  # + import, - export
        "solar_battery_power_w": None if row.get(BATTERY_POWER) is None else -row[BATTERY_POWER],
        "solar_battery_soc_pct": row.get(BATTERY_SOC),
        "victron_l1_current_a": grid_i[1],
        "victron_l2_current_a": grid_i[2],
        "victron_l3_current_a": grid_i[3],
        "victron_l1_voltage_v": grid_v[1],
        "victron_l2_voltage_v": grid_v[2],
        "victron_l3_voltage_v": grid_v[3],
        "ev_voltage_l1_v": ev_v[1],
        "ev_voltage_l2_v": ev_v[2],
        "ev_voltage_l3_v": ev_v[3],
        "ev_current_a": ev_i[1],
        "ev_current_b": ev_i[2],
        "ev_current_c": ev_i[3],
        "ev_current_raw_a": ev_i[1] / CORRECTION_FACTOR,
        "ev_current_raw_b": ev_i[2] / CORRECTION_FACTOR,
        "ev_current_raw_c": ev_i[3] / CORRECTION_FACTOR,
        "ev_active_power_w": _v(row, EV_POWER, 0.0),
        "ev_soc_pct": row.get(EV_SOC),
    }
