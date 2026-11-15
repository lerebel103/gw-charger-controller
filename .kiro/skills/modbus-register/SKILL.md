---
name: modbus-register
description: Add or change a GW HCA-G2 charger Modbus register in the app. Use when reading or writing a new charger register or exposing one to Home Assistant.
---

# Add or change a charger register

1. **Look it up** in `resources/HCA.G2.modbus.file.pdf`. Extract text with `pdftotext -layout` (install poppler if missing; `pypdf` is not in the venv).
2. **Record** the address, access mode (R/W), type, gain, unit and range in the code comment or constant next to the `_REG_*` definitions in `app/modbus/ev.py`.
3. **Check existing use**: grep the address in `app/`. The app already writes 10029 (setpoint), 10019, 10023 and 10032.
4. **Use FC03 (holding registers) only.** FC04 makes the charger unresponsive.
5. **Choose the read trigger.** Never every cycle for a new register, and never in Standby. Prefer on connect or on demand.
6. **Standby-exception writes** (user commands from HA only) use a short session: connect, write, read back to confirm, disconnect. They must not re-enable normal control-loop EV Modbus activity. The rules are in `AGENTS.md`; update its register list if you add one.
7. **HA commands**: validate in the parser in `app/ha/client.py` (range, type, allowed values) before touching state or the charger. Add the entity in `app/ha/` discovery if it is exposed.
8. **Tests**: assert by call count (reads and writes issued per cycle, zero in Standby) as well as values. See `tests/unit/test_modbus_ev.py`.
9. Run the verification commands from the dev-workflow steering.
