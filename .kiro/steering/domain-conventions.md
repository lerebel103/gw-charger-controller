# Domain conventions

Signs and units used across the app. Verified against the code and live data; do not change them.

- **Grid current** is signed: positive is import, negative is export.
- **Home battery power**: positive is charging, negative is discharging. The code is verified correct. Never change the sign handling.
- **HA `sensor.reverse_battery_power`** is positive when discharging (inverted). Negate it only in the test loaders (`tests/unit/replay_data.py`), never in app code.
- **EV setpoint** is a desired total in W. `EVChargerModbusClient.write_setpoint` rounds it to 100 W (register 10029, minimum raw 44 = 4.4 kW). The car may ignore it, lag it, or step in uneven whole-amp increments.
- **EV power and current**
  - Register 10015 has 0.1 kW resolution (U16, gain 10), so it moves in 100 W steps and reads about 10 % low on single-phase sessions.
  - The three phase currents are multiplied by the correction factor `1 + correction_pct / 100` (capped at 10 %, currently 5.6 %). Voltages are not corrected.
  - `sensor.ev_charger_power` (`ev_active_power_w`, name "Charge Power (computed)") is the sum over phases of V x corrected I. Do not apply the factor again. It matches real power to about 3 % RMS for both the 1-phase and 3-phase cars.
  - "Charge Power (onboard)" is register 10015 x the correction factor (the old behaviour). It is less accurate and kept for comparison.
  - Details: `ev-calibration` skill.
- **Victron signs for zero-sum analysis**
  - `victron_grid_*` (current and power): positive is import.
  - `victron_vebus_activein_*` (Multiplus AC-in): negative while the Multiplus delivers into the AC-in bus.
  - `victron_pvinverter_*`: positive while generating. Its current is identical on all three phases (derived).
  - The EV step at a start or stop edge is `EV = dGrid - dVebusIn + dPV`, per phase, for current or power.
- **Timezone**: the site is Australia/Perth (UTC+8; `docker-compose.yml` sets `TZ`). HA history exports are in UTC.
- **Standby** and the charger status register are specified in `AGENTS.md`: no EV Modbus I/O in Standby except the listed on-demand runtime writes.
