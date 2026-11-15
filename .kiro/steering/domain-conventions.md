# Domain conventions

Signs and units used across the app. Verified against the code and live data; do not change them.

- **Grid current** is signed: positive is import, negative is export.
- **Home battery power**: positive is charging, negative is discharging. The code is verified correct. Never change the sign handling.
- **HA `sensor.reverse_battery_power`** is positive when discharging (inverted). Negate it only in the test loaders (`tests/unit/replay_data.py`), never in app code.
- **EV setpoint** is a desired total in W. `EVChargerModbusClient.write_setpoint` rounds it to 100 W (register 10029, minimum raw 44 = 4.4 kW). The car may ignore it, lag it, or step in uneven whole-amp increments.
- **EV power register 10015** has 0.1 kW resolution (U16, gain 10). The app multiplies the power and the three phase currents by the correction factor `1 + correction_pct / 100` (capped at 10 %). Voltages are not corrected. Published power sits about 3 % below the sum of V x I; no control decision uses the EV power magnitude. Details: `.agents/tasks/ev-power-undershoot-investigation.md` (local, not committed).
- **Timezone**: the site is Australia/Perth (UTC+8; `docker-compose.yml` sets `TZ`). HA history exports are in UTC.
- **Standby** and the charger status register are specified in `AGENTS.md`: no EV Modbus I/O in Standby except the listed on-demand runtime writes.
