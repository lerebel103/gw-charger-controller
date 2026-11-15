# Design decisions (eco ramp, PR #27)

- **Min charge is a floor.** While EV SOC is below `ev_min_soc_pct` (and `eco_day_min_charge_enabled`), the Eco day and Eco night setpoint is at least `_MIN_CHARGE_W` (4400 W), immediately on plug-in. The normal ramp continues above it and does not stop at min SOC. The breaker limiter may still cut below it.
- **Night**: min SOC comes first. The night target is used only when it is higher.
- **Never compare the setpoint with the car's draw.** The ramp is driven by site signals only (grid, battery, breaker). The follow guard was removed on purpose; do not bring it back.
- **No register-based setpoint cap.** The ramp rises naturally, bounded by site signals, the breaker limiter and the 22 kW hard max (`_MAX_CHARGE_W` in `app/control/constants.py`). A 10039/10058 cap was tried and dropped at the user's request (PR #27, commit 739511a). Do not reintroduce one without asking.
- **Register facts (live probe, 2026-11-15)**
  - 10029 is the setpoint register; the app writes it as needed (only when it differs from the last read).
  - 10039 (Max Grid Power Draw) read 0, meaning not set.
  - 10058 read 2, meaning the 22 kW model.
  - The GoodWe app shows 11 kW. Which register holds that value is unresolved (`.agents/tasks/charger-max-register-open.md`, local).
- **Modbus function codes**: the charger does not support FC04 (input registers). An FC04 read leaves it unresponsive for several seconds. Use FC03 (holding registers) only.
- **Live probes** of the charger must be read-only and need the user's permission first. The controller normally holds the connection.
- **`app/control/breaker.py` is off-limits** unless the user explicitly asks.
- **Ramp-down** speed is unchanged. Decision D1 (faster ramp-down) is open; do not change it unasked.
