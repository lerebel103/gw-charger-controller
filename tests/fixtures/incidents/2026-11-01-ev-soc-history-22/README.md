# 2026-11-01 to 2026-11-05 EV SOC history-22 fixture

Home Assistant history export "history-22" (UTC; the site is Australia/Perth, UTC+8). Window: 2026-11-01T17:00Z to
2026-11-05T09:10Z. Used by `tests/unit/test_soc_estimator_history22.py` through `tests/unit/soc_sim.py`.
No credentials or hostnames are included.

`ev_soc_energy.csv` is **RECORDED**, all 444 data rows of the export, unchanged, with the original header. Entities:
`sensor.ev_charger_ev_soc` (%, 0.5 steps), `sensor.ev_charger_session_energy` (Wh, 0.1 kWh resolution, resets at
each new session and persists after the charge ends), `number.ev_charger_ev_battery_capacity` (82 kWh).
At 2026-11-04T23:48:22-26Z (07:48 local on 11-05) all three entities go `unavailable` for about 2-5 s (integration
restart), outside any session. The loader skips non-numeric states.
There is no charger power, status or phase data in this export. The replays derive the cloud outages (frozen or
stale SOC) synthetically, see the test module.

## Sessions (energy-register resets), local time

| ID | Local start | Local end | Min | Start SOC | End SOC | kWh | Avg kW | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| S1 | 11-02 17:16 | 17:21 | 5 | 70.5 (drops to 70.0) | 70.0 | 0.30 | 3.9 | aborted start, no SOC step |
| S2 | 11-03 00:30 | 02:36 | 125 | 70.0 | 80.0 | 8.40 | 4.0 | real charge |
| S3 | 11-04 00:30 | 00:41 | 11 | 79.0 | 80.0 | 0.70 | 3.9 | top-up |
| S4 | 11-04 13:18 | 14:51 | 93 | 67.5 | 80.0 | 11.10 | 7.2 | real charge, fast |
| S5 | 11-05 00:30 | 00:35 | 5 | 79.0 | 80.0 | 0.30 | 3.9 | top-up |
| S6 | 11-05 15:26 | 17:09 | 104 | 69.5 | 78.0 | 7.40 | 4.3 | real charge |

The first row of the energy entity (5900 Wh at 11-01) is a stray value (5900 -> 1000 Wh, no SOC change) from before the first reset. The test segmentation counts it as session 0 and ignores it.
Energy per reported SOC percent is 0.836 to 0.865 kWh at 82 kWh (k about 1.02 to 1.06).
