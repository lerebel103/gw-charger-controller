"""Unit tests for VictronModbusClient."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modbus_victron import VictronModbusClient, _uint16_to_int16
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
# int16 conversion
# ---------------------------------------------------------------------------


class TestUint16ToInt16:
    def test_positive(self):
        assert _uint16_to_int16(100) == 100

    def test_zero(self):
        assert _uint16_to_int16(0) == 0

    def test_negative(self):
        # 0xFFFF = -1 as int16
        assert _uint16_to_int16(0xFFFF) == -1

    def test_large_negative(self):
        # 0x8000 = -32768
        assert _uint16_to_int16(0x8000) == -32768

    def test_max_positive(self):
        # 0x7FFF = 32767
        assert _uint16_to_int16(0x7FFF) == 32767


# ---------------------------------------------------------------------------
# VictronModbusClient
# ---------------------------------------------------------------------------


class TestVictronModbusClient:
    def _make_state(self, **overrides) -> AppState:
        defaults = {"victron_ip": "192.168.1.30", "victron_port": 502}
        defaults.update(overrides)
        return AppState(**defaults)

    # --- _needs_reconnect ---

    def test_needs_reconnect_no_client(self):
        state = self._make_state()
        client = VictronModbusClient(state)
        assert client._needs_reconnect() is True

    def test_needs_reconnect_ip_changed(self):
        state = self._make_state()
        vc = VictronModbusClient(state)
        vc._client = MagicMock(connected=True)
        vc._connected_ip = "192.168.1.30"
        vc._connected_port = 502
        state.victron_ip = "10.0.0.1"
        assert vc._needs_reconnect() is True

    def test_needs_reconnect_port_changed(self):
        state = self._make_state()
        vc = VictronModbusClient(state)
        vc._client = MagicMock(connected=True)
        vc._connected_ip = "192.168.1.30"
        vc._connected_port = 502
        state.victron_port = 503
        assert vc._needs_reconnect() is True

    def test_no_reconnect_when_connected_same_params(self):
        state = self._make_state()
        vc = VictronModbusClient(state)
        vc._client = MagicMock(connected=True)
        vc._connected_ip = "192.168.1.30"
        vc._connected_port = 502
        assert vc._needs_reconnect() is False

    # --- _read_registers ---

    @pytest.mark.asyncio
    async def test_read_registers_updates_state(self):
        state = self._make_state(victron_grid_meter_unit_id=30)
        vc = VictronModbusClient(state)

        mock_client = AsyncMock()
        vc._client = mock_client

        # Grid L1=500, L2=300, L3=-100 (as uint16 for -100: 0xFF9C = 65436)
        grid_resp = _make_response([500, 300, 65436])
        # Battery power = -200 (uint16: 0xFF38 = 65336), SOC = 75
        batt_resp = _make_response([65336, 75])
        # Voltages: 2300 (230.0V), 2310 (231.0V), 2290 (229.0V)
        v1_resp = _make_response([2300])
        v2_resp = _make_response([2310])
        v3_resp = _make_response([2290])

        mock_client.read_holding_registers = AsyncMock(side_effect=[grid_resp, batt_resp, v1_resp, v2_resp, v3_resp])

        await vc._read_registers()

        # grid_power_w = 500 + 300 + (-100) = 700
        assert state.grid_power_w == 700.0
        # battery_power = -200
        assert state.solar_battery_power_w == -200.0
        assert state.solar_battery_soc_pct == 75.0
        assert state.victron_l1_voltage_v == 230.0
        assert state.victron_l2_voltage_v == 231.0
        assert state.victron_l3_voltage_v == 229.0

    @pytest.mark.asyncio
    async def test_read_registers_grid_error_raises(self):
        state = self._make_state()
        vc = VictronModbusClient(state)
        mock_client = AsyncMock()
        vc._client = mock_client

        mock_client.read_holding_registers = AsyncMock(return_value=_make_error_response())

        from pymodbus.exceptions import ModbusException

        with pytest.raises(ModbusException):
            await vc._read_registers()

    @pytest.mark.asyncio
    async def test_read_registers_battery_error_raises(self):
        state = self._make_state()
        vc = VictronModbusClient(state)
        mock_client = AsyncMock()
        vc._client = mock_client

        grid_resp = _make_response([0, 0, 0])
        batt_resp = _make_error_response()

        mock_client.read_holding_registers = AsyncMock(side_effect=[grid_resp, batt_resp])

        from pymodbus.exceptions import ModbusException

        with pytest.raises(ModbusException):
            await vc._read_registers()

    @pytest.mark.asyncio
    async def test_read_registers_voltage_error_raises(self):
        state = self._make_state(victron_grid_meter_unit_id=30)
        vc = VictronModbusClient(state)
        mock_client = AsyncMock()
        vc._client = mock_client

        grid_resp = _make_response([0, 0, 0])
        batt_resp = _make_response([0, 50])
        v1_resp = _make_error_response()

        mock_client.read_holding_registers = AsyncMock(side_effect=[grid_resp, batt_resp, v1_resp])

        from pymodbus.exceptions import ModbusException

        with pytest.raises(ModbusException):
            await vc._read_registers()

    # --- reconnect ---

    @pytest.mark.asyncio
    async def test_reconnect_empty_ip_skips(self):
        state = self._make_state(victron_ip="")
        vc = VictronModbusClient(state)
        await vc.reconnect()
        assert vc._client is None

    @pytest.mark.asyncio
    async def test_reconnect_success(self):
        state = self._make_state()
        vc = VictronModbusClient(state)

        mock_client_instance = AsyncMock()
        mock_client_instance.connect = AsyncMock(return_value=True)
        mock_client_instance.connected = True

        with patch(
            "app.modbus_victron.AsyncModbusTcpClient",
            return_value=mock_client_instance,
        ):
            await vc.reconnect()

        assert vc._client is mock_client_instance
        assert vc._connected_ip == "192.168.1.30"
        assert vc._connected_port == 502

    @pytest.mark.asyncio
    async def test_reconnect_retries_on_failure(self):
        state = self._make_state()
        vc = VictronModbusClient(state)

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
            patch("app.modbus_victron.AsyncModbusTcpClient", side_effect=make_client),
            patch("app.modbus_victron.exponential_backoff", return_value=0.0),
        ):
            await vc.reconnect()

        assert vc._client is success_client
        assert call_count == 3

    # --- _close ---

    @pytest.mark.asyncio
    async def test_close_clears_client(self):
        state = self._make_state()
        vc = VictronModbusClient(state)
        mock_client = MagicMock()
        vc._client = mock_client
        await vc._close()
        mock_client.close.assert_called_once()
        assert vc._client is None

    # --- poll_loop (single iteration) ---

    @pytest.mark.asyncio
    async def test_poll_loop_reads_on_connected(self):
        """Verify poll_loop calls _read_registers when connected."""
        state = self._make_state()
        vc = VictronModbusClient(state, poll_interval_s=0.01)

        mock_client = AsyncMock()
        mock_client.connected = True
        vc._client = mock_client
        vc._connected_ip = state.victron_ip
        vc._connected_port = state.victron_port

        iteration = 0

        async def fake_read():
            nonlocal iteration
            iteration += 1
            if iteration >= 1:
                raise asyncio.CancelledError

        with patch.object(vc, "_read_registers", side_effect=fake_read), pytest.raises(asyncio.CancelledError):
            await vc.poll_loop()

        assert iteration == 1

    @pytest.mark.asyncio
    async def test_retains_last_values_on_failure(self):
        """On read failure, last known values are retained in AppState."""
        state = self._make_state()
        state.grid_power_w = 1000.0
        state.solar_battery_power_w = 500.0
        state.solar_battery_soc_pct = 80.0

        vc = VictronModbusClient(state)
        mock_client = AsyncMock()
        mock_client.connected = True
        vc._client = mock_client
        vc._connected_ip = state.victron_ip
        vc._connected_port = state.victron_port

        # Simulate a read failure
        from pymodbus.exceptions import ModbusException

        mock_client.read_holding_registers = AsyncMock(side_effect=ModbusException("timeout"))

        import contextlib

        # Patch reconnect to avoid infinite loop; run one iteration manually
        with (
            patch.object(vc, "reconnect", new_callable=AsyncMock),
            patch.object(vc, "_needs_reconnect", return_value=False),
            contextlib.suppress(ModbusException, OSError),
        ):
            await vc._read_registers()

        # Values should be retained
        assert state.grid_power_w == 1000.0
        assert state.solar_battery_power_w == 500.0
        assert state.solar_battery_soc_pct == 80.0
