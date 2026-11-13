"""Probe CSV schema must stay stable when a register block read fails."""

import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "victron_probe.py"


def _load():
    spec = importlib.util.spec_from_file_location("victron_probe", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
async def test_sample_keeps_vxi_fields_when_voltage_current_block_fails():
    probe = _load()

    async def fake_read(address, count, device_id):
        resp = MagicMock()
        if address == 2616:
            resp.isError.return_value = True
            resp.registers = []
        else:
            resp.isError.return_value = False
            resp.registers = [0] * count
        return resp

    client = MagicMock()
    client.read_holding_registers = AsyncMock(side_effect=fake_read)
    row = await probe.sample(client, 30)
    for ph in ("l1", "l2", "l3"):
        assert row[f"vxi_{ph}_va"] is None
        assert row[f"v_{ph}"] is None
    assert probe.fmt(row)
