"""Home Assistant device metadata helpers."""

from typing import Any

from app.state import AppState
from app.version import __version__

_DEVICE_BASE = {
    "identifiers": ["ev_charger_integration"],
    "name": "EV Charger",
    "model": "GW22K-HCA-20",
    "manufacturer": "lerebel103",
    "sw_version": __version__,
}


def device_payload(state: AppState) -> dict[str, Any]:
    """Build Home Assistant device payload, including serial when known."""
    payload = dict(_DEVICE_BASE)
    if state.ev_serial_number:
        payload["serial_number"] = state.ev_serial_number
    return payload
