"""EV charger (GW22K-HCA-20) Modbus TCP client."""

import inspect
import logging
import time as _t

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from app.backoff import exponential_backoff
from app.log_throttle import LogThrottle
from app.state import AdvancedChargingMode, AppState, ChargerStatus, PlugAndChargeAutoStart, SinglePhaseSwitching

logger = logging.getLogger(__name__)
_throttle = LogThrottle(logger, suppress_seconds=60.0)

# Default Modbus slave ID for the EV charger
_SLAVE_ID = 247

# --- Read registers ---
# Contiguous block: 10009..10018 (10 registers)
_REG_PHASE_A_VOLTAGE = 10009
_CONTIGUOUS_COUNT = 10  # 10009–10018

# Separate reads
_REG_COMPLETION_TIME = 10031
_REG_ADVANCED_CHARGING_MODE = 10032
_REG_SERIAL_NUMBER = 10040
_REG_TOTAL_ENERGY = 10065
_REG_CAR_CONNECTION = 10075

# --- Write registers ---
_REG_PLUG_AND_CHARGE = 10019
_REG_SINGLE_PHASE_SWITCHING = 10023
_REG_MAX_CHARGING_POWER = 10029
_REG_MAX_GRID_POWER_DRAW = 10039
_REG_CHARGER_ENABLE = 10060
_RAW_SETPOINT_MIN = 44  # practical minimum to reliably start charging (= 4.4 kW)
_RAW_RUNTIME_SETPOINT_MIN = 42  # documented physical minimum (= 4.2 kW)
_RAW_SETPOINT_MAX = 220  # 22.0 kW
_RECONNECT_DELAY_S = 1.0
_RECONNECT_DELAY_MAX_S = 60.0
_CONNECT_TIMEOUT_S = 3.0
_READ_RETRIES = 3
_MAX_CONSECUTIVE_READ_FAILURES = 3  # force reconnect after this many consecutive read errors


