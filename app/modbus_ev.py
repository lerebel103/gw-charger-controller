"""EV charger (GW22K-HCA-20) Modbus TCP client."""

from __future__ import annotations

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
_REG_PLUG_AND_CHARGE = 10019
_REG_MAX_CHARGING_POWER = 10029
_REG_CHARGER_ENABLE = 10060
_RAW_SETPOINT_MIN = 44  # minimum raw value (= 4.4 kW); 0 is also valid (pause)


class EVChargerModbusClient:
    """Modbus TCP client for the GW22K-HCA-20 EV charger.

    Provides ``ensure_connected()``, ``read()``, ``write_setpoint()``, and
    ``ensure_enabled()`` methods called by the control loop each iteration.
    Does not run its own async task.
    """

    def __init__(self, state: AppState) -> None:
        self._state = state
        self._client: AsyncModbusTcpClient | None = None
        self._connected_ip: str = ""
        self._connected_port: int = 0
        self._reconnect_attempt: int = 0
        self._reconnect_after: float = 0.0

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.connected

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    async def ensure_connected(self) -> None:
        """Check connection and reconnect if needed. Non-blocking single attempt."""
        if self.connected and not self._config_changed():
            return

        if self._config_changed():
            await self._close()

        ip = self._state.ev_charger_ip
        port = self._state.ev_charger_port
        if not ip:
            return

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
                logger.info("Connected to EV charger at %s:%d", ip, port)
            else:
                self._schedule_retry()
        except (OSError, ModbusException) as exc:
            logger.warning("EV charger connection failed: %s", exc)
            self._schedule_retry()

    async def reconnect(self) -> None:
        """Force a reconnect (e.g. after IP/port change via MQTT)."""
        await self._close()
        self._reconnect_attempt = 0
        self._reconnect_after = 0.0

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def read(self) -> None:
        """Read all registers and update AppState. Closes connection on error."""
        if not self.connected:
            return
        try:
            await self._read_registers()
        except (ModbusException, OSError) as exc:
            logger.warning("EV charger read failed: %s", exc)
            await self._close()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def write_setpoint(self, power_w: float) -> None:
        """Write the maximum charging power register.

        Compares the desired raw value against the charger's actual register
        value (read each poll) and only writes if they differ.

        Args:
            power_w: Desired charge power in watts (0 = pause, >= 4200 = charge).
        """
        if not self._state.ev_connected and power_w > 0:
            return

        if not self.connected:
            logger.warning("write_setpoint skipped: Modbus not connected")
            return

        raw = round(power_w / 100) if power_w > 0 else 0
        if raw > 0 and raw < _RAW_SETPOINT_MIN:
            raw = _RAW_SETPOINT_MIN

        if self._state.ev_charger_setpoint_raw is not None and raw == self._state.ev_charger_setpoint_raw:
            return

        try:
            resp = await self._client.write_register(address=_REG_MAX_CHARGING_POWER, value=raw, slave=_SLAVE_ID)
            if resp.isError():
                raise ModbusException(f"Setpoint write error: {resp}")
            self._state.ev_charger_setpoint_raw = raw
            logger.debug("Wrote charging setpoint raw=%d (%.0f W)", raw, power_w)
        except (ModbusException, OSError) as exc:
            logger.warning("EV charger setpoint write failed: %s", exc)

    async def ensure_plug_and_charge(self) -> None:
        """Ensure plug-and-charge is enabled. Writes register 10019=1 if not already set."""
        if self._state.ev_plug_and_charge:
            return
        if not self.connected:
            return
        try:
            resp = await self._client.write_register(address=_REG_PLUG_AND_CHARGE, value=1, slave=_SLAVE_ID)
            if resp.isError():
                raise ModbusException(f"Plug and charge write error: {resp}")
            self._state.ev_plug_and_charge = True
            logger.info("Plug and charge was disabled — enabled (register 10019=1)")
        except (ModbusException, OSError) as exc:
            logger.warning("Failed to enable plug and charge: %s", exc)

    async def ensure_enabled(self) -> None:
        """Write register 10060=2 every cycle to keep the charger enabled.

        This register is a command register that does not hold state,
        so we write unconditionally each iteration.
        """
        if not self.connected:
            return
        try:
            await self._client.write_register(address=_REG_CHARGER_ENABLE, value=2, slave=_SLAVE_ID)
        except (ModbusException, OSError) as exc:
            logger.warning("Failed to write charger enable: %s", exc)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _config_changed(self) -> bool:
        return self._state.ev_charger_ip != self._connected_ip or self._state.ev_charger_port != self._connected_port

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
        """Read EV charger registers and update AppState."""
        assert self._client is not None  # noqa: S101

        # Contiguous block: registers 10009–10017 (9 registers)
        main_resp = await self._client.read_holding_registers(
            address=_REG_PHASE_A_VOLTAGE, count=_CONTIGUOUS_COUNT, slave=_SLAVE_ID
        )
        if main_resp.isError():
            raise ModbusException(f"EV charger main register read error: {main_resp}")

        regs = main_resp.registers
        self._state.ev_voltage_l1_v = regs[0] / 10.0
        self._state.ev_voltage_l2_v = regs[1] / 10.0
        self._state.ev_voltage_l3_v = regs[2] / 10.0
        self._state.ev_current_a = regs[3] / 10.0
        self._state.ev_current_b = regs[4] / 10.0
        self._state.ev_current_c = regs[5] / 10.0
        self._state.ev_active_power_w = regs[6] / 10.0 * 1000.0
        self._state.ev_session_energy_wh = regs[7] / 10.0 * 1000.0
        self._state.ev_charger_status = regs[8]

        # Completion time (register 10031)
        ct_resp = await self._client.read_holding_registers(address=_REG_COMPLETION_TIME, count=1, slave=_SLAVE_ID)
        if ct_resp.isError():
            raise ModbusException(f"EV charger completion time read error: {ct_resp}")
        self._state.ev_completion_time_h = ct_resp.registers[0]

        # Total accumulated energy (registers 10065-10066, U32)
        te_resp = await self._client.read_holding_registers(address=_REG_TOTAL_ENERGY, count=2, slave=_SLAVE_ID)
        if te_resp.isError():
            raise ModbusException(f"EV charger total energy read error: {te_resp}")
        raw_hi = te_resp.registers[0]
        raw_lo = te_resp.registers[1]
        raw_u32 = (raw_hi << 16) | raw_lo
        self._state.ev_total_energy_wh = raw_u32 / 10.0 * 1000.0
        logger.debug(
            "Total energy regs: hi=%d lo=%d raw_u32=%d wh=%.0f",
            raw_hi,
            raw_lo,
            raw_u32,
            self._state.ev_total_energy_wh,
        )

        # Car connection status (register 10075)
        cc_resp = await self._client.read_holding_registers(address=_REG_CAR_CONNECTION, count=1, slave=_SLAVE_ID)
        if cc_resp.isError():
            raise ModbusException(f"EV charger car connection read error: {cc_resp}")
        self._state.ev_connected = cc_resp.registers[0] != 0

        # Plug and charge state (register 10019)
        pnc_resp = await self._client.read_holding_registers(address=_REG_PLUG_AND_CHARGE, count=1, slave=_SLAVE_ID)
        if pnc_resp.isError():
            raise ModbusException(f"EV charger plug and charge read error: {pnc_resp}")
        self._state.ev_plug_and_charge = pnc_resp.registers[0] == 1

        # Current setpoint (register 10029)
        sp_resp = await self._client.read_holding_registers(address=_REG_MAX_CHARGING_POWER, count=1, slave=_SLAVE_ID)
        if sp_resp.isError():
            raise ModbusException(f"EV charger setpoint read error: {sp_resp}")
        self._state.ev_charger_setpoint_raw = sp_resp.registers[0]

        # Compute voltage drop percentages
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
                setattr(self._state, drop_attr, 100.0 * (victron_v - ev_v) / victron_v)
            else:
                setattr(self._state, drop_attr, None)
