---
name: incident-replay
description: Turn a Home Assistant history CSV (entity_id,state,last_changed) into an incident fixture and open-loop replay tests through ControlLoop.run_loop. Use when investigating or reproducing a control-loop incident from recorded data.
---

# Incident replay

1. **Find the data** in `incoming/` (see the dev-workflow steering). Never rely on chat attachments.
2. **Trim** the CSV to the incident window plus a lead-in, and save it under `tests/fixtures/incidents/<date>-<topic>/`. Add a `README.md` there. Label any synthesised series in both the file name and the README (for example `*_synthesised.csv`). Timestamps are UTC.
3. **Load** with `tests/unit/replay_data.py`: `load_replay(*paths)` forward-fills every entity (HA only records changes), `cycles(series, start, end, step_s)` resamples onto control cycles, and `cycle_inputs(row)` maps entities to AppState fields. Extend its entity mapping rather than writing a new loader. `sensor.reverse_battery_power` is inverted: negate it only in the loader.
4. **Drive the real loop** with `run_scenario(...)` in `tests/unit/eco_replay_harness.py` (fake clock, patched sleep, mocked clients). Open loop: pass `ev_fields` with recorded EV values. Closed loop: pass a `car` model (`StepAmpCar`, `LaggingCar`, `IgnoringCar`, ...). `tests/unit/test_eco_ramp_realdata_replay.py` (`run_recorded`) is the worked example.
5. **Assert** the setpoint's response to site signals (grid, battery, breaker). Never assert against the car's draw.
6. **Prove the tests fail on main**: `git archive <main-sha> | tar -x -C /tmp/<dir>`, copy in the new tests and fixtures, run pytest. Record which tests fail and why.
7. Tests that reproduce old main behaviour are opt-in: guard them with `GW_REPLAY_LEGACY=1` (see `test_eco_ramp_realdata_replay.py`).
8. Run the verification commands from the dev-workflow steering.
