# 2026-11-15 eco-day ramp incident fixtures

Home Assistant history exports (UTC; the user is in Perth, UTC+8). Used by
`tests/unit/test_eco_ramp_realdata_replay.py` through `tests/unit/replay_data.py` (forward-fills each entity, since HA
only records changes). No credentials or hostnames are included.

- `min_charge_on_flat_4400.csv`: Eco day, "Eco day min charge" ON, 05:50-07:06 UTC. The recorded production setpoint
  (`sensor.ev_charger_charge_setpoint`) is 0 until 05:53:05 and then flat 4400 W for over an hour while the home battery
  was 98.5-100 % and the site exported. High-rate entities (voltages, battery power, grid currents) are trimmed to the
  last value per 5 s control cycle. PV is omitted (not used by the controller).
- `min_charge_on_ev_soc_synthesised.csv`: **SYNTHESISED, not recorded.** The real EV SOC export for this window was
  never saved. Built from known facts: 26.0 until 05:52, 25.5 at 05:52, then +0.5 % steps (linear in time) reaching
  31.5 at 07:03.
- `min_charge_off_stall_4910.csv`: Eco day, min charge switched OFF, 07:19-07:24 UTC. Setpoint rose 4400 -> 4720 ->
  ... -> 4909.856 W and stalled (v0.1.13 "EV is following" guard against the car's whole-amp steps). PV, battery, grid
  currents and EV voltages are subsampled (every ~10-30 s). Grid voltages are omitted (239.1-241.6 V); the loader uses
  240 V. EV SOC rows (32.5, 33.0) are recorded.

- `golden_correct_ramp_2026-11-12.csv`: export "history-13", 2026-11-12 07:30-09:40 UTC, the ramp working correctly
  (EV SOC 69.5-79.5 %, above Min EV SOC). Setpoint 4400 W at 07:35:41, +40 W per cycle (~6.45 s) to a 6400 W peak at
  07:41:42, back to 4400 W by ~08:12 as PV fades; the car followed (6 -> ~9 A). Voltages kept per 30 s, other
  high-rate entities per 5 s; PV and headroom omitted.

Sign convention: `sensor.reverse_battery_power` is positive while the home battery DISCHARGES (CSV 3: +2.5 kW while
the SOC falls 99.9 -> 93.8 %). The loader negates it into `solar_battery_power_w` (+ charging).

In CSV 1 and CSV 2 the car stayed at ~4224 W / ~6 A, so the replay feeds the recorded EV values open loop.
