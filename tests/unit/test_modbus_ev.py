"""Unit tests for EVChargerModbusClient."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modbus_ev import EVChargerModbusClient
from app.state import AppState

# ---------------------------------------------------------------------------
# Helper: build a fake Modbus response
# ---------------------------------------------------------------------------


def _make_response(registers: list[int], *, is_error: bool = False):
    resp = MagicMock()
    resp.isError.return_value = is_error
    resp.registers = registers
    return resp


def _make_error_response():
    return _make_response([], is_error=True)


# ---------------------------------------------------------------------------
# EVChargerModbusClient
# ---------------------------------------------------------------------------


class TestEVChargerModbusClient:
    def _make_state(self, **overrides) -> AppState:
        defaults = {"ev_charger_ip": "192.168.1.20", "ev_charger_port": 502}
        defaults.update(overrides)
        return AppState(**defaults)

    # --- _needs_reconnect ---

    def test_needs_reconnect_no_client(self):
        state = self._make_state()
        client = EVChargerModbusClient(state)
        assert client._needs_reconnect() is True

    def test_needs_reconnect_ip_changed(self):
        state = self._make_state()
        ec = EVChargerModbusClient(state)
        ec._client = MagicMock(connected=True)
        ec._connected_ip = "192.168.1.20"
        ec._connected_port = 502
        state.ev_charger_ip = "10.0.0.1"
        assert ec._needs_reconnect() is True

    def test_needs_reconnect_port_changed(self):
        state = self._make_state()
        ec = EVChargerModbusClient(state)
        ec._client = MagicMock(connected=True)
        ec._connected_ip = "192.168.1.20"
        ec._connected_port = 502
        state.ev_charger_port = 503
        assert ec._needs_reconnect() is True

    def test_no_reconnect_when_connected_same_params(self):
        state = self._make_state()
        ec = EVChargerModbusClient(state)
        ec._client = MagicMock(connected=True)
        ec._connected_ip = "192.168.1.20"
        ec._connected_port = 502
        assert ec._needs_reconnect() is False

    # --- _read_registers ---

    @pytest.mark.asyncio
    async def test_read_registers_updates_state(self):
        state = self._make_state()
        # Set Victron voltages so voltage drop can be computed
        state.victron_l1_voltage_v = 230.0
        state.victron_l2_voltage_v = 231.0
        state.victron_l3_voltage_v = 229.0

        ec = EVChargerModbusClient(state)
        mock_client = AsyncMock()
        ec._client = mock_client

        # Contiguous block 10009–10017 (9 registers):
        # 10009=2280 (228.0V), 10010=2290 (229.0V), 10011=2270 (227.0V)
        # 10012=160 (16.0A), 10013=155 (15.5A), 10014=158 (15.8A)
        # 10015=110 (11.0kW → 11000W), 10016=50 (5.0kWh → 5000Wh)
        # 10017=3 (charging)
        main_resp = _make_response([2280, 2290, 2270, 160, 155, 158, 110, 50, 3])
        # Completion time = 2 hours
        ct_resp = _make_response([2])
        # Total energy U32: high=0, low=1500 → raw_u32=1500 → 150.0 kWh → 150000 Wh
        te_resp = _make_response([0, 1500])
        # Car connection = 2 (connected)
        cc_resp = _make_response([2])

        mock_client.read_holding_registers = AsyncMock(side_effect=[main_resp, ct_resp, te_resp, cc_resp])

        await ec._read_registers()

        assert state.ev_voltage_l1_v == 228.0
        assert state.ev_voltage_l2_v == 229.0
        assert state.ev_voltage_l3_v == 227.0
        assert state.ev_current_a == 16.0
        assert state.ev_current_b == 15.5
        assert state.ev_current_c == 15.8
        assert state.ev_active_power_w == 11000.0
        assert state.ev_session_energy_wh == 5000.0
        assert state.ev_charger_status == 3
        assert state.ev_completion_time_h == 2
        assert state.ev_total_energy_wh == 150000.0
        assert state.ev_connected is True

        # Voltage drops: 100 * (victron - ev) / victron
        assert state.l1_voltage_drop_pct == pytest.approx(100.0 * (230.0 - 228.0) / 230.0)
        assert state.l2_voltage_drop_pct == pytest.approx(100.0 * (231.0 - 229.0) / 231.0)
        assert state.l3_voltage_drop_pct == pytest.approx(100.0 * (229.0 - 227.0) / 229.0)

    @pytest.mark.asyncio
    async def test_read_registers_car_disconnected(self):
        state = self._make_state()
        ec = EVChargerModbusClient(state)
        mock_client = AsyncMock()
        ec._client = mock_client

        main_resp = _make_response([0, 0, 0, 0, 0, 0, 0, 0, 0])
        ct_resp = _make_response([0])
        te_resp = _make_response([0, 0])
        cc_resp = _make_response([0])  # disconnected

        mock_client.read_holding_registers = AsyncMock(side_effect=[main_resp, ct_resp, te_resp, cc_resp])

        await ec._read_registers()
        assert state.ev_connected is False

    @pytest.mark.asyncio
    async def test_read_registers_main_error_raises(self):
        state = self._make_state()
        ec = EVChargerModbusClient(state)
        mock_client = AsyncMock()
        ec._client = mock_client

        mock_client.read_holding_registers = AsyncMock(return_value=_make_error_response())

        from pymodbus.exceptions import ModbusException

        with pytest.raises(ModbusException):
            await ec._read_registers()

    @pytest.mark.asyncio
    async def test_read_registers_completion_time_error_raises(self):
        state = self._make_state()
        ec = EVChargerModbusClient(state)
        mock_client = AsyncMock()
        ec._client = mock_client

        main_resp = _make_response([0, 0, 0, 0, 0, 0, 0, 0, 0])
        ct_resp = _make_error_response()

        mock_client.read_holding_registers = AsyncMock(side_effect=[main_resp, ct_resp])

        from pymodbus.exceptions import ModbusException

        with pytest.raises(ModbusException):
            await ec._read_registers()

    @pytest.mark.asyncio
    async def test_read_registers_car_connection_error_raises(self):
        state = self._make_state()
        ec = EVChargerModbusClient(state)
        mock_client = AsyncMock()
        ec._client = mock_client

        main_resp = _make_response([0, 0, 0, 0, 0, 0, 0, 0, 0])
        ct_resp = _make_response([0])
        te_resp = _make_response([0, 0])
        cc_resp = _make_error_response()

        mock_client.read_holding_registers = AsyncMock(side_effect=[main_resp, ct_resp, te_resp, cc_resp])

        from pymodbus.exceptions import ModbusException

        with pytest.raises(ModbusException):
            await ec._read_registers()

    # --- voltage drop computation ---

    @pytest.mark.asyncio
    async def test_voltage_drop_none_when_victron_unavailable(self):
        state = self._make_state()
        # Victron voltages are None (default)
        ec = EVChargerModbusClient(state)
        mock_client = AsyncMock()
        ec._client = mock_client

        main_resp = _make_response([2300, 2310, 2290, 0, 0, 0, 0, 0, 0])
        ct_resp = _make_response([0])
        te_resp = _make_response([0, 0])
        cc_resp = _make_response([2])

        mock_client.read_holding_registers = AsyncMock(side_effect=[main_resp, ct_resp, te_resp, cc_resp])

        await ec._read_registers()

        assert state.l1_voltage_drop_pct is None
        assert state.l2_voltage_drop_pct is None
        assert state.l3_voltage_drop_pct is None

    # --- total energy conversion ---

    @pytest.mark.asyncio
    async def test_read_registers_total_energy_conversion(self):
        """U32 total energy: high=0, low=1500 → raw_u32=1500 → 150000.0 Wh."""
        state = self._make_state()
        ec = EVChargerModbusClient(state)
        mock_client = AsyncMock()
        ec._client = mock_client

        main_resp = _make_response([0, 0, 0, 0, 0, 0, 0, 0, 0])
        ct_resp = _make_response([0])
        # U32: high=0, low=1500 → (0 << 16) | 1500 = 1500 → 1500/10*1000 = 150000 Wh
        te_resp = _make_response([0, 1500])
        cc_resp = _make_response([2])

        mock_client.read_holding_registers = AsyncMock(side_effect=[main_resp, ct_resp, te_resp, cc_resp])

        await ec._read_registers()
        assert state.ev_total_energy_wh == 150000.0

    @pytest.mark.asyncio
    async def test_read_registers_total_energy_large_u32(self):
        """U32 total energy with high word: high=1, low=0 → 65536 → 6553600.0 Wh."""
        state = self._make_state()
        ec = EVChargerModbusClient(state)
        mock_client = AsyncMock()
        ec._client = mock_client

        main_resp = _make_response([0, 0, 0, 0, 0, 0, 0, 0, 0])
        ct_resp = _make_response([0])
        # U32: high=1, low=0 → (1 << 16) | 0 = 65536 → 65536/10*1000 = 6553600 Wh
        te_resp = _make_response([1, 0])
        cc_resp = _make_response([2])

        mock_client.read_holding_registers = AsyncMock(side_effect=[main_resp, ct_resp, te_resp, cc_resp])

        await ec._read_registers()
        assert state.ev_total_energy_wh == 6553600.0

    # --- write_enable ---

    @pytest.mark.asyncio
    async def test_write_enable_true_writes_1(self):
        """write_enable(True) writes value 1 to register 10060."""
        state = self._make_state()
        state.ev_connected = True
        ec = EVChargerModbusClient(state)

        mock_client = AsyncMock()
        mock_client.connected = True
        write_resp = _make_response([])
        mock_client.write_register = AsyncMock(return_value=write_resp)
        ec._client = mock_client

        await ec.write_enable(True)

        mock_client.write_register.assert_called_once_with(address=10060, value=2, slave=247)

    @pytest.mark.asyncio
    async def test_write_enable_false_writes_0(self):
        """write_enable(False) writes value 0 to register 10060."""
        state = self._make_state()
        state.ev_connected = True
        ec = EVChargerModbusClient(state)

        mock_client = AsyncMock()
        mock_client.connected = True
        write_resp = _make_response([])
        mock_client.write_register = AsyncMock(return_value=write_resp)
        ec._client = mock_client

        await ec.write_enable(False)

        mock_client.write_register.assert_called_once_with(address=10060, value=1, slave=247)

    @pytest.mark.asyncio
    async def test_write_enable_skips_when_modbus_disconnected(self):
        """write_enable skips when client is None."""
        state = self._make_state()
        state.ev_connected = True
        ec = EVChargerModbusClient(state)
        ec._client = None

        await ec.write_enable(True)
        # Should not raise

    @pytest.mark.asyncio
    async def test_write_enable_skips_when_ev_not_connected(self):
        """write_enable skips when ev_connected is False."""
        state = self._make_state()
        state.ev_connected = False
        ec = EVChargerModbusClient(state)

        mock_client = AsyncMock()
        mock_client.connected = True
        ec._client = mock_client

        await ec.write_enable(True)

        mock_client.write_register.assert_not_called()

    # --- write_setpoint ---

    @pytest.mark.asyncio
    async def test_write_setpoint_success(self):
        state = self._make_state()
        state.ev_connected = True
        ec = EVChargerModbusClient(state)

        mock_client = AsyncMock()
        mock_client.connected = True
        write_resp = _make_response([])
        mock_client.write_register = AsyncMock(return_value=write_resp)
        ec._client = mock_client

        await ec.write_setpoint(5000)

        mock_client.write_register.assert_called_once_with(address=10029, value=50, slave=247)

    @pytest.mark.asyncio
    async def test_write_setpoint_low_value(self):
        state = self._make_state()
        state.ev_connected = True
        ec = EVChargerModbusClient(state)

        mock_client = AsyncMock()
        mock_client.connected = True
        write_resp = _make_response([])
        mock_client.write_register = AsyncMock(return_value=write_resp)
        ec._client = mock_client

        # 500W → raw = round(500/100) = 5, but min is 42 (4.2 kW)
        await ec.write_setpoint(500)

        mock_client.write_register.assert_called_once_with(address=10029, value=42, slave=247)

    @pytest.mark.asyncio
    async def test_write_setpoint_skips_when_not_connected(self):
        state = self._make_state()
        state.ev_connected = False
        ec = EVChargerModbusClient(state)

        mock_client = AsyncMock()
        mock_client.connected = True
        ec._client = mock_client

        await ec.write_setpoint(3680)

        mock_client.write_register.assert_not_called()

    @pytest.mark.asyncio
    async def test_write_setpoint_skips_when_modbus_disconnected(self):
        state = self._make_state()
        state.ev_connected = True
        ec = EVChargerModbusClient(state)
        ec._client = None

        await ec.write_setpoint(3680)
        # Should not raise

    # --- reconnect ---

    @pytest.mark.asyncio
    async def test_reconnect_empty_ip_skips(self):
        state = self._make_state(ev_charger_ip="")
        ec = EVChargerModbusClient(state)
        await ec.reconnect()
        assert ec._client is None

    @pytest.mark.asyncio
    async def test_reconnect_success(self):
        state = self._make_state()
        ec = EVChargerModbusClient(state)

        mock_client_instance = AsyncMock()
        mock_client_instance.connect = AsyncMock(return_value=True)
        mock_client_instance.connected = True

        with patch(
            "app.modbus_ev.AsyncModbusTcpClient",
            return_value=mock_client_instance,
        ):
            await ec.reconnect()

        assert ec._client is mock_client_instance
        assert ec._connected_ip == "192.168.1.20"
        assert ec._connected_port == 502

    @pytest.mark.asyncio
    async def test_reconnect_retries_on_failure(self):
        state = self._make_state()
        ec = EVChargerModbusClient(state)

        fail_client = AsyncMock()
        fail_client.connect = AsyncMock(return_value=False)

        success_client = AsyncMock()
        success_client.connect = AsyncMock(return_value=True)
        success_client.connected = True

        call_count = 0

        def make_client(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return fail_client
            return success_client

        with (
            patch("app.modbus_ev.AsyncModbusTcpClient", side_effect=make_client),
            patch("app.modbus_ev.exponential_backoff", return_value=0.0),
        ):
            await ec.reconnect()

        assert ec._client is success_client
        assert call_count == 3

    # --- _close ---

    @pytest.mark.asyncio
    async def test_close_clears_client(self):
        state = self._make_state()
        ec = EVChargerModbusClient(state)
        mock_client = MagicMock()
        ec._client = mock_client
        await ec._close()
        mock_client.close.assert_called_once()
        assert ec._client is None

    # --- poll_loop (single iteration) ---

    @pytest.mark.asyncio
    async def test_poll_loop_reads_on_connected(self):
        """Verify poll_loop calls _read_registers when connected."""
        state = self._make_state()
        ec = EVChargerModbusClient(state, poll_interval_s=0.01)

        mock_client = AsyncMock()
        mock_client.connected = True
        ec._client = mock_client
        ec._connected_ip = state.ev_charger_ip
        ec._connected_port = state.ev_charger_port

        iteration = 0

        async def fake_read():
            nonlocal iteration
            iteration += 1
            if iteration >= 1:
                raise asyncio.CancelledError

        with patch.object(ec, "_read_registers", side_effect=fake_read), pytest.raises(asyncio.CancelledError):
            await ec.poll_loop()

        assert iteration == 1

    @pytest.mark.asyncio
    async def test_retains_last_values_on_failure(self):
        """On read failure, last known values are retained in AppState."""
        state = self._make_state()
        state.ev_active_power_w = 5000.0
        state.ev_session_energy_wh = 2000.0
        state.ev_connected = True

        ec = EVChargerModbusClient(state)
        mock_client = AsyncMock()
        mock_client.connected = True
        ec._client = mock_client
        ec._connected_ip = state.ev_charger_ip
        ec._connected_port = state.ev_charger_port

        from pymodbus.exceptions import ModbusException

        mock_client.read_holding_registers = AsyncMock(side_effect=ModbusException("timeout"))

        import contextlib

        with (
            patch.object(ec, "reconnect", new_callable=AsyncMock),
            patch.object(ec, "_needs_reconnect", return_value=False),
            contextlib.suppress(ModbusException, OSError),
        ):
            await ec._read_registers()

        assert state.ev_active_power_w == 5000.0
        assert state.ev_session_energy_wh == 2000.0
        assert state.ev_connected is True
