"""EV charger (GW22K-HCA-20) Modbus TCP client."""

from __future__ import annotations

import asyncio
import logging

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from app.backoff import exponential_backoff
from app.state import AppState

logger = logging.getLogger(__name__)

# Default Modbus slave ID for the EV charger
_SLAVE_ID = 247

# --- Read registers ---
# Contiguous block: 10009..10017 (9 registers)
_REG_PHASE_A_VOLTAGE = 10009
_CONTIGUOUS_COUNT = 9  # 10009–10017

# Separate reads
_REG_COMPLETION_TIME = 10031
_REG_TOTAL_ENERGY = 10065
_REG_CAR_CONNECTION = 10075

# --- Write registers ---
_REG_PLUG_AND_CHARGE = 10019  # 1 = Plug and Charge mode
_REG_MAX_CHARGING_POWER = 10029
_REG_CHARGER_ENABLE = 10060
_RAW_SETPOINT_MIN = 14  # minimum raw value (= 1.4 kW)


class EVChargerModbusClient:
    """Async Modbus TCP client for the GW22K-HCA-20 EV charger.

    Polls charger registers at a configurable interval and provides
    ``write_setpoint`` for the control loop.  Reconnects with exponential
    backoff on failure and when the target IP/port changes.
    """

    def __init__(self, state: AppState, poll_interval_s: float = 5.0) -> None:
        self._state = state
        self._poll_interval_s = poll_interval_s
        self._client: AsyncModbusTcpClient | None = None
        self._connected_ip: str = ""
        self._connected_port: int = 0
        self._prev_ev_connected: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def poll_loop(self) -> None:
        """Run the polling loop as an asyncio task."""
        while True:
            if self._needs_reconnect():
                await self.reconnect()

            if self._client is not None and self._client.connected:
                try:
                    await self._read_registers()
                except (ModbusException, OSError) as exc:
                    logger.warning("EV charger read failed: %s", exc)
                    await self._close()
                    await self.reconnect()

            await asyncio.sleep(self._poll_interval_s)

    async def write_setpoint(self, power_w: float) -> None:
        """Write the maximum charging power register.

        Args:
            power_w: Desired charge power in watts.
                     Converted to raw = round(power_w / 100), clamped to min 14.
        """
        if not self._state.ev_connected:
            logger.warning("write_setpoint skipped: EV not connected")
            return

        if self._client is None or not self._client.connected:
            logger.warning("write_setpoint skipped: Modbus not connected")
            return

        raw = max(_RAW_SETPOINT_MIN, round(power_w / 100))
        try:
            resp = await self._client.write_register(address=_REG_MAX_CHARGING_POWER, value=raw, slave=_SLAVE_ID)
            if resp.isError():
                raise ModbusException(f"Setpoint write error: {resp}")
            logger.info("Wrote charging setpoint raw=%d (%.0f W)", raw, power_w)
        except (ModbusException, OSError) as exc:
            logger.warning("EV charger setpoint write failed: %s", exc)

    async def write_enable(self, enable: bool) -> None:
        """Enable or disable the charger via register 10060.

        Args:
            enable: True to enable (write 1), False to disable (write 0).
        """
        if self._client is None or not self._client.connected:
            logger.warning("write_enable skipped: Modbus not connected")
            return

        if not self._state.ev_connected:
            logger.warning("write_enable skipped: EV not connected")
            return

        value = 1 if enable else 0
        try:
            resp = await self._client.write_register(address=_REG_CHARGER_ENABLE, value=value, slave=_SLAVE_ID)
            if resp.isError():
                raise ModbusException(f"Charger enable write error: {resp}")
            logger.info("Wrote charger enable=%d", value)
        except (ModbusException, OSError) as exc:
            logger.warning("EV charger enable write failed: %s", exc)

    async def reconnect(self) -> None:
        """Close existing connection and reconnect with exponential backoff."""
        await self._close()

        ip = self._state.ev_charger_ip
        port = self._state.ev_charger_port

        if not ip:
            logger.warning("EV charger IP not configured, skipping connection")
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
                    logger.info("Connected to EV charger at %s:%d", ip, port)
                    return
                else:
                    logger.warning("EV charger connection to %s:%d returned False", ip, port)
            except (OSError, ModbusException) as exc:
                logger.warning(
                    "EV charger connection attempt %d to %s:%d failed: %s",
                    attempt,
                    ip,
                    port,
                    exc,
                )

            delay = exponential_backoff(attempt)
            logger.info("Retrying EV charger connection in %.1f s", delay)
            await asyncio.sleep(delay)
            attempt += 1

            # If IP/port changed while waiting, restart with new target
            if self._state.ev_charger_ip != ip or self._state.ev_charger_port != port:
                ip = self._state.ev_charger_ip
                port = self._state.ev_charger_port
                attempt = 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _needs_reconnect(self) -> bool:
        """Check whether a reconnect is needed."""
        if self._client is None or not self._client.connected:
            return True
        return self._state.ev_charger_ip != self._connected_ip or self._state.ev_charger_port != self._connected_port

    async def _close(self) -> None:
        """Close the current Modbus connection if open."""
        if self._client is not None:
            self._client.close()
            self._client = None

    async def _read_registers(self) -> None:
        """Read EV charger registers and update AppState."""
        assert self._client is not None  # noqa: S101

        # --- Contiguous block: registers 10009–10017 (9 registers) ---
        main_resp = await self._client.read_holding_registers(
            address=_REG_PHASE_A_VOLTAGE, count=_CONTIGUOUS_COUNT, slave=_SLAVE_ID
        )
        if main_resp.isError():
            raise ModbusException(f"EV charger main register read error: {main_resp}")

        regs = main_resp.registers  # indices 0..8 → registers 10009..10017

        self._state.ev_voltage_l1_v = regs[0] / 10.0  # 10009
        self._state.ev_voltage_l2_v = regs[1] / 10.0  # 10010
        self._state.ev_voltage_l3_v = regs[2] / 10.0  # 10011
        self._state.ev_current_a = regs[3] / 10.0  # 10012
        self._state.ev_current_b = regs[4] / 10.0  # 10013
        self._state.ev_current_c = regs[5] / 10.0  # 10014
        # 10015: power in kW*10 → physical kW = raw/10, watts = kW*1000
        self._state.ev_active_power_w = regs[6] / 10.0 * 1000.0
        # 10016: energy in kWh*10 → physical kWh = raw/10, Wh = kWh*1000
        self._state.ev_session_energy_wh = regs[7] / 10.0 * 1000.0
        self._state.ev_charger_status = regs[8]  # 10017

        # --- Completion time (register 10031) ---
        ct_resp = await self._client.read_holding_registers(address=_REG_COMPLETION_TIME, count=1, slave=_SLAVE_ID)
        if ct_resp.isError():
            raise ModbusException(f"EV charger completion time read error: {ct_resp}")
        self._state.ev_completion_time_h = ct_resp.registers[0]

        # --- Total accumulated energy (registers 10065-10066, U32, SF=10, kWh) ---
        te_resp = await self._client.read_holding_registers(address=_REG_TOTAL_ENERGY, count=2, slave=_SLAVE_ID)
        if te_resp.isError():
            raise ModbusException(f"EV charger total energy read error: {te_resp}")
        raw_u32 = (te_resp.registers[0] << 16) | te_resp.registers[1]
        self._state.ev_total_energy_wh = raw_u32 / 10.0 * 1000.0

        # --- Car connection status (register 10075) ---
        cc_resp = await self._client.read_holding_registers(address=_REG_CAR_CONNECTION, count=1, slave=_SLAVE_ID)
        if cc_resp.isError():
            raise ModbusException(f"EV charger car connection read error: {cc_resp}")
        self._state.ev_connected = cc_resp.registers[0] != 0

        # On rising edge (EV just connected), set Plug and Charge mode
        if self._state.ev_connected and not self._prev_ev_connected:
            try:
                resp = await self._client.write_register(address=_REG_PLUG_AND_CHARGE, value=1, slave=_SLAVE_ID)
                if resp.isError():
                    raise ModbusException(f"Plug and Charge write error: {resp}")
                logger.info("EV connected — set Plug and Charge mode (register 10019=1)")
            except (ModbusException, OSError) as exc:
                logger.warning("Failed to set Plug and Charge mode: %s", exc)
        self._prev_ev_connected = self._state.ev_connected

        # --- Compute voltage drop percentages ---
        self._compute_voltage_drops()

    def _compute_voltage_drops(self) -> None:
        """Compute per-phase voltage drop between Victron GX and EV charger."""
        pairs = [
            ("victron_l1_voltage_v", "ev_voltage_l1_v", "l1_voltage_drop_pct"),
            ("victron_l2_voltage_v", "ev_voltage_l2_v", "l2_voltage_drop_pct"),
            ("victron_l3_voltage_v", "ev_voltage_l3_v", "l3_voltage_drop_pct"),
        ]
        for victron_attr, ev_attr, drop_attr in pairs:
            victron_v = getattr(self._state, victron_attr)
            ev_v = getattr(self._state, ev_attr)
            if victron_v is not None and ev_v is not None and victron_v > 0:
                setattr(
                    self._state,
                    drop_attr,
                    100.0 * (victron_v - ev_v) / victron_v,
                )
            else:
                setattr(self._state, drop_attr, None)
