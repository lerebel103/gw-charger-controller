# 2026-11-15 EV SOC estimator fixture

Home Assistant history export "history-19" (UTC; the site is Australia/Perth, UTC+8). Used by
`tests/unit/test_soc_estimator_replay.py` through `tests/unit/replay_data.py` (forward-fills each entity, since HA
only records changes). No credentials or hostnames are included.

- `ev_soc_energy.csv`: **RECORDED**, all 115 data rows of the export unchanged, with the original header.
  - `sensor.ev_charger_ev_soc`: 40.0 -> 48.5 % in 0.5 % steps, one step about every 6 min at about 4.4 kW
    (16:00-18:15Z), then 48.5 -> 43 % while the car was driven.
  - `sensor.ev_charger_session_energy` (Wh, charger register, 0.1 kWh resolution): 100 -> 6800 Wh. It persists after
    the charge ends, so only deltas are meaningful.
  - `number.ev_charger_ev_battery_capacity`: 82 kWh.
  - At 23:22:47Z the SOC, capacity and session energy all go `unavailable` for about 3 s (integration restart).
    `load_replay` drops non-numeric states, so forward-fill hides the blip. The blip test is synthetic (`None`).

Not recorded, so **SYNTHESISED inside the tests**: charger power (4224 W while the energy register moved in the last
120 s, else 0 W), charger status (charging in progress), and the frozen/stale SOC variants of the synthetic stall and
stale replays. The history-19 healthy replay uses the recorded SOC and energy as they are.

Measured from the recording: first genuine SOC change 40.5 % at 16:34:06Z (200 Wh), last 48.5 % at 18:13:02Z
(6800 Wh). That is 6600 Wh over 8.0 pp, so k_span = 825 Wh/pp x 100 / 82000 Wh = 1.006.
