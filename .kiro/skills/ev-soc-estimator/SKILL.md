---
name: ev-soc-estimator
description: Check, replay or retune the energy-based EV SOC estimator (stall and outage detection, learned k) using a Home Assistant history CSV. Use when the cloud SOC stalls or goes unavailable, when k drifts, or with a new car or battery capacity.
---
# EV SOC estimator
Code: `app/control/soc_estimator.py`, `resolve_ev_soc` and `max_soc_stop_reason` in `app/control/power_utils.py`. Design: `docs/soc-estimator.md`. Tests: `tests/unit/test_soc_estimator_history22.py`, `test_soc_estimator_replay.py` (history-19), `test_soc_estimator.py`, `test_soc_k_persistence.py`, `test_charge_to_max_soc.py`. Helper: `tests/unit/soc_sim.py`.

## Data and k
1. **Data**: an HA history CSV (`entity_id,state,last_changed`, UTC; see the dev-workflow steering) with `sensor.ev_charger_ev_soc`, `sensor.ev_charger_session_energy` and `number.ev_charger_ev_battery_capacity`.
2. **Segment sessions** on session-energy resets (a drop in the register). Use `segment_sessions` in `soc_sim.py`. Drop aborted starts with no SOC step. The energy register persists after a charge ends, so the end of a session is its last energy increase.
3. **kWh per SOC point** per session: ordinary least squares of session kWh against reported SOC over the genuine steps (SOC steps in 0.5 pp). Skip the first step after plug-in, because the cloud value lags the energy by 0.0-0.2 kWh.
4. **Implied k** = `kWh_per_pct * 100 / capacity_kwh`. History-22 (82 kWh): 0.836-0.865 kWh per point, k 1.019-1.055 per session, about 1.04 overall. History-19: k_span 1.006.
5. Trust a session for k only with at least 4 pp and 4 kWh. Scatter of about ±0.02 between sessions matches the 0.5 pp quantisation.

## Replay and outage simulation
- **Healthy replay**: `soc_sim.run(tl, limit, k0, mode="healthy")` over a `Timeline` per session. The tests assert the report never flags a stall and the estimate tracks the report.
- **Outage**: `mode="freeze"` (value fresh but frozen) or `"stale"` (no messages), with `outage_t` anywhere in the session, `cold=False|True`, and `k0` of `None` (cold k 1.0) or a pre-learned value. Sweep outage times and read stop error against the interpolated true SOC.
- **Cross-check** a few cases through `ControlLoop.run_loop` (`LOOP_CASES`). See the `incident-replay` skill for fixture rules.
- Run: `pytest tests/unit/test_soc_estimator_history22.py -q` (about 3 s) with the dev-workflow verification commands.

## Constants (`app/control/constants.py`)
| Constant | Value | Meaning |
| --- | --- | --- |
| `_SOC_STALL_TOLERANCE_PCT` | 1.5 | The report may trail the estimate by this many pp before it counts as stalled (about 1.2 kWh at 82 kWh) |
| `_SOC_ESTIMATE_MARGIN_PCT` | 0.5 | Stop this many pp early, only while the estimate is the SOC source |
| `_SOC_MAX_ENERGY_STEP_WH` | 3000 | A larger single register jump is a discontinuity |
| `_SOC_K_DEFAULT` | 1.0 | Start value, so the set capacity is the starting point |
| `_SOC_K_MIN` / `_SOC_K_MAX` | 0.8 / 1.2 | Clamp; a `k_span` outside it is ignored |
| `_SOC_K_EMA_WEIGHT` | 0.1 | `k += 0.1 * (k_span - k)` per genuine SOC change |
| `_SOC_K_LEARN_MIN_SPAN_PCT` / `_SOC_K_LEARN_MIN_ENERGY_KWH` | 4 / 4 | Minimum learning span |

`k` and its capacity are saved in `config.yaml` (`ev_soc_k`, `ev_soc_k_capacity_kwh`). A k saved for another capacity is ignored. Changing the tolerance for battery ageing or weather is not wanted (user decision).

## Validated results (history-22, three real sessions, 82 kWh)
- Outage starting anywhere from the first third of a session to the stop point, limit 80 %: stops **0.83 pp short to 0.15 pp over** (warm). Cold-start runs reach +0.28 pp over. The user accepts a small overshoot.
- k about 1.04. The default k 1.0 is safe: the estimate runs high and stops about 0.5-0.7 pp short.
- A freeze is flagged after about 1.3 kWh of delivered energy.
- 36 ad hoc cases through `ControlLoop.run_loop` matched the driver in stop reason and timing (within one 20 s cycle). The tests keep six of them (`LOOP_CASES`).

## Known limitations
- **F1**: the 0.5 pp margin applies only once the estimate is the source (stalled or stale). A freeze within about 1.5 pp of the limit stops with no margin: +0.0 to +0.15 pp over warm.
- **F2**: with no earlier SOC observation (app started with the car plugged in) the learning span begins on a possibly stale first value. Cold k came out 0.998, 1.087, 1.026 against about 1.055, 1.036, 1.019, which biases the estimate low.
- **F4**: the 1.5 pp stall tolerance is tight at high power (7.2 kW leads by up to 0.98 pp, more at 11-22 kW with slow cloud updates). The stalled flag can flap, and each stall resets the learning span.
- **F5**: the anchor is quantised to 0.5 pp and lags energy by 0.0-0.2 kWh at plug-in, so a `LAST_KNOWN` anchor can be 0.3-0.5 pp stale. Not simulated.
- The estimate needs the charger's session energy. An app restart mid-charge loses the anchor.

## "Charge to set max SoC" switch
HA switch `ev_charger_charge_to_max_soc` (`charge_to_max_soc_enabled`), on by default and persisted to `config.yaml`. When off, `max_soc_stop_reason` returns None, so neither the reported nor the estimated SOC can end or block a session, in Manual and Eco. Start, ramp and every other stop condition are unchanged. Use it for a car whose SOC is unknown. Tests: `test_charge_to_max_soc.py`.
