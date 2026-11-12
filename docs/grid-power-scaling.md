# Spec: Closed-Loop Main-Breaker Protection (Signed Import-Current Based)

Implementation: `app/control/breaker.py` (limiter), `app/control/breaker_state.py` (types),
call site in `app/control/loop.py`, eco anti-windup in `app/control/mode_strategies.py`.

## 1. Goal

Keep the **average** current on every grid phase below **80 %** of the main breaker rating
(`grid_breaker_limit_a`, default 32 A, so 25.6 A). The EV is throttled only when a phase actually imports more
than that, and never because of export. The limiter only ever reduces the setpoint and never touches charger
register 10039. Everything is judged in **amps**; volts are only used to turn amps into the watts of the setpoint.

## 2. Background and constraints

- **Setpoint is a TOTAL.** The charger setpoint is spread equally over three phases regardless of the vehicle.
  A single-phase car on L1 gets about `setpoint / 3` on L1. The setpoint is also only a hint to the car. The
  truth is the measured raw EV current per phase.
- **The Victron proxies SIGNED per-phase current.** The grid-meter current registers 2617/2619/2621
  (`victron_grid_meter_unit_id`, default 30, decoded int16 / 10) are **positive for import and negative for
  export**. This was verified live against the EM540 meter. The EM540 itself, and the Home Assistant export in
  the incident fixture, report **unsigned** magnitudes; the Victron adds the direction.
- **Why current and not power.** A breaker trips on current. At a low power factor the current is much larger
  than the real power suggests. Live sample (2026-11-13): L1 `P = +150 W`, `I = +2.0 A`, `V = 239.9 V`
  (apparent power about 480 VA, PF about 0.3); L2 `P = -50 W`, `I = -1.8 A`; L3 `P` around 0 W while `I` flips
  between +2.0 A and -2.0 A. The current stays near 2 A even at tiny real power, so real-power limits
  underestimate the load on the breaker.
- **Per-phase power (registers 820-822) is no longer a limiter input.** It is still read, because
  `grid_power_w` (their sum) drives Eco mode, but the per-phase values are not stored.
- **Balanced PV and a single-phase car.** Victron balances PV across phases, so with one phase loaded by the car,
  L1 imports while L2/L3 export and the *total* grid power can be negative. Each phase is therefore judged on its
  own signed current.
- **Only EV-active phases can be relieved.** Throttling the EV does nothing for a phase it does not load.
- **Verification tool.** `scripts/victron_probe.py` is a read-only probe that logs the exact registers the
  controller uses (raw words and decoded values) so the signed-current decoding can be checked against the meter.

## 3. Definitions

Per phase `n` (1..3), each control cycle:

| Symbol | Meaning |
| --- | --- |
| `I_grid[n]` | **signed** grid current (A), + = import, - = export (`victron_l{n}_current_a`) |
| `I_imp[n]` | per-sample import `max(0, I_grid)`; the clamp is applied **per sample**, not to the mean |
| `V[n]` | grid-meter voltage (100..300 V), else 230 V. Used only to convert amps to setpoint watts |
| `I_ev[n]` | raw charger current (A), **without** `correction_pct` (`ev_current_raw_*`) |
| EV-active | `I_ev[n] > 0.5 A` |
| `I_lim` | `0.80 x grid_breaker_limit_a` (25.6 A at 32 A) |
| mean | 30 s rolling mean (`_PHASE_IMPORT_MEAN_WINDOW_S`) |

Export never creates headroom and never offsets import on another phase or in time.

## 4. Algorithm (`limit_phase_current(loop, setpoint, now=None)`)

