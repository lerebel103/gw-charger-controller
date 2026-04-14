"""State package exports.

Keeps the public import surface stable: `from app.state import ...`.
"""

from __future__ import annotations

from app.state.enums import AdvancedChargingMode, ChargerStatus, PlugAndChargeAutoStart, SinglePhaseSwitching
from app.state.models import PERSISTED_FIELDS, AppState, StateSnapshot

__all__ = [
    "AppState",
    "PERSISTED_FIELDS",
    "StateSnapshot",
    "AdvancedChargingMode",
    "ChargerStatus",
    "PlugAndChargeAutoStart",
    "SinglePhaseSwitching",
]