class EVChargerModbusClient:
    """Modbus TCP client for the GW22K-HCA-20 EV charger.

    Provides ``ensure_connected()``, ``read()``, ``write_setpoint()``,
    ``start_charging()``, and ``stop_charging()`` methods called by the
    control loop each iteration.
    Does not run its own async task.
    """

    def __init__(self, state: AppState) -> None:
        self._state = state
        self._client: AsyncModbusTcpClient | None = None
        self._client_ip: str = ""
        self._client_port: int = 0
        self._reconnect_attempt: int = 0
        self._reconnect_after: float = 0.0
        self._serial_read_attempted: bool = False
        self._consecutive_read_failures: int = 0

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.connected

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    async def ensure_connected(self) -> None:
        """Check connection and reconnect if needed. Non-blocking single attempt."""
        if self._config_changed():
            await self._close()

        if self.connected:
            return

        # While disconnected (or reconnecting), status must be treated as stale.
        self._state.ev_comm_healthy = False

        ip = self._state.ev_charger_ip
        port = self._state.ev_charger_port
        if not ip:
            return

        if self._client is None:
            self._client_ip = ip
            self._client_port = port
            self._client = AsyncModbusTcpClient(
                ip,
                port=port,
                reconnect_delay=_RECONNECT_DELAY_S,
                reconnect_delay_max=_RECONNECT_DELAY_MAX_S,
                timeout=_CONNECT_TIMEOUT_S,
                retries=_READ_RETRIES,
            )

        now = _t.monotonic()
        if now < self._reconnect_after:
            return

        try:
            connected = await self._client.connect()
            if connected:
                self._connected_ip = ip
                self._connected_port = port
                self._reconnect_attempt = 0
                self._serial_read_attempted = False
                _throttle.clear("ev_connect_fail")
                _throttle.reset("ev_write_setpoint_disconnected")
                _throttle.info("ev_connected", "Connected to EV charger at %s:%d", ip, port)
                await self._read_serial_number_once()
            else:
                _throttle.warning("ev_connect_fail", "EV charger connection failed (no connect)")
                self._schedule_retry()
        except (OSError, ModbusException) as exc:
            _throttle.warning("ev_connect_fail", "EV charger connection failed: %s", exc)
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
        """Read all registers and update AppState."""
        if not self.connected:
            self._state.ev_comm_healthy = False
            return
        try:
            await self._read_registers()
            self._consecutive_read_failures = 0
            self._state.ev_comm_healthy = True
            self._state.ev_last_read_ok_at = _t.monotonic()
            _throttle.clear("ev_read_fail")
            _throttle.reset("ev_connected")
        except (ModbusException, OSError) as exc:
            self._consecutive_read_failures += 1
            self._state.ev_comm_healthy = False
            self._state.ev_last_read_error_at = _t.monotonic()
            _throttle.warning("ev_read_fail", "EV charger read failed: %s", exc)
            if self._consecutive_read_failures >= _MAX_CONSECUTIVE_READ_FAILURES:
                await self._close()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def write_setpoint(self, power_w: float) -> None:
        """Write the maximum charging power register.

        Compares the desired raw value against the charger's actual register
        value (read each poll) and only writes if they differ.

        Args:
            power_w: Desired charge power in watts.

                Operational notes:
                - 4.4 kW is the practical minimum that reliably starts charging.
                - 4.2 kW may be used by the control loop immediately before an
                  explicit stop command to keep register values in-range while
                  intentionally stopping charging.
        """
        if not self._state.ev_connected and power_w > 0:
            return

        if not self.connected:
            _throttle.warning("ev_write_setpoint_disconnected", "write_setpoint skipped: Modbus not connected")
            return

        raw = round(power_w / 100) if power_w > 0 else 0
        if raw > 0 and raw < _RAW_SETPOINT_MIN:
            raw = _RAW_SETPOINT_MIN

        if self._state.ev_charger_setpoint_raw is not None and raw == self._state.ev_charger_setpoint_raw:
            return

        try:
            resp = await self._client.write_register(address=_REG_MAX_CHARGING_POWER, value=raw, device_id=_SLAVE_ID)
            if resp.isError():
                raise ModbusException(f"Setpoint write error: {resp}")
            self._state.ev_charger_setpoint_raw = raw
            _throttle.clear("ev_setpoint_write_fail")
            logger.debug("Wrote charging setpoint raw=%d (%.0f W)", raw, power_w)
        except (ModbusException, OSError) as exc:
            _throttle.warning("ev_setpoint_write_fail", "EV charger setpoint write failed: %s", exc)

    async def ensure_plug_and_charge(self) -> None:
        """Ensure plug-and-charge is enabled. Writes register 10019=1 if not already set."""
        if self._state.ev_plug_and_charge:
            return
        if not self.connected:
            return
        try:
            resp = await self._client.write_register(address=_REG_PLUG_AND_CHARGE, value=1, device_id=_SLAVE_ID)
            if resp.isError():
                raise ModbusException(f"Plug and charge write error: {resp}")
            self._state.ev_plug_and_charge = True
            self._state.ev_plug_and_charge_auto_start = 1
            self._state.ev_plug_and_charge_auto_start_enum = PlugAndChargeAutoStart.ON
            logger.info("Plug and charge was disabled — enabled (register 10019=1)")
        except (ModbusException, OSError) as exc:
            logger.warning("Failed to enable plug and charge: %s", exc)

    async def write_advanced_charging_mode(self, mode: AdvancedChargingMode) -> bool:
        """Write register 10032 (advanced charging mode)."""
        if not self.connected:
            return False
        try:
            resp = await self._client.write_register(
                address=_REG_ADVANCED_CHARGING_MODE,
                value=int(mode.value),
                device_id=_SLAVE_ID,
            )
            if resp.isError():
                raise ModbusException(f"Advanced charging mode write error: {resp}")
            self._state.ev_advanced_charging_mode = int(mode.value)
            self._state.ev_advanced_charging_mode_enum = mode
            logger.info(
                "Wrote advanced charging mode: register %d <- raw=%d (%s)",
                _REG_ADVANCED_CHARGING_MODE,
                int(mode.value),
                mode.display_name,
            )
            return True
        except (ModbusException, OSError) as exc:
            logger.warning("Failed to write advanced charging mode: %s", exc)
            return False

    async def read_advanced_charging_mode(self) -> AdvancedChargingMode | None:
        """Read register 10032 (advanced charging mode)."""
        if not self.connected:
            return None
        try:
            resp = await self._client.read_holding_registers(
                address=_REG_ADVANCED_CHARGING_MODE,
                count=1,
                device_id=_SLAVE_ID,
            )
            if resp.isError():
                raise ModbusException(f"Advanced charging mode read error: {resp}")
            raw = resp.registers[0]
            mode = AdvancedChargingMode.from_register(raw)
            self._state.ev_advanced_charging_mode = raw
            self._state.ev_advanced_charging_mode_enum = mode
            return mode
        except (ModbusException, OSError) as exc:
            logger.warning("Failed to read advanced charging mode: %s", exc)
            return None

    async def write_plug_and_charge_auto_start(self, mode: PlugAndChargeAutoStart) -> bool:
        """Write register 10019 (plug-and-charge auto start)."""
        if not self.connected:
            return False
        try:
            resp = await self._client.write_register(
                address=_REG_PLUG_AND_CHARGE,
                value=int(mode.value),
                device_id=_SLAVE_ID,
            )
            if resp.isError():
                raise ModbusException(f"Plug and charge write error: {resp}")
            self._state.ev_plug_and_charge_auto_start = int(mode.value)
            self._state.ev_plug_and_charge_auto_start_enum = mode
            self._state.ev_plug_and_charge = mode == PlugAndChargeAutoStart.ON
            logger.info(
                "Wrote plug-and-charge auto start: register %d <- raw=%d (%s)",
                _REG_PLUG_AND_CHARGE,
                int(mode.value),
                mode.display_name,
            )
            return True
        except (ModbusException, OSError) as exc:
            logger.warning("Failed to write plug-and-charge auto start: %s", exc)
            return False

    async def write_single_phase_switching(self, mode: SinglePhaseSwitching) -> bool:
        """Write register 10023 (single phase switching)."""
        if not self.connected:
            return False
        try:
            resp = await self._client.write_register(
                address=_REG_SINGLE_PHASE_SWITCHING,
                value=int(mode.value),
                device_id=_SLAVE_ID,
            )
            if resp.isError():
                raise ModbusException(f"Single phase switching write error: {resp}")
            self._state.ev_single_phase_switching = int(mode.value)
            self._state.ev_single_phase_switching_enum = mode
            logger.info(
                "Wrote single phase switching: register %d <- raw=%d (%s)",
                _REG_SINGLE_PHASE_SWITCHING,
                int(mode.value),
                mode.display_name,
            )
            return True
        except (ModbusException, OSError) as exc:
            logger.warning("Failed to write single phase switching: %s", exc)
            return False

    async def write_max_grid_power_draw(self, power_w: float) -> bool:
        """Write register 10039 (max grid drawing power) using physical range 4200-22000 W."""
        if not self.connected:
            return False

        raw = round(power_w / 100.0)
        raw = max(_RAW_RUNTIME_SETPOINT_MIN, min(_RAW_SETPOINT_MAX, raw))

        try:
            resp = await self._client.write_register(
                address=_REG_MAX_GRID_POWER_DRAW,
                value=raw,
                device_id=_SLAVE_ID,
            )
            if resp.isError():
                raise ModbusException(f"Max grid power draw write error: {resp}")
            self._state.ev_max_grid_power_draw_raw = raw
            logger.info(
                "Wrote max grid power draw: register %d <- raw=%d (%.0f W)",
                _REG_MAX_GRID_POWER_DRAW,
                raw,
                raw * 100.0,
            )
            return True
        except (ModbusException, OSError) as exc:
            logger.warning("Failed to write max grid power draw: %s", exc)
            return False

    async def read_plug_and_charge_auto_start(self) -> PlugAndChargeAutoStart | None:
        """Read register 10019 (plug-and-charge auto start)."""
        if not self.connected:
            return None
        try:
            resp = await self._client.read_holding_registers(address=_REG_PLUG_AND_CHARGE, count=1, device_id=_SLAVE_ID)
            if resp.isError():
                raise ModbusException(f"EV charger plug and charge read error: {resp}")
            raw = resp.registers[0]
            mode = PlugAndChargeAutoStart.from_register(raw)
            self._state.ev_plug_and_charge_auto_start = raw
            self._state.ev_plug_and_charge_auto_start_enum = mode
            self._state.ev_plug_and_charge = mode == PlugAndChargeAutoStart.ON
            return mode
        except (ModbusException, OSError) as exc:
            logger.warning("Failed to read plug-and-charge auto start: %s", exc)
            return None

    async def read_single_phase_switching(self) -> SinglePhaseSwitching | None:
        """Read register 10023 (single phase switching)."""
        if not self.connected:
            return None
        try:
            resp = await self._client.read_holding_registers(
                address=_REG_SINGLE_PHASE_SWITCHING,
                count=1,
                device_id=_SLAVE_ID,
            )
            if resp.isError():
                raise ModbusException(f"Single phase switching read error: {resp}")
            raw = resp.registers[0]
            mode = SinglePhaseSwitching.from_register(raw)
            self._state.ev_single_phase_switching = raw
            self._state.ev_single_phase_switching_enum = mode
            return mode
        except (ModbusException, OSError) as exc:
            logger.warning("Failed to read single phase switching: %s", exc)
            return None

    async def read_max_grid_power_draw(self) -> float | None:
        """Read register 10039 (max grid drawing power) and return watts."""
        if not self.connected:
            return None
        try:
            resp = await self._client.read_holding_registers(
                address=_REG_MAX_GRID_POWER_DRAW,
                count=1,
                device_id=_SLAVE_ID,
            )
            if resp.isError():
                raise ModbusException(f"Max grid power draw read error: {resp}")
            raw = resp.registers[0]
            self._state.ev_max_grid_power_draw_raw = raw
            return raw * 100.0
        except (ModbusException, OSError) as exc:
            logger.warning("Failed to read max grid power draw: %s", exc)
            return None

    async def start_charging(self) -> None:
        """Send a one-shot start command (register 10060=2).

        Register 10060 is a non-persistent command register. The charger clears
        it after processing, so this method should only be called when a start
        transition is required.

        Clears the cached setpoint so the next write_setpoint call will
        unconditionally re-send the value, ensuring the charger has the
        correct power limit before it begins drawing current.
        """
        if not self.connected:
            return
        self._state.ev_charger_setpoint_raw = None
        try:
            await self._client.write_register(address=_REG_CHARGER_ENABLE, value=2, device_id=_SLAVE_ID)
        except (ModbusException, OSError) as exc:
            logger.warning("Failed to send charger start command: %s", exc)

    async def stop_charging(self) -> None:
        """Send a one-shot stop command (register 10060=1).

        Register 10060 is a non-persistent command register. The charger clears
        it after processing, so this method should only be called when a stop
        transition is required.
        """
        if not self.connected:
            return
        try:
            await self._client.write_register(address=_REG_CHARGER_ENABLE, value=1, device_id=_SLAVE_ID)
        except (ModbusException, OSError) as exc:
            logger.warning("Failed to send charger stop command: %s", exc)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _config_changed(self) -> bool:
        if self._client is None:
            return False
        return self._state.ev_charger_ip != self._client_ip or self._state.ev_charger_port != self._client_port

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
        self._client_ip = ""
        self._client_port = 0
        self._consecutive_read_failures = 0
        self._state.ev_comm_healthy = False
        self._serial_read_attempted = False

    async def disconnect(self) -> None:
        """Close the current Modbus connection.

        Used by on-demand standby commands to avoid leaving background sessions open.
        """
        await self._close()

    async def _read_registers(self) -> None:
        """Read EV charger registers and update AppState."""
        assert self._client is not None  # noqa: S101

        correction_pct = min(10.0, max(0.0, float(self._state.correction_pct)))
        correction_factor = 1.0 + (correction_pct / 100.0)

        # Contiguous block: registers 10009–10018
        main_resp = await self._client.read_holding_registers(
            address=_REG_PHASE_A_VOLTAGE, count=_CONTIGUOUS_COUNT, device_id=_SLAVE_ID
        )
        if main_resp.isError():
            raise ModbusException(f"EV charger main register read error: {main_resp}")

        regs = main_resp.registers
        self._state.ev_voltage_l1_v = regs[0] / 10.0
        self._state.ev_voltage_l2_v = regs[1] / 10.0
        self._state.ev_voltage_l3_v = regs[2] / 10.0
        self._state.ev_current_a = (regs[3] / 10.0) * correction_factor
        self._state.ev_current_b = (regs[4] / 10.0) * correction_factor
        self._state.ev_current_c = (regs[5] / 10.0) * correction_factor
        self._state.ev_active_power_w = (regs[6] / 10.0 * 1000.0) * correction_factor
        self._state.ev_session_energy_wh = regs[7] / 10.0 * 1000.0
        self._state.ev_charger_status = regs[8]
        self._state.ev_charger_status_enum = ChargerStatus.from_register(regs[8])
        self._update_comm_connection_status(regs[9] if len(regs) > 9 else 0)

        # Completion time + advanced charging mode (registers 10031-10032)
        ct_resp = await self._client.read_holding_registers(address=_REG_COMPLETION_TIME, count=2, device_id=_SLAVE_ID)
        if ct_resp.isError():
            raise ModbusException(f"EV charger completion time read error: {ct_resp}")
        self._state.ev_completion_time_h = ct_resp.registers[0]
        mode_raw = ct_resp.registers[1] if len(ct_resp.registers) > 1 else None
        self._state.ev_advanced_charging_mode = mode_raw
        self._state.ev_advanced_charging_mode_enum = AdvancedChargingMode.from_register(mode_raw)

        # Total accumulated energy (registers 10065-10066, U32)
        te_resp = await self._client.read_holding_registers(address=_REG_TOTAL_ENERGY, count=2, device_id=_SLAVE_ID)
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
        cc_resp = await self._client.read_holding_registers(address=_REG_CAR_CONNECTION, count=1, device_id=_SLAVE_ID)
        if cc_resp.isError():
            raise ModbusException(f"EV charger car connection read error: {cc_resp}")
        self._state.ev_connected = cc_resp.registers[0] != 0

        # Plug and charge state (register 10019)
        pnc_resp = await self._client.read_holding_registers(address=_REG_PLUG_AND_CHARGE, count=1, device_id=_SLAVE_ID)
        if pnc_resp.isError():
            raise ModbusException(f"EV charger plug and charge read error: {pnc_resp}")
        pnc_raw = pnc_resp.registers[0]
        pnc_mode = PlugAndChargeAutoStart.from_register(pnc_raw)
        self._state.ev_plug_and_charge = pnc_mode == PlugAndChargeAutoStart.ON
        self._state.ev_plug_and_charge_auto_start = pnc_raw
        self._state.ev_plug_and_charge_auto_start_enum = pnc_mode

        # Single phase switching state (register 10023)
        sps_resp = await self._client.read_holding_registers(
            address=_REG_SINGLE_PHASE_SWITCHING,
            count=1,
            device_id=_SLAVE_ID,
        )
        if sps_resp.isError():
            raise ModbusException(f"EV charger single phase switching read error: {sps_resp}")
        sps_raw = sps_resp.registers[0]
        self._state.ev_single_phase_switching = sps_raw
        self._state.ev_single_phase_switching_enum = SinglePhaseSwitching.from_register(sps_raw)

        # Current setpoint (register 10029)
        sp_resp = await self._client.read_holding_registers(
            address=_REG_MAX_CHARGING_POWER, count=1, device_id=_SLAVE_ID
        )
        if sp_resp.isError():
            raise ModbusException(f"EV charger setpoint read error: {sp_resp}")
        self._state.ev_charger_setpoint_raw = sp_resp.registers[0]

        # Max grid drawing power (register 10039) is best-effort because some
        # charger firmware variants may not expose this register consistently.
        try:
            mgp_resp = await self._client.read_holding_registers(
                address=_REG_MAX_GRID_POWER_DRAW,
                count=1,
                device_id=_SLAVE_ID,
            )
            if mgp_resp.isError():
                _throttle.warning("ev_max_grid_power_read", "EV charger max grid power draw read error: %s", mgp_resp)
                self._state.ev_max_grid_power_draw_raw = None
            else:
                _throttle.clear("ev_max_grid_power_read")
                self._state.ev_max_grid_power_draw_raw = mgp_resp.registers[0]
        except (ModbusException, OSError) as exc:
            _throttle.warning(
                "ev_max_grid_power_read",
                "Failed to read max grid power draw (register %d): %s",
                _REG_MAX_GRID_POWER_DRAW,
                exc,
            )
            self._state.ev_max_grid_power_draw_raw = None

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

    async def _read_serial_number_once(self) -> None:
        """Read serial number once on startup/connection and cache in AppState."""
        if self._serial_read_attempted:
            return
        self._serial_read_attempted = True
        if not self.connected:
            return
        try:
            resp = await self._client.read_holding_registers(address=_REG_SERIAL_NUMBER, count=8, device_id=_SLAVE_ID)
            if resp.isError():
                raise ModbusException(f"EV charger serial number read error: {resp}")

            raw_bytes = bytearray()
            for reg in resp.registers:
                raw_bytes.append((reg >> 8) & 0xFF)
                raw_bytes.append(reg & 0xFF)

            decoded = raw_bytes.decode("ascii", errors="ignore").replace("\x00", "").strip()
            self._state.ev_serial_number = decoded[:8] if decoded else None
        except (ModbusException, OSError) as exc:
            logger.warning("Failed to read serial number: %s", exc)

    def _update_comm_connection_status(self, raw: int) -> None:
        """Decode register 10018 bitfield into AppState flags."""
        self._state.ev_comm_connection_status_raw = raw
        self._state.ev_comm_wifi_router_connected = bool(raw & (1 << 0))
        self._state.ev_comm_iot_cloud_connected = bool(raw & (1 << 1))
        self._state.ev_comm_inverter_online = bool(raw & (1 << 2))
        self._state.ev_comm_mid_meter_online = bool(raw & (1 << 3))
        self._state.ev_comm_gw_meter_online = bool(raw & (1 << 4))
        self._state.ev_comm_ems_online = bool(raw & (1 << 5))