1. **Standby**: reset limiter state and return the setpoint untouched. No Modbus activity is added.
2. **Fail-safe on missing data** (all modes, including Manual and Eco-night). *Missing* means only a value that
   is genuinely absent: a failed read, `None`, or no fresh sample this cycle (stale). Values that merely look
   odd are never "inconsistent"; there is no plausibility check. The one exception is the gross-sanity decode in
   `app/modbus/victron.py`: `|I| > 2 x grid_breaker_limit_a` is a bad decode, becomes `None`, and counts as
   missing. If the grid current or the raw EV current is missing on any phase, count the cycle. For the first
   two consecutive cycles (the debounce window) the limiter **holds**: the output is
   `min(setpoint, hold ceiling)`. The ceiling starts at the last value returned on a valid cycle (the minimum
   charge power if there is none) and is **monotonically tightened** to every output returned while data stays
   missing, including 0. A request of 5000 W after a valid 6000 W is held at 5000 W, and a following 11000 W
   request still returns 5000 W. The ceiling resets when valid data returns. The output never ramps up on
   missing data; an active cap is still honoured. From the **3rd** consecutive cycle (about 20 s at a 10 s loop
   interval) return the minimum charge power (4400 W) with a throttled WARNING. A stop latch stays at 0 while
   data is missing. On valid data normal behaviour resumes, and an active cap keeps its gradual release.

   **Partial data**: a missing phase does not disable protection on the others. Overload protection (trim, fast
   trip, stop counter) is still evaluated on every usable phase and the result is the stricter of that and the
   hold / minimum fallback. A cap is never released on partial data.
3. **Stop latch release** (evaluated before the zero-setpoint early return, so it can release while the setpoint
   is 0): release only when on every phase a restart at the minimum fits with margin:
   `mean I_imp + (4400 / 3) / V <= I_lim - 2 A`. After release the next start is held at the minimum and then
   released gradually.
4. **Over the threshold** on a phase when `mean I_imp > I_lim`. Below it, there is no cap on any EV-active phase.
   `excess_A = mean I_imp - I_lim`.
5. **Fast trip**: the *raw* (latest-sample) `I_imp` on an EV-active phase above `0.90 x grid_breaker_limit_a`
   immediately returns the minimum setpoint (WARNING).
6. **Trim** (mean over the threshold on an EV-active phase): the binding phase is the EV-active over-threshold
   phase with the largest `excess_A`. The cap is anchored to the measured EV current and mapped to a total over
   three phases:

   `cap_w = 3 x V[n] x max(0, mean I_ev[n] - excess_A)`

   The factor 3 is there because the setpoint is a TOTAL split equally over three phases, whatever the car does.
   The cap ratchets down only (`cap_w = min(cap_w, new)`) and the output is `min(setpoint, max(cap_w, 4400 W))`.
   Using the same window for import and EV current avoids a lagging grid mean cutting after the EV has already
   dropped.
7. **Overloaded at the minimum** for 3 consecutive cycles sets the stop latch: setpoint 0 via the existing stop
   path. The counter uses the **effective output**: a cycle counts when a phase is over (trim or fast trip) and
   the output is at or below 4400 W, even if the calculated cap is higher (e.g. an EV not following the minimum
   command). It resets when no phase is over.
8. **Release**: while no phase is over and the EV is active, the cap is held until every EV-active phase has
   `mean I_imp < I_lim - 2 A`. Then it rises by `_BREAKER_CAP_RELEASE_STEP_W` (200 W) per cycle until it reaches
   the requested setpoint and is cleared. Inside the dead band (between `I_lim - 2 A` and `I_lim`) the cap is held.
9. **Overload on a phase the EV does not load**: no cap, throttled WARNING that EV throttling cannot relieve it.
10. Finally clamp to the physical 22 kW maximum. A positive result is never below 4400 W except the explicit stop (0).

### Removed

- The **plausibility check** (`|P| > 1.05 x V x I + 50 W` marked data as missing). It assumed unsigned current.
  With the signed current, the right-hand side goes negative for exporting phases, so good data was flagged
  "implausible; treating as missing" almost every cycle, which drove the hold / minimum path and could freeze
  or cut the Eco ramp.
- The **power-based limit** (`0.8 x I_brk x V` in watts) and the separate **current guard**: current is now the
  only quantity.
- Per-phase power as a limiter input.

### Thresholds

