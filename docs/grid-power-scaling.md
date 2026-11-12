# Spec: Closed-Loop Main-Breaker Protection (Import-Power Based)

Implementation: `app/control/breaker.py` (limiter), `app/control/breaker_state.py` (types),
call site in `app/control/loop.py`, eco anti-windup in `app/control/mode_strategies.py`.

## 1. Goal

Keep the **average** current on every grid phase below **80 %** of the main breaker rating
(`grid_breaker_limit_a`, default 32 A). The EV is throttled only when a phase actually imports more than
that, and never because of export. The limiter only ever reduces the setpoint and never touches charger
register 10039.

## 2. Background and constraints

- **Setpoint is a TOTAL.** The charger setpoint is spread over three phases regardless of the vehicle. A
  single-phase car on L1 gets about `setpoint / 3` on L1. The setpoint is also only a hint to the car.
  The truth is the measured EV power: the sum of `V x I` per phase from the charger registers.
- **Meter current is unsigned.** The grid meter reports a positive current for both import and export.
  Direction is only available from the signed per-phase power.
- **Per-phase power is signed and accurate** (+ import, - export), from Victron system registers
  820-822 (unit 100) into `victron_l{1,2,3}_power_w`. Voltage and current come from the grid meter block
  read 2616-2621 (`victron_grid_meter_unit_id`).
- **Balanced PV and a single-phase car.** Victron balances PV across phases, so with one phase loaded by the
  car, L1 imports while L2/L3 export and the *total* grid power can be negative. Total grid power is therefore
  useless for breaker protection; each phase must be judged on its own import.
- **Only EV-active phases can be relieved.** Throttling the EV does nothing for a phase it does not load.

## 3. Definitions

Per phase `n` (1..3), each control cycle:

| Symbol | Meaning |
| --- | --- |
| `P_grid[n]` | signed grid power (W), + = import |
| `V[n]` | grid voltage: meter reading (100..300 V), else `abs(P)/I`, else 230 V |
| `I_grid[n]` | unsigned meter current (A) |
| `I_ev[n]` | raw charger current (A), **without** `correction_pct` (`ev_current_raw_*`) |
| `P_ev[n]` | measured EV power = `V_ev[n] x I_ev[n]` |
| EV-active | `I_ev[n] > 0.5 A` |
| `P_imp` | per-sample import `max(0, P_grid)`; the clamp is applied **per sample**, not to the mean |
| `import_a` | `I_grid` when `P_grid > 0`, else 0 |
| `P_lim[n]` | `0.80 x grid_breaker_limit_a x V[n]` |
| mean | 30 s rolling mean (`_PHASE_IMPORT_MEAN_WINDOW_S`) |

Export never creates headroom and never offsets import on another phase.

## 4. Algorithm (`limit_phase_current(loop, setpoint, now=None)`)

1. **Standby**: reset limiter state and return the setpoint untouched. No Modbus activity is added.
2. **Fail-safe** (all modes, including Manual and Eco-night): if grid power, grid current or the raw EV
   current is missing, stale or implausible on any phase, count the cycle. For the first two consecutive
   cycles (the debounce window) the limiter **holds**: the output is `min(setpoint, last applied output)`,
   where the last applied output is the last value returned on a valid cycle (the minimum charge power if
   there is none, e.g. the session just started). The output can stay the same or go down but never ramps
   up on bad data; an active cap is still honoured. From the **3rd** consecutive cycle (about 20 s at a
   10 s loop interval) return the minimum charge power (4400 W) with a throttled WARNING. A stopped latch
   stays at 0 while data is missing. On valid data normal behaviour resumes, and an active cap keeps its
   gradual release (see release).
   *Plausibility*: `|P_grid| > 1.05 x V x I_grid + 50 W` marks the phase data implausible; it counts as missing.
