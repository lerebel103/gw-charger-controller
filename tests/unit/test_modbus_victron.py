"""Unit tests for VictronModbusClient."""

from __future__ import annotations

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

    # --- _config_changed ---

    def test_config_changed_ip_changed(self):
        state = self._make_state()
        vc = VictronModbusClient(state)
        vc._connected_ip = "192.168.1.30"
        vc._connected_port = 502
        state.victron_ip = "10.0.0.1"
        assert vc._config_changed() is True

    def test_config_changed_port_changed(self):
        state = self._make_state()
        vc = VictronModbusClient(state)
        vc._connected_ip = "192.168.1.30"
        vc._connected_port = 502
        state.victron_port = 503
        assert vc._config_changed() is True

    def test_config_changed_false_when_same_params(self):
        state = self._make_state()
        vc = VictronModbusClient(state)
        vc._connected_ip = "192.168.1.30"
        vc._connected_port = 502
        assert vc._config_changed() is False

    # --- connected property ---

    def test_connected_false_no_client(self):
        state = self._make_state()
        vc = VictronModbusClient(state)
        assert vc.connected is False

    def test_connected_true_when_client_connected(self):
        state = self._make_state()
        vc = VictronModbusClient(state)
        vc._client = MagicMock(connected=True)
        assert vc.connected is True

    def test_connected_false_when_client_disconnected(self):
        state = self._make_state()
        vc = VictronModbusClient(state)
        vc._client = MagicMock(connected=False)
        assert vc.connected is False

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
    async def test_reconnect_closes_and_resets(self):
        state = self._make_state()
        vc = VictronModbusClient(state)
        mock_client = MagicMock()
        vc._client = mock_client
        vc._reconnect_attempt = 5
        vc._reconnect_after = 999.0

        await vc.reconnect()

        mock_client.close.assert_called_once()
        assert vc._client is None
        assert vc._reconnect_attempt == 0
        assert vc._reconnect_after == 0.0

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

    # --- read() ---

    @pytest.mark.asyncio
    async def test_read_calls_read_registers_when_connected(self):
        """Verify read() calls _read_registers when connected."""
        state = self._make_state()
        vc = VictronModbusClient(state)

        mock_client = AsyncMock()
        mock_client.connected = True
        vc._client = mock_client

        with patch.object(vc, "_read_registers", new_callable=AsyncMock) as mock_rr:
            await vc.read()

        mock_rr.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_read_skips_when_not_connected(self):
        """read() is a no-op when not connected."""
        state = self._make_state()
        vc = VictronModbusClient(state)

        with patch.object(vc, "_read_registers", new_callable=AsyncMock) as mock_rr:
            await vc.read()

        mock_rr.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_read_closes_on_error(self):
        """read() closes the connection on ModbusException."""
        state = self._make_state()
        vc = VictronModbusClient(state)

        mock_client = AsyncMock()
        mock_client.connected = True
        vc._client = mock_client

        from pymodbus.exceptions import ModbusException

        with patch.object(vc, "_read_registers", side_effect=ModbusException("fail")):
            await vc.read()

        assert vc._client is None

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

        from pymodbus.exceptions import ModbusException

        mock_client.read_holding_registers = AsyncMock(side_effect=ModbusException("timeout"))

        import contextlib

        with contextlib.suppress(ModbusException, OSError):
            await vc._read_registers()

        # Values should be retained
        assert state.grid_power_w == 1000.0
        assert state.solar_battery_power_w == 500.0
        assert state.solar_battery_soc_pct == 80.0