| Name | Value |
| --- | --- |
| Mean limit | 80 % of breaker, 25.6 A at 32 A (`_BREAKER_SAFETY_FRACTION`) |
| Fast trip | 90 % raw, 28.8 A at 32 A (`_BREAKER_INSTANT_THRESHOLD_FRACTION`) |
| Window | 30 s |
| Release margin | 2 A below the mean limit |
| Release step | 200 W per cycle |
| Stop after | 3 cycles overloaded at minimum |
| Fail-safe after | 3 cycles of missing data |
| Gross-sanity decode | `\|I\| <= 2 x grid_breaker_limit_a`, else missing |

## 5. Eco-day anti-windup

The Eco-day ramp adds 200 W per cycle when the home battery allows. Two guards stop it running away:

- `clamp_eco_day_to_applied`: after limiting, if the applied setpoint is below the request while ramping, the
  internal ramp setpoint is pulled down to the applied value (never below 4400 W). A limiter stop therefore
  restarts the ramp from the minimum.
- "EV is following": the ramp-up never exceeds `3 x max_n P_ev[n] + allowance` (minimum 4400 W), where
  `P_ev = V_ev x I_ev` and `allowance = max(eco_day_ramp_step_w, _ECO_DAY_FOLLOW_ALLOWANCE_W = 500 W)`, so a car
  that realises less than the setpoint cannot wind the ramp up to 22 kW. This guard (and only this one) uses the
  **calibrated** (`correction_pct`-applied) EV current, matching the written setpoint, and the 500 W floor covers
  `write_setpoint()` rounding and small under-reporting, so a following EV can always advance the ramp. Breaker
  protection keeps the raw current. The guard is skipped when the EV currents are unknown. Eco start/stop
  hysteresis (grid/battery gates, cooldown) is unchanged.

## 6. Data model and Modbus

`AppState` (not persisted): `victron_l{1,2,3}_current_a` (**signed**), `ev_current_raw_{a,b,c}`
(charger-reported, no correction). They are cleared to `None` on read failure so stale values are never used.
`grid_power_w` is still the sum of the three phase powers (registers 820-822). The limiter keeps loop-side state
in `BreakerLimiterState` (`loop._breaker`) and a per-phase `PhaseSample(t, import_a, ev_a)` buffer
(`loop._phase_samples`). Standby behaviour in AGENTS.md is untouched: the limiter performs no Modbus I/O.

## 7. Home Assistant entities

`l{1,2,3}_breaker_headroom_pct` keeps its name and unit (%). It is `(I_lim - mean I_imp) / I_lim x 100`
(capped at 100, 100 while exporting). It is `None` for a phase whose current grid current is missing, instead of
the old buffered value. In Standby, grid-only headroom stays valid (no EV data needed). Entities and the breaker
limit control are unchanged.

## 8. Logging

- INFO (throttled): trim, with binding phase, mean import A, limit A, EV current A, excess A, cap W.
- WARNING (throttled): fast trip, fail-safe, stop, overload on a non-EV phase.
- Conditions clear with `LogThrottle.clear()` when they end.

## 9. Worked example: the 2026-11-07 incident

Single-phase car on L1, Eco-day, about 233 V. The mean limit is `0.8 x 32 = 25.6 A`. Measured: L1 import about
+0.3..+1.0 kW, which is up to about **+4.1 A**, while L2/L3 export (negative signed current). The HA export holds
the EM540's unsigned currents; the replay rebuilds the Victron's signed current as `copysign(I, P)` per phase.

- New: mean L1 import peaks at a few amps, far below 25.6 A, so there is **no cap** and the ramp reaches 22 kW.
- Old (bug): the cap was anchored to the instantaneous EV current, and a single-phase limit (about 31 A x 233 V =
  7.2 to 7.45 kW) was written as the **total** setpoint. The car then received about a third of it (about 2.4 kW
  on L1) and the ramp wound up while capped.

