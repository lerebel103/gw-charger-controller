"""Victron GX Modbus TCP client for reading grid, battery, and voltage data."""

from __future__ import annotations

import logging
import struct

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from app.backoff import exponential_backoff
from app.state import AppState

logger = logging.getLogger(__name__)

# Victron system service unit ID (com.victronenergy.system)
_SYSTEM_UNIT_ID = 100

# System registers (unit ID 100)
_REG_GRID_L1_POWER = 820
_REG_GRID_L2_POWER = 821
_REG_GRID_L3_POWER = 822
_REG_BATTERY_POWER = 842
_REG_BATTERY_SOC = 843

# Grid meter voltage registers (unit ID from state.victron_grid_meter_unit_id)
_REG_GRID_L1_VOLTAGE = 2616
_REG_GRID_L2_VOLTAGE = 2618
_REG_GRID_L3_VOLTAGE = 2620


def _uint16_to_int16(value: int) -> int:
    """Convert an unsigned 16-bit register value to a signed int16."""
    return struct.unpack(">h", struct.pack(">H", value))[0]


class VictronModbusClient:
    """Modbus TCP client for the Victron GX device.

    Provides ``ensure_connected()`` and ``read()`` methods called by the
    control loop each iteration.  Does not run its own async task.
    """

    def __init__(self, state: AppState) -> None:
        self._state = state
        self._client: AsyncModbusTcpClient | None = None
        self._connected_ip: str = ""
        self._connected_port: int = 0
        self._reconnect_attempt: int = 0
        self._reconnect_after: float = 0.0  # monotonic time to wait until

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.connected

    async def ensure_connected(self) -> None:
        """Check connection and reconnect if needed. Non-blocking single attempt."""
        if self.connected and not self._config_changed():
            return

        if self._config_changed():
            await self._close()

        ip = self._state.victron_ip
        port = self._state.victron_port
        if not ip:
            return

        # Respect backoff timing
        import time as _t
        now = _t.monotonic()
        if now < self._reconnect_after:
            return

        try:
            client = AsyncModbusTcpClient(ip, port=port)
            connected = await client.connect()
            if connected:
                self._client = client
                self._connected_ip = ip
                self._connected_port = port
                self._reconnect_attempt = 0
                logger.info("Connected to Victron GX at %s:%d", ip, port)
            else:
                self._schedule_retry()
        except (OSError, ModbusException) as exc:
            logger.warning("Victron GX connection failed: %s", exc)
            self._schedule_retry()

    async def read(self) -> None:
        """Read all registers and update AppState. Closes connection on error."""
        if not self.connected:
            return
        try:
            await self._read_registers()
        except (ModbusException, OSError) as exc:
            logger.warning("Victron GX read failed: %s", exc)
            await self._close()

    async def reconnect(self) -> None:
        """Force a reconnect (e.g. after IP/port change via MQTT)."""
        await self._close()
        self._reconnect_attempt = 0
        self._reconnect_after = 0.0

    def _config_changed(self) -> bool:
        return (
            self._state.victron_ip != self._connected_ip
            or self._state.victron_port != self._connected_port
        )

    def _schedule_retry(self) -> None:
        import time as _t
        delay = exponential_backoff(self._reconnect_attempt)
        self._reconnect_after = _t.monotonic() + delay
        self._reconnect_attempt += 1

    async def _close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    async def _read_registers(self) -> None:
        """Read system and grid meter registers and update AppState."""
        assert self._client is not None  # noqa: S101

        grid_resp = await self._client.read_holding_registers(
            address=_REG_GRID_L1_POWER, count=3, slave=_SYSTEM_UNIT_ID
        )
        if grid_resp.isError():
            raise ModbusException(f"Grid power read error: {grid_resp}")

        batt_resp = await self._client.read_holding_registers(
            address=_REG_BATTERY_POWER, count=2, slave=_SYSTEM_UNIT_ID
        )
        if batt_resp.isError():
            raise ModbusException(f"Battery read error: {batt_resp}")

        grid_l1 = _uint16_to_int16(grid_resp.registers[0])
        grid_l2 = _uint16_to_int16(grid_resp.registers[1])
        grid_l3 = _uint16_to_int16(grid_resp.registers[2])
        battery_power = _uint16_to_int16(batt_resp.registers[0])
        battery_soc = batt_resp.registers[1]

        self._state.grid_power_w = float(grid_l1 + grid_l2 + grid_l3)
        self._state.solar_battery_power_w = float(battery_power)
        self._state.solar_battery_soc_pct = float(battery_soc)

        grid_meter_unit = self._state.victron_grid_meter_unit_id

        v1_resp = await self._client.read_holding_registers(
            address=_REG_GRID_L1_VOLTAGE, count=1, slave=grid_meter_unit
        )
        if v1_resp.isError():
            raise ModbusException(f"Grid L1 voltage read error: {v1_resp}")

        v2_resp = await self._client.read_holding_registers(
            address=_REG_GRID_L2_VOLTAGE, count=1, slave=grid_meter_unit
        )
        if v2_resp.isError():
            raise ModbusException(f"Grid L2 voltage read error: {v2_resp}")

        v3_resp = await self._client.read_holding_registers(
            address=_REG_GRID_L3_VOLTAGE, count=1, slave=grid_meter_unit
        )
        if v3_resp.isError():
            raise ModbusException(f"Grid L3 voltage read error: {v3_resp}")

        self._state.victron_l1_voltage_v = v1_resp.registers[0] / 10.0
        self._state.victron_l2_voltage_v = v2_resp.registers[0] / 10.0
        self._state.victron_l3_voltage_v = v3_resp.registers[0] / 10.0