3. **Stop latch release** (evaluated before the zero-setpoint early return, so it can release while the
   setpoint is 0): release only when on every phase a restart at the minimum fits with margin:
   `mean_import + 4400/3 <= P_lim - 2 A x V` (and the same in amps). After release the next start is held at
   the minimum and then released gradually.
4. **Over limit** on a phase when the mean exceeds the limit:
   `excess = max(mean_import - P_lim, (mean_import_a - 0.8 x I_brk) x V)`.
   The second term is the **current guard**: with power factor < 1 the current can exceed 80 % while the power
   does not. It only counts while importing.
5. **Fast trip**: the *raw* (latest-sample) import on an EV-active phase above `0.90 x I_brk x V` (or raw
   importing current above `0.90 x I_brk`) immediately returns the minimum setpoint (WARNING).
6. **Trim** (mean over limit on an EV-active phase): the cap is anchored to measured EV power, mapped to a
   total over three phases:

   `cap = 3 x max(0, mean_P_ev[n] - excess[n])` which equals `3 x (P_lim[n] - base_import[n])`

   where `base_import = mean_import - mean_P_ev` is the house load on the phase. With several over-limit
   phases the lowest cap wins (binding phase). The cap ratchets down only (`cap_w = min(cap_w, new)`), the
   output is `min(setpoint, max(cap, 4400 W))`. Using the same window for import and EV power avoids a lagging
   grid mean cutting after the EV has already dropped.
7. **Overloaded at the minimum** for 3 consecutive cycles (fast trip, or a cap at or below 4400 W) sets the stop
   latch: setpoint 0 via the existing stop path.
8. **Release**: while no phase is over limit and the EV is active, the cap is held until every EV-active phase
   has `mean_import < P_lim - 2 A x V`. Then it rises by `_BREAKER_CAP_RELEASE_STEP_W` (200 W) per cycle until it
   reaches the requested setpoint and is cleared. Inside the dead band (between `P_lim - 2 A x V` and `P_lim`) the cap is held.
9. **Overload on a phase the EV does not load**: no cap, throttled WARNING that EV throttling cannot relieve it.
10. Finally clamp to the physical 22 kW maximum. A positive result is never below 4400 W except the explicit stop (0).

### Thresholds

| Name | Value |
| --- | --- |
| Mean limit | 80 % of breaker (`_BREAKER_SAFETY_FRACTION`) |
| Fast trip | 90 % raw (`_BREAKER_INSTANT_THRESHOLD_FRACTION`) |
| Window | 30 s |
| Release margin | 2 A x V below the limit |
| Release step | 200 W per cycle |
| Stop after | 3 cycles overloaded at minimum |
| Fail-safe after | 3 cycles of missing data |
| Plausibility | `|P| <= 1.05 x V x I + 50 W` |

## 5. Eco-day anti-windup

The Eco-day ramp adds 200 W per cycle when the home battery allows. Two guards stop it running away:

- `clamp_eco_day_to_applied`: after limiting, if the applied setpoint is below the request while ramping, the
  internal ramp setpoint is pulled down to the applied value (never below 4400 W). A limiter stop therefore
  restarts the ramp from the minimum.
- "EV is following": the ramp-up never exceeds `3 x max_n P_ev[n] + eco_day_ramp_step_w` (minimum 4400 W), so a
  car that realises less than the setpoint cannot wind the ramp up to 22 kW. The guard is skipped when the raw
  EV currents are unknown. Eco start/stop hysteresis (grid/battery gates, cooldown) is unchanged.

## 6. Data model and Modbus

`AppState` (not persisted, not in the snapshot): `victron_l{1,2,3}_power_w` (signed), `ev_current_raw_{a,b,c}`
(charger-reported, no correction). All are cleared to `None` on read failure so stale values are never used.
`grid_power_w` is still the sum of the three phases. The limiter keeps loop-side state in
`BreakerLimiterState` (`loop._breaker`) and a per-phase `PhaseSample` buffer (`loop._phase_samples`).
Standby behaviour in AGENTS.md is untouched: the limiter performs no Modbus I/O.

