"""State enums for EV charger integration."""

from __future__ import annotations

from enum import IntEnum


class ChargerStatus(IntEnum):
    """GW22K-HCA-20 charger status codes (register 10017)."""

    IDLE_NO_CONNECTOR = 0
    IDLE_CONNECTOR_PLUGGED = 1
    HANDSHAKING_WITH_VEHICLE = 2
    CHARGING_IN_PROGRESS = 3
    CHARGING_COMPLETED = 4
    ABNORMAL_ALARM = 5
    SCHEDULED_START = 6
    MAINTENANCE = 7
    START_FAILED = 8
    SYSTEM_UPGRADE_IN_PROGRESS = 9
    CHARGING_INTERRUPTED_INSUFFICIENT_PV_BATTERY = 10
    UNKNOWN = 255

    @classmethod
    def from_register(cls, value: int | None) -> ChargerStatus | None:
        """Convert register value to ChargerStatus enum."""
        if value is None:
            return None
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN

    @property
    def display_name(self) -> str:
        """Human-readable name for the status."""
        names = {
            0: "Idle (no connector plugged)",
            1: "Idle (connector plugged)",
            2: "Handshaking with vehicle",
            3: "Charging in progress",
            4: "Charging completed",
            5: "Abnormal alarm",
            6: "Scheduled start",
            7: "Maintenance",
            8: "Start failed",
            9: "System upgrade in progress",
            10: "Charging interrupted (insufficient PV/battery power)",
            255: "Unknown",
        }
        return names.get(self.value, f"Unknown ({self.value})")

    @classmethod
    def ha_options(cls) -> list[str]:
        """Return Home Assistant enum options in stable declaration order."""
        return [status.display_name for status in cls]


class AdvancedChargingMode(IntEnum):
    """GW22K-HCA-20 advanced charging mode (register 10032)."""

    FAST_CHARGING = 0
    PV_CHARGING = 1
    PV_BATTERY_HYBRID_CHARGING = 2
    UNKNOWN = 255

    @classmethod
    def from_register(cls, value: int | None) -> AdvancedChargingMode | None:
        """Convert register value to AdvancedChargingMode enum."""
        if value is None:
            return None
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN

    @property
    def display_name(self) -> str:
        names = {
            0: "Fast charging",
            1: "PV charging",
            2: "PV + battery hybrid charging",
            255: "Unknown",
        }
        return names.get(self.value, f"Unknown ({self.value})")

    @classmethod
    def ha_options(cls) -> list[str]:
        """Return Home Assistant enum options in stable declaration order."""
        return [mode.display_name for mode in cls if mode != cls.UNKNOWN]

    @classmethod
    def from_display_name(cls, value: str) -> AdvancedChargingMode | None:
        """Map Home Assistant display label to enum value."""
        for mode in cls:
            if mode.display_name == value:
                return mode
        return None


class PlugAndChargeAutoStart(IntEnum):
    """GW22K-HCA-20 plug-and-charge auto start (register 10019)."""

    OFF = 0
    ON = 1
    UNKNOWN = 255

    @classmethod
    def from_register(cls, value: int | None) -> PlugAndChargeAutoStart | None:
        """Convert register value to PlugAndChargeAutoStart enum."""
        if value is None:
            return None
        if value == 2:
            return cls.ON
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN

    @property
    def display_name(self) -> str:
        names = {
            0: "Off",
            1: "On",
            255: "Unknown",
        }
        return names.get(self.value, f"Unknown ({self.value})")

    @classmethod
    def ha_options(cls) -> list[str]:
        """Return Home Assistant enum options in stable declaration order."""
        return [value.display_name for value in cls if value != cls.UNKNOWN]

    @classmethod
    def from_display_name(cls, value: str) -> PlugAndChargeAutoStart | None:
        """Map Home Assistant display label to enum value."""
        for item in cls:
            if item.display_name == value:
                return item
        return None


class SinglePhaseSwitching(IntEnum):
    """GW22K-HCA-20 single phase switching (register 10023)."""

    DISABLED = 0
    ENABLED = 1
    UNKNOWN = 255

    @classmethod
    def from_register(cls, value: int | None) -> SinglePhaseSwitching | None:
        """Convert register value to SinglePhaseSwitching enum."""
        if value is None:
            return None
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN

    @property
    def display_name(self) -> str:
        names = {
            0: "Disabled",
            1: "Enabled",
            255: "Unknown",
        }
        return names.get(self.value, f"Unknown ({self.value})")

    @classmethod
    def from_switch_payload(cls, value: str) -> SinglePhaseSwitching | None:
        """Map Home Assistant switch payload to enum value."""
        if value == "ON":
            return cls.ENABLED
        if value == "OFF":
            return cls.DISABLED
        return None
