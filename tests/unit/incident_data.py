"""Helpers for loading recorded incident data (Home Assistant history CSV exports).

Stdlib only (pandas is not a project dependency).
"""

from __future__ import annotations

import csv
import math
from datetime import UTC, datetime
from pathlib import Path

INCIDENT_DIR = Path(__file__).parent.parent / "fixtures" / "incidents" / "2026-11-07-breaker-cap"
HA_HISTORY_CSV = INCIDENT_DIR / "ha_history_per_phase.csv"

Series = dict[str, list[tuple[datetime, float]]]


def _parse_ts(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def load_ha_history(path: Path = HA_HISTORY_CSV) -> Series:
    """Load an HA history export (entity_id,state,last_changed) into time-ordered numeric series.

    Non-numeric states such as 'unavailable' / 'unknown' are skipped.
    """
    series: Series = {}
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                value = float(row["state"])
            except TypeError, ValueError:
                continue
            series.setdefault(row["entity_id"], []).append((_parse_ts(row["last_changed"]), value))
    for points in series.values():
        points.sort(key=lambda p: p[0])
    return series


def resample_cycles(
    series: Series, start: datetime, end: datetime, step_s: float = 10.0
) -> list[dict[str, float | None]]:
    """Zero-order-hold resample to control cycles. Value is None before an entity's first sample."""
    cycles: list[dict[str, float | None]] = []
    cursors = dict.fromkeys(series, 0)
    last: dict[str, float | None] = dict.fromkeys(series)
    t = start
    while t <= end:
        for entity, points in series.items():
            idx = cursors[entity]
            while idx < len(points) and points[idx][0] <= t:
                last[entity] = points[idx][1]
                idx += 1
            cursors[entity] = idx
        cycles.append({"t": t.timestamp(), **last})
        t = datetime.fromtimestamp(t.timestamp() + step_s, tz=UTC)
    return cycles


def signed_current(current_a: float, power_w: float) -> float:
    """Reconstruct the Victron's SIGNED grid current from the EM540's unsigned current and signed power.

    The Home Assistant EM540 export reports unsigned current magnitudes, while the Victron proxies signed
    per-phase current (+ import, - export). The sign is taken from the real power of the same phase.
    """
    return math.copysign(current_a, power_w)