## 7. Home Assistant entities

`l{1,2,3}_breaker_headroom_pct` is now `(P_lim - mean_import) / P_lim x 100` (capped at 100, 100 while
exporting). Entities and the breaker limit control are unchanged.

## 8. Logging

- INFO (throttled): normal trim, with binding phase, mean import W, limit W, EV phase power W, excess W, cap W.
- WARNING (throttled): fast trip, fail-safe, stop, implausible data, overload on a non-EV phase.
- Conditions clear with `LogThrottle.clear()` when they end.

## 9. Worked example: the 2026-11-07 incident

Single-phase car on L1, Eco-day, 233 V. `P_lim = 0.8 x 32 x 233 = 5.96 kW` per phase (about 6.1 kW at 238 V).
Measured: L1 import +0.3..+1.0 kW, L2/L3 export -0.8..-1.3 kW, all meter currents positive (3..5 A).

- New: mean L1 import peaks at about 1.0 kW, far below 5.96 kW, so there is **no cap** and the ramp reaches 22 kW.
- Old (bug): the meter current was treated as signed, the cap anchored to the instantaneous EV current, and a
  single-phase limit (about 31 A x 233 V = 7.2 to 7.45 kW) was written as the **total** setpoint. The car then
  received about a third of it (about 2.4 kW on L1) and the ramp wound up while capped.

Replaying the recorded data through both limiters: the old one cut 7200 W to 7043 W at 06:34:30 UTC and held about
7.3 kW from then on; the new one never engages.

Synthetic single-phase trim: base load 3.0 kW on L1, car follows `setpoint / 3`. The cap converges to
`3 x (5964.8 - 3000) = 8894 W`, which puts L1 import at the 80 % limit without freezing the car below it.

## 10. Edge cases

- Car stops drawing (all `I_ev` <= 0.5 A): no EV-active phase, no cap; overload is only warned about.
- Phase data implausible or missing: hold at the last applied output for 2 cycles, then the minimum from
  the 3rd (about 20 s at a 10 s loop), in every mode (behaviour change: Manual and Eco-night used to pass
  the setpoint through unlimited).
- First cycles with no history: not treated as missing; the limit uses the samples available.
- Limiter stop while the session state machine still believes it is charging: same as before (stop is applied
  after the state machine); not redesigned here.

## 11. Tests

| Area | Tests |
| --- | --- |
| Incident replay (fixture, 10 s cycles) | `test_breaker_incident_replay.py::test_incident_replay_never_engages_cap`, `::test_incident_data_has_unsigned_currents_and_signed_power` |
| Loader | `test_incident_data.py` |
| Sampling, clamp per sample, headroom | `TestSamplingAndMeans` |
| Basics, Standby | `TestLimiterBasics` |
| Single-phase trim, x3 mapping, hysteresis, gradual release | `TestSinglePhaseTrimAndRelease` |
| Fast trip, stop latch, restart | `TestFastTripAndStop` |
| Export-only, non-EV phase, three-phase car | `TestExportAndPhases` |
| Missing / implausible data: hold for 2 cycles (higher request held, lower passes, no prior value gives minimum), minimum on the 3rd, resume on valid data; pinned Manual, Eco-day and Eco-night | `TestFailSafe` |
| Current guard (PF < 1) | `TestCurrentGuard` |
| Edges, run_loop integration | `TestEdges`, `TestRunLoopIntegration` |
| Eco anti-windup | `test_breaker_eco_antiwindup.py` |
| Modbus plumbing | `test_modbus_victron.py`, `test_modbus_ev.py` |

All in `tests/unit/test_breaker_protection.py` unless a file is named. Fixtures:
`tests/fixtures/incidents/2026-11-07-breaker-cap/`.

## 12. Out of scope

Writing charger register 10039, per-phase EV control (the charger takes one total), and predicting house load.
