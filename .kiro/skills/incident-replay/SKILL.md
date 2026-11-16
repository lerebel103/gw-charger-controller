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

## SOC-estimator fixtures and outage simulation
Worked example: `tests/fixtures/incidents/2026-11-01-ev-soc-history-22/` (`ev_soc_energy.csv`, `README.md`), `tests/unit/soc_sim.py` and `tests/unit/test_soc_estimator_history22.py`. The `ev-soc-estimator` skill has the full procedure.
1. **Fixture**: keep only `sensor.ev_charger_ev_soc`, `sensor.ev_charger_session_energy` and `number.ev_charger_ev_battery_capacity`, unchanged, with the original header. The README must say what is recorded and what is simulated. The export has no power, status or phase data.
2. **Sessions**: split on session-energy resets (`segment_sessions`). Drop aborted starts that show no SOC step.
3. **Outage kinds** (simulated, never recorded): `freeze` keeps publishing the last value, so it stays fresh but stops changing (the HA republish case). `stale` stops all messages, so the value is stale after 300 s.
4. **Anchors**: `cold=False` (warm) plays the plug-in before the session, so the anchor is `LAST_KNOWN`. `cold=True` starts the app with the car already connected, so the anchor is the first reported value. Run both, because they differ by up to 0.3 pp.
5. **True SOC** is the recorded SOC linearly interpolated between step times (`Timeline.reported`). Judge stop error against it, not against the 0.5 pp steps. Extend the tail past the last recorded step (`Timeline`) so the session can reach the limit.
6. **Fake clock**: `soc_sim.FakeClock` and `install_clock(monkeypatch)` replace `power_utils._time`. Never sleep.
7. **Cross-check through `ControlLoop`**: `test_control_loop_stops_on_the_estimate_like_the_driver` runs a few cases through `run_loop` (Manual mode, `run_scenario`) and requires the same stop reason and a stop within one cycle of `soc_sim.run`. If they disagree, suspect a driver mismatch (for example warm versus cold) before the app code.
