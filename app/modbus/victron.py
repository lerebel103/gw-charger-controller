"""Victron GX Modbus TCP client for reading grid, battery, and voltage data."""

import inspect
import logging
import struct

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from app.backoff import exponential_backoff
from app.log_throttle import LogThrottle
from app.state import AppState

logger = logging.getLogger(__name__)
_throttle = LogThrottle(logger, suppress_seconds=60.0)

# Victron system service unit ID (com.victronenergy.system)
_SYSTEM_UNIT_ID = 100

# System registers (unit ID 100)
_REG_GRID_L1_POWER = 820
_REG_GRID_L2_POWER = 821
_REG_GRID_L3_POWER = 822
_REG_BATTERY_POWER = 842
_REG_BATTERY_SOC = 843

# Grid meter voltage/current registers (unit ID from state.victron_grid_meter_unit_id)
# Block read 2616-2621: L1V, L1I, L2V, L2I, L3V, L3I
_REG_GRID_L1_VOLTAGE = 2616


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
                _throttle.clear("victron_connect_fail")
                _throttle.info("victron_connected", "Connected to Victron GX at %s:%d", ip, port)
            else:
                _throttle.warning("victron_connect_fail", "Victron GX connection failed (no connect)")
                self._schedule_retry()
        except (OSError, ModbusException) as exc:
            _throttle.warning("victron_connect_fail", "Victron GX connection failed: %s", exc)
            self._schedule_retry()

    async def read(self) -> None:
        """Read all registers and update AppState. Closes connection on error."""
        if not self.connected:
            # FR-14: Prevent stale current data from being re-sampled while disconnected
            self._state.victron_l1_current_a = None
            self._state.victron_l2_current_a = None
            self._state.victron_l3_current_a = None
            return
        try:
            await self._read_registers()
            _throttle.clear("victron_read_fail")
            _throttle.reset("victron_connected")
        except (ModbusException, OSError) as exc:
            # FR-14: Clear per-phase current on read failure to prevent stale safety data
            self._state.victron_l1_current_a = None
            self._state.victron_l2_current_a = None
            self._state.victron_l3_current_a = None
            _throttle.warning("victron_read_fail", "Victron GX read failed: %s", exc)
            await self._close()

    async def reconnect(self) -> None:
        """Force a reconnect (e.g. after IP/port change via MQTT)."""
        await self._close()
        self._reconnect_attempt = 0
        self._reconnect_after = 0.0

    def _config_changed(self) -> bool:
        return self._state.victron_ip != self._connected_ip or self._state.victron_port != self._connected_port

    def _schedule_retry(self) -> None:
        import time as _t

        delay = exponential_backoff(self._reconnect_attempt)
        self._reconnect_after = _t.monotonic() + delay
        self._reconnect_attempt += 1

    async def _close(self) -> None:
        if self._client is not None:
            maybe_awaitable = self._client.close()
            if inspect.isawaitable(maybe_awaitable):
                await maybe_awaitable
            self._client = None

    async def _read_registers(self) -> None:
        """Read system and grid meter registers and update AppState."""
        assert self._client is not None  # noqa: S101

        grid_resp = await self._client.read_holding_registers(
            address=_REG_GRID_L1_POWER, count=3, device_id=_SYSTEM_UNIT_ID
        )
        if grid_resp.isError():
            raise ModbusException(f"Grid power read error: {grid_resp}")

        batt_resp = await self._client.read_holding_registers(
            address=_REG_BATTERY_POWER, count=2, device_id=_SYSTEM_UNIT_ID
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

        # Block read: 2616-2621 (3 voltages + 3 currents interleaved)
        vc_resp = await self._client.read_holding_registers(
            address=_REG_GRID_L1_VOLTAGE, count=6, device_id=grid_meter_unit
        )
        if vc_resp.isError():
            self._state.victron_l1_current_a = None
            self._state.victron_l2_current_a = None
            self._state.victron_l3_current_a = None
            raise ModbusException(f"Grid voltage/current block read error: {vc_resp}")

        if len(vc_resp.registers) < 6:
            self._state.victron_l1_current_a = None
            self._state.victron_l2_current_a = None
            self._state.victron_l3_current_a = None
            raise ModbusException(
                f"Grid voltage/current block read incomplete: expected 6 registers, got {len(vc_resp.registers)}"
            )

        # Even offsets = voltages (uint16, ÷10), odd offsets = currents (int16, ÷10)
        self._state.victron_l1_voltage_v = vc_resp.registers[0] / 10.0
        self._state.victron_l2_voltage_v = vc_resp.registers[2] / 10.0
        self._state.victron_l3_voltage_v = vc_resp.registers[4] / 10.0

        # Decode signed currents with plausibility check (FR-15)
        plausibility_limit = 2.0 * self._state.grid_breaker_limit_a
        raw_currents = (
            _uint16_to_int16(vc_resp.registers[1]) / 10.0,
            _uint16_to_int16(vc_resp.registers[3]) / 10.0,
            _uint16_to_int16(vc_resp.registers[5]) / 10.0,
        )

        self._state.victron_l1_current_a = raw_currents[0] if abs(raw_currents[0]) <= plausibility_limit else None
        self._state.victron_l2_current_a = raw_currents[1] if abs(raw_currents[1]) <= plausibility_limit else None
        self._state.victron_l3_current_a = raw_currents[2] if abs(raw_currents[2]) <= plausibility_limit else None

        if logger.isEnabledFor(logging.DEBUG):
            l1 = f"{self._state.victron_l1_current_a:.1f}" if self._state.victron_l1_current_a is not None else "N/A"
            l2 = f"{self._state.victron_l2_current_a:.1f}" if self._state.victron_l2_current_a is not None else "N/A"
            l3 = f"{self._state.victron_l3_current_a:.1f}" if self._state.victron_l3_current_a is not None else "N/A"
            logger.debug("Grid phase current: L1=%s A, L2=%s A, L3=%s A", l1, l2, l3)