Synthetic single-phase trim: base load about 12.9 A on L1 (3.0 kW at 233 V), car follows `setpoint / 3`. The cap
converges to `3 x 233 x (25.6 - 12.9) = 8.9 kW`, which puts L1 import at the 80 % limit without freezing the
car below it.

Low power factor: L1 `P = 700 W` but `I = +27 A` (PF about 0.1). The mean exceeds 25.6 A by 1.4 A, so with the EV
drawing `I_ev` the cap is `3 x V x (I_ev - 1.4 A)`. A power-based limit would never have noticed.

## 10. Edge cases

- Car stops drawing (all `I_ev` <= 0.5 A): no EV-active phase, no cap; overload is only warned about.
- Missing data (read failure, `None`, stale): hold at the last applied output for 2 cycles, then the minimum from
  the 3rd (about 20 s at a 10 s loop), in every mode.
- Signed currents with low or zero real power (for example `-1.8 A` at about 0 W, or L3 flipping between +2.0 A
  and -2.0 A) are valid data: no hold, no cap.
- Standby / EV reads skipped: grid samples keep being recorded for headroom diagnostics, but without EV data
  and never marked fresh for the limiter. The sample history is cleared when EV reads stop and when they
  resume, so retained (stale) EV currents never pair with fresh grid readings after leaving Standby. No EV
  Modbus polling is added.
- First cycles with no history: not treated as missing; the limit uses the samples available.
- Limiter stop while the session state machine still believes it is charging: same as before (stop is applied
  after the state machine); not redesigned here.

## 11. Tests

| Area | Tests |
| --- | --- |
| Incident replay (fixture, signed current rebuilt as `copysign(I, P)`, 10 s cycles) | `test_breaker_incident_replay.py::test_incident_replay_never_engages_cap`, `::test_incident_data_has_unsigned_currents_and_signed_power`, `::test_signed_current_reconstruction_takes_sign_from_power` |
| Loader | `test_incident_data.py` |
| Sampling, clamp per sample, headroom, 230 V fallback | `TestSamplingAndMeans` |
| Basics, Standby | `TestLimiterBasics` |
| Single-phase trim, x3 mapping, hysteresis, gradual release | `TestSinglePhaseTrimAndRelease` |
| Fast trip, stop latch, restart | `TestFastTripAndStop` |
| Export-only (negative currents), non-EV phase, three-phase car | `TestExportAndPhases` |
| Partial data keeps protecting healthy phases (fast trip + stop, trim, no release) | `TestPartialDataProtection` |
| Stop counter on effective output (EV ignores the minimum) | `TestStopCounterUsesEffectiveOutput` |
| Headroom `None` on missing current; grid-only headroom in Standby | `TestHeadroomInvalidInputs` |
| Monotonic hold ceiling (6000 W -> 5000 W, 11000 W -> 5000 W), reset on valid data, zero | `TestMonotonicHold` |
| Standby stale EV samples do not cap after exit | `TestStandbySampleInvalidation` |
| Missing data: hold for 2 cycles, minimum on the 3rd, resume; pinned Manual, Eco-day, Eco-night; inconsistent values are not missing | `TestFailSafe` |
| Current is authoritative: low power factor trims, same magnitude while exporting does not | `TestCurrentIsAuthoritative` |
| Live log regression: signed -1.8 A / -1.7 A, L3 flipping +/-2.0 A, no hold, no cap | `TestLiveSignedCurrentRegression` |
| Edges, run_loop integration | `TestEdges`, `TestRunLoopIntegration` |
| Eco anti-windup, follow-guard allowance | `test_breaker_eco_antiwindup.py` |
| Modbus plumbing (signed decode, gross-sanity, read failure clears currents) | `test_modbus_victron.py`, `test_modbus_ev.py` |

All in `tests/unit/test_breaker_protection.py` unless a file is named. Fixtures:
`tests/fixtures/incidents/2026-11-07-breaker-cap/`.

## 12. Out of scope

Writing charger register 10039, per-phase EV control (the charger takes one total), and predicting house load.
