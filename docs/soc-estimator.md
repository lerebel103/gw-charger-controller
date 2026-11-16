# EV SOC estimator

The vehicle SOC arrives from a cloud API through Home Assistant and MQTT. The cloud sometimes stops updating while Home Assistant keeps republishing the last value, so the app sees a fresh but frozen SOC and can charge past `ev_max_soc_pct`. The estimator in `app/control/soc_estimator.py` cross-checks the report against the energy delivered to the car and takes over when the report stalls or goes stale.

## Math

- Anchor: the reported SOC at the moment it last changed, with the accumulated session energy at that moment.
- `estimated = anchor_soc + (acc_wh - anchor_wh) / (capacity_kwh * 1000 * k) * 100`, clamped to 0-100.
- Accumulated energy sums the non-negative deltas of `ev_session_energy_wh`. `None` values add nothing, a negative step (register reset at a new session) re-baselines, and a single step above 3000 Wh is a discontinuity and is ignored. Anchors store the accumulated value, never the raw register.
- Stalled: reported fresh and `estimated - reported > 1.5` percentage points (about 1.2 kWh at 82 kWh). It clears on the next genuine SOC change.
- Stale: the reported value is older than 5 minutes. The estimate is used without the stalled flag.

## Effective SOC and stop reasons

| Situation | Source | Effective SOC |
| --- | --- | --- |
| Report fresh, not stalled | `reported` | reported |
| Stalled, or stale with an anchor | `estimated` | estimate |
| No anchor, report fresh | `reported` | reported (as before) |
| No anchor, report stale | `none` | None (as before) |

All consumers get the effective value through `get_ev_soc`: the min SOC floor, eco night, the max SOC gate and the fallback setpoint. The stop check is `max_soc_stop_reason`:

- `max_soc_reached`: fresh reported SOC at or above the limit (with the existing 0.5 point margin when the limit is 100).
- `max_soc_estimated`: the estimate is at or above the limit. The 0.5 point margin applies only while the estimate is the SOC source. While the report is healthy the estimate is a safety net with no margin, so a healthy session is not stopped early.
- When the `Charge to set max SoC` switch (`charge_to_max_soc_enabled`, default on) is off, `max_soc_stop_reason` returns None, so neither `max_soc_reached` nor `max_soc_estimated` can stop or block a session. Nothing else changes.
- There is no absolute ceiling. `ev_max_soc_pct` is authoritative, so a temporary 100 % limit for a long trip works.

## Learned k

`k` is the ratio of charger-side energy to battery energy. It starts at 1.0, so the configured capacity is the absolute starting point.

- Each new genuine SOC change refines `k` when the span from the first genuine change in the current span has at least 4 percentage points and 4 kWh: `k_span = (delta_kWh / delta_pct * 100) / capacity_kwh`, then `k += 0.1 * (k_span - k)`, clamped to 0.8-1.2. A `k_span` outside that range is ignored.
- The span restarts on a stall, stale report, energy gap, discontinuity, SOC decrease, capacity change, plug-in or unplug.
- `ev_soc_k` and `ev_soc_k_capacity_kwh` are saved in `config.yaml`. A `k` saved for another capacity is ignored (two cars).
- History-19 (82 kWh car, 40.5 to 48.5 %, 6.6 kWh): `k_span` = 1.006.

## Constants (`app/control/constants.py`)

`_SOC_STALL_TOLERANCE_PCT` 1.5, `_SOC_ESTIMATE_MARGIN_PCT` 0.5, `_SOC_MAX_ENERGY_STEP_WH` 3000, `_SOC_K_DEFAULT` 1.0, `_SOC_K_MIN` 0.8, `_SOC_K_MAX` 1.2, `_SOC_K_EMA_WEIGHT` 0.1, `_SOC_K_LEARN_MIN_SPAN_PCT` 4, `_SOC_K_LEARN_MIN_ENERGY_KWH` 4.

## Home Assistant entities

`EV SOC (estimated)`, `EV SOC Source` (`reported`, `estimated`, `none`), `EV SOC Stalled` (problem) and `EV SOC Efficiency (k)` are diagnostics. `EV SOC` publishes the effective SOC. They stay published during an EV comms outage. The charging `stopped` event carries `soc_source`.

## Risks

- After a plug-in with no SOC reported yet, the estimate anchors on the last known SOC. That is an upper bound only if the car was not charged elsewhere. If it was, the estimate is low until the integration publishes a changed value.
- An app restart mid-charge loses the anchor and the last known SOC. The first value received afterwards becomes the anchor. If the report was already frozen, the estimate starts from the frozen value.
- Cloud lag makes the change anchor late, so the estimate runs slightly low (about 0.1 % per minute at 4.4 kW). The margin and the stall tolerance absorb it.
- At 22 kW with a 6 minute cloud cadence one update gap is about 2.2 kWh, which exceeds the stall tolerance. The stalled flag can then flap. This is harmless because the estimate is accurate to about 1 point, and the tolerance can be raised.
- Known limitation: a session-energy reset missed during Standby or an EV-read outage is undetectable when the recovered counter is at or above the old one. Energy delivered before the recovery read is then undercounted (for example 2,000 Wh then 8,200 Wh counts 6,200 Wh), so the estimate runs low and the stop lands later than the limit. This needs a missed reset plus a larger recovered counter. It is accepted: a small overshoot is acceptable, and there is no lifetime-register reconciliation.
- The estimate needs the charger's session energy. Without it no estimate exists and behaviour is as before.
## Validation
Checked against `history-22` (three real sessions, 82 kWh) with simulated freeze and stale outages, warm and cold anchors, and a cross-check through `ControlLoop.run_loop` (36 ad hoc cases matched; six are kept as tests).
- At an 80 % limit the stop lands 0.83 pp short to 0.15 pp over the limit (up to +0.28 pp over when the app starts mid-session).
- Learned k is about 1.04. A freeze is flagged after about 1.3 kWh.
- Limitations: the margin applies only once the estimate is the source, cold-start k learning, the tight stall tolerance at high power, and the 0.5 pp anchor quantisation.
Tests: `tests/unit/test_soc_estimator_history22.py` with `tests/unit/soc_sim.py` and the fixture in `tests/fixtures/incidents/2026-11-01-ev-soc-history-22/`. Procedure and constants: the `ev-soc-estimator` skill.
