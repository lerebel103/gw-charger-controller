#!/usr/bin/env python3
"""Standalone probe: read the exact Victron GX registers the controller uses and log them.

Read-only. Mirrors app/modbus/victron.py:
  unit 100 (com.victronenergy.system):
    820-822  grid L1/L2/L3 power      int16, W   (+ import / - export)
    842      battery power            int16, W
    843      battery SOC              uint16, %
  grid meter unit (default 30, com.victronenergy.grid):
    2616-2621  L1V, L1I, L2V, L2I, L3V, L3I   V: uint16 /10, I: int16 /10
  extra cross-check (NOT used by the controller today):
    2600-2602  grid meter L1/L2/L3 power  int16, W

Raw register words are printed alongside the decoded values so the decoding itself can be verified.

Usage:
  .venv/bin/python scripts/victron_probe.py --host victron.admin.home.lan
  .venv/bin/python scripts/victron_probe.py --host 192.168.1.30 --unit 30 --interval 2 --csv probe.csv
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import datetime as dt
import struct
import sys

from pymodbus.client import AsyncModbusTcpClient

SYSTEM_UNIT = 100


def s16(word: int) -> int:
    """Decode an unsigned 16-bit register word as signed int16."""
    return struct.unpack(">h", struct.pack(">H", word))[0]


async def read(client: AsyncModbusTcpClient, address: int, count: int, unit: int) -> list[int] | None:
    resp = await client.read_holding_registers(address=address, count=count, device_id=unit)
    if resp.isError() or len(resp.registers) < count:
        print(f"  read error addr={address} unit={unit}: {resp}", file=sys.stderr)
        return None
    return list(resp.registers)


async def sample(client: AsyncModbusTcpClient, meter_unit: int) -> dict[str, object]:
    row: dict[str, object] = {"time": dt.datetime.now().isoformat(timespec="milliseconds")}

    sys_grid = await read(client, 820, 3, SYSTEM_UNIT)
    batt = await read(client, 842, 2, SYSTEM_UNIT)
    vi = await read(client, 2616, 6, meter_unit)
    meter_p = await read(client, 2600, 3, meter_unit)

    for i, ph in enumerate(("l1", "l2", "l3")):
        row[f"sys_p_{ph}_w"] = s16(sys_grid[i]) if sys_grid else None
        row[f"meter_p_{ph}_w"] = s16(meter_p[i]) if meter_p else None
        row[f"v_{ph}"] = vi[2 * i] / 10.0 if vi else None
        row[f"i_{ph}"] = s16(vi[2 * i + 1]) / 10.0 if vi else None
        row[f"raw_i_{ph}"] = f"0x{vi[2 * i + 1]:04x}" if vi else None
        # Derived: V x I (signed) for comparison with power. Always present so the CSV schema is stable.
        row[f"vxi_{ph}_va"] = round(row[f"v_{ph}"] * row[f"i_{ph}"], 1) if vi else None  # type: ignore[operator]
    row["batt_p_w"] = s16(batt[0]) if batt else None
    row["batt_soc_pct"] = batt[1] if batt else None
    return row


def fmt(row: dict[str, object]) -> str:
    parts = [str(row["time"])]
    for ph in ("l1", "l2", "l3"):
        parts.append(
            f"{ph.upper()}: sysP={row[f'sys_p_{ph}_w']}W meterP={row[f'meter_p_{ph}_w']}W "
            f"V={row[f'v_{ph}']} I={row[f'i_{ph}']}A ({row[f'raw_i_{ph}']}) VxI={row.get(f'vxi_{ph}_va')}"
        )
    parts.append(f"BATT: P={row['batt_p_w']}W SOC={row['batt_soc_pct']}%")
    return " | ".join(parts)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", required=True, help="Victron GX host or IP")
    ap.add_argument("--port", type=int, default=502)
    ap.add_argument("--unit", type=int, default=30, help="grid meter unit id (victron_grid_meter_unit_id)")
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between samples")
    ap.add_argument("--count", type=int, default=0, help="number of samples (0 = until Ctrl-C)")
    ap.add_argument("--csv", help="optional CSV output path")
    args = ap.parse_args()

    client = AsyncModbusTcpClient(args.host, port=args.port)
    if not await client.connect():
        sys.exit(f"Could not connect to {args.host}:{args.port}")

    writer = None
    fh = None
    n = 0
    try:
        while args.count == 0 or n < args.count:
            row = await sample(client, args.unit)
            print(fmt(row), flush=True)
            if args.csv:
                if writer is None:
                    fh = open(args.csv, "w", newline="")  # noqa: SIM115
                    writer = csv.DictWriter(fh, fieldnames=list(row.keys()))
                    writer.writeheader()
                writer.writerow(row)
                fh.flush()  # type: ignore[union-attr]
            n += 1
            await asyncio.sleep(args.interval)
    finally:
        if fh:
            fh.close()
        client.close()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
