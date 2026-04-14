"""Time helpers used by the control loop."""

from __future__ import annotations

import logging
import re
from datetime import datetime, time

from app.state import AppState

logger = logging.getLogger(__name__)

_HHMM_RE = re.compile(r"^(\d{1,2}):([0-5]\d)$")


def validate_hhmm(s: str) -> bool:
    """Return True iff *s* is a valid time in H:MM or HH:MM format."""
    m = _HHMM_RE.match(s)
    if not m:
        return False
    hour, minute = int(m.group(1)), int(m.group(2))
    return 0 <= hour <= 23 and 0 <= minute <= 59


def normalise_hhmm(s: str) -> str:
    """Normalise a valid H:MM or HH:MM string to HH:MM."""
    h, m = s.split(":")
    return f"{int(h):02d}:{m}"


def parse_hhmm(s: str) -> time:
    """Parse an H:MM or HH:MM string into a datetime.time."""
    h, m = s.split(":")
    return time(int(h), int(m))


def is_within_discharge_window(state: AppState) -> bool:
    """Return True if the current local time is within [start, end)."""
    if not (validate_hhmm(state.solar_battery_discharge_start) and validate_hhmm(state.solar_battery_discharge_end)):
        return False

    start = parse_hhmm(state.solar_battery_discharge_start)
    end = parse_hhmm(state.solar_battery_discharge_end)
    now = datetime.now().time()  # noqa: DTZ005

    result = start <= now < end if start <= end else now >= start or now < end

    logger.debug(
        "Discharge window check: now=%s start=%s end=%s -> %s",
        now.strftime("%H:%M:%S"),
        state.solar_battery_discharge_start,
        state.solar_battery_discharge_end,
        result,
    )
    return result
