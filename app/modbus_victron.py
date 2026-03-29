"""Victron GX Modbus TCP client for reading grid, battery, and voltage data."""

from __future__ import annotations

import asyncio
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
    """Async Modbus TCP client for the Victron GX device.

    Reads grid power (L1+L2+L3), battery power, battery SOC, and grid
    voltages at a configurable interval.  Reconnects with exponential
    backoff on failure and when the target IP/port changes.
    """

    def __init__(self, state: AppState, poll_interval_s: float = 5.0) -> None:
        self._state = state
        self._poll_interval_s = poll_interval_s
        self._client: AsyncModbusTcpClient | None = None
        self._connected_ip: str = ""
        self._connected_port: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def poll_loop(self) -> None:
        """Run the polling loop as an asyncio task."""
        while True:
            # Reconnect if IP/port changed or not connected
            if self._needs_reconnect():
                await self.reconnect()

            if self._client is not None and self._client.connected:
                try:
                    await self._read_registers()
                except (ModbusException, OSError) as exc:
                    logger.warning("Victron GX read failed: %s", exc)
                    await self._close()
                    await self.reconnect()

            await asyncio.sleep(self._poll_interval_s)

    async def reconnect(self) -> None:
        """Close existing connection and reconnect with exponential backoff."""
        await self._close()

        ip = self._state.victron_ip
        port = self._state.victron_port

        if not ip:
            logger.warning("Victron GX IP not configured, skipping connection")
            return

        attempt = 0
        while True:
            try:
                client = AsyncModbusTcpClient(ip, port=port)
                connected = await client.connect()
                if connected:
                    self._client = client
                    self._connected_ip = ip
                    self._connected_port = port
                    logger.info("Connected to Victron GX at %s:%d", ip, port)
                    return
                else:
                    logger.warning("Victron GX connection to %s:%d returned False", ip, port)
            except (OSError, ModbusException) as exc:
                logger.warning(
                    "Victron GX connection attempt %d to %s:%d failed: %s",
                    attempt,
                    ip,
                    port,
                    exc,
                )

            delay = exponential_backoff(attempt)
            logger.info("Retrying Victron GX connection in %.1f s", delay)
            await asyncio.sleep(delay)
            attempt += 1

            # If IP/port changed while we were waiting, restart with new target
            if self._state.victron_ip != ip or self._state.victron_port != port:
                ip = self._state.victron_ip
                port = self._state.victron_port
                attempt = 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _needs_reconnect(self) -> bool:
        """Check whether a reconnect is needed."""
        if self._client is None or not self._client.connected:
            return True
        return self._state.victron_ip != self._connected_ip or self._state.victron_port != self._connected_port

    async def _close(self) -> None:
        """Close the current Modbus connection if open."""
        if self._client is not None:
            self._client.close()
            self._client = None

    async def _read_registers(self) -> None:
        """Read system and grid meter registers and update AppState."""
        assert self._client is not None  # noqa: S101

        # --- System registers (unit ID 100) ---
        # Read 820..822 (3 registers) for grid power
        grid_resp = await self._client.read_holding_registers(
            address=_REG_GRID_L1_POWER, count=3, slave=_SYSTEM_UNIT_ID
        )
        if grid_resp.isError():
            raise ModbusException(f"Grid power read error: {grid_resp}")

        # Read 842..843 (2 registers) for battery power + SOC
        batt_resp = await self._client.read_holding_registers(
            address=_REG_BATTERY_POWER, count=2, slave=_SYSTEM_UNIT_ID
        )
        if batt_resp.isError():
            raise ModbusException(f"Battery read error: {batt_resp}")

        # Parse system registers
        grid_l1 = _uint16_to_int16(grid_resp.registers[0])
        grid_l2 = _uint16_to_int16(grid_resp.registers[1])
        grid_l3 = _uint16_to_int16(grid_resp.registers[2])

        battery_power = _uint16_to_int16(batt_resp.registers[0])  # int16, W
        battery_soc = batt_resp.registers[1]  # uint16, %

        self._state.grid_power_w = float(grid_l1 + grid_l2 + grid_l3)
        self._state.solar_battery_power_w = float(battery_power)
        self._state.solar_battery_soc_pct = float(battery_soc)

        # --- Grid meter voltage registers (configurable unit ID) ---
        grid_meter_unit = self._state.victron_grid_meter_unit_id

        # Registers 2616, 2618, 2620 are not contiguous (gap at 2617, 2619)
        # Read them individually
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

        # uint16, scale 10 → physical = raw / 10
        self._state.victron_l1_voltage_v = v1_resp.registers[0] / 10.0
        self._state.victron_l2_voltage_v = v2_resp.registers[0] / 10.0
        self._state.victron_l3_voltage_v = v3_resp.registers[0] / 10.0
