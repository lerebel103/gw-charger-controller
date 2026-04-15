"""Payload parsing helpers for Home Assistant runtime commands."""

from app.state import AdvancedChargingMode, PlugAndChargeAutoStart, SinglePhaseSwitching


def parse_advanced_mode_payload(payload: str) -> AdvancedChargingMode | None:
    """Parse advanced charging mode payload from HA label or raw register value string."""
    payload_clean = str(payload).strip()
    mode = AdvancedChargingMode.from_display_name(payload_clean)
    if mode is None:
        normalized = payload_clean.casefold()
        for candidate in AdvancedChargingMode:
            if candidate.display_name.casefold() == normalized:
                mode = candidate
                break
    if mode is not None and mode != AdvancedChargingMode.UNKNOWN:
        return mode
    try:
        raw = int(payload_clean)
    except (TypeError, ValueError):
        return None
    mode = AdvancedChargingMode.from_register(raw)
    if mode in (None, AdvancedChargingMode.UNKNOWN):
        return None
    return mode


def parse_plug_and_charge_payload(payload: str) -> PlugAndChargeAutoStart | None:
    """Parse plug-and-charge payload from HA label or raw register value string."""
    payload_clean = str(payload).strip()
    upper = payload_clean.upper()
    if upper == "ON":
        return PlugAndChargeAutoStart.ON
    if upper == "OFF":
        return PlugAndChargeAutoStart.OFF
    mode = PlugAndChargeAutoStart.from_display_name(payload_clean)
    if mode is None:
        normalized = payload_clean.casefold()
        for candidate in PlugAndChargeAutoStart:
            if candidate.display_name.casefold() == normalized:
                mode = candidate
                break
    if mode is not None and mode != PlugAndChargeAutoStart.UNKNOWN:
        return mode
    try:
        raw = int(payload_clean)
    except (TypeError, ValueError):
        return None
    mode = PlugAndChargeAutoStart.from_register(raw)
    if mode in (None, PlugAndChargeAutoStart.UNKNOWN):
        return None
    return mode


def parse_single_phase_payload(payload: str) -> SinglePhaseSwitching | None:
    """Parse single-phase switching payload from HA switch text or numeric value."""
    payload_clean = str(payload).strip()
    mode = SinglePhaseSwitching.from_switch_payload(payload_clean.upper())
    if mode is not None:
        return mode
    try:
        raw = int(payload_clean)
    except (TypeError, ValueError):
        return None
    return SinglePhaseSwitching.from_register(raw)


def parse_max_grid_power_draw_payload(payload: str) -> float | None:
    """Parse max grid power draw payload in watts (physical range 4200-22000 W)."""
    try:
        value = float(str(payload).strip())
    except (TypeError, ValueError):
        return None
    if not (4200.0 <= value <= 22000.0):
        return None
    return round(value / 100.0) * 100.0
