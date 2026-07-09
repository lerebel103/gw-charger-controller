# Spec: Dynamic Phase-Current Scaling (Breaker Protection)

Status: Ready
Owner: TBD
Related modules: `app/modbus/victron.py`, `app/control/power_utils.py`, `app/control/loop.py`,
`app/state/models.py`, `app/ha/entities.py`, `app/ha/constants.py`

---

## 1. Goal

Prevent the EV charger from overloading the property's main incoming breaker by dynamically
scaling back the charge setpoint whenever the **measured current on any single grid phase**
approaches a configurable limit.

Because the GW22K charger distributes its power setpoint **equally across all active phases**
and cannot shift load away from an overloaded phase, the total charge power must be reduced so
that the *worst-case* phase stays within budget — a single heavily-loaded phase governs the
whole setpoint.

## 2. Background & Constraints

- The Victron grid meter (`com.victronenergy.grid`, unit id `victron_grid_meter_unit_id`,
  default 30) directly measures per-phase RMS current — no need to derive `I = P/V`.
  Verified register mapping:
  | Register | Path | Type | Scale | Unit |
  |---|---|---|---|---|
  | 2616 | `/Ac/L1/Voltage` | uint16 | ÷10 | V |
  | 2617 | `/Ac/L1/Current` | int16 (signed) | ÷10 | A |
  | 2618 | `/Ac/L2/Voltage` | uint16 | ÷10 | V |
  | 2619 | `/Ac/L2/Current` | int16 (signed) | ÷10 | A |
  | 2620 | `/Ac/L3/Voltage` | uint16 | ÷10 | V |
  | 2621 | `/Ac/L3/Current` | int16 (signed) | ÷10 | A |
  - Current sign: **positive = import, negative = export** (same convention as grid power).
  - These sit contiguously, so a single block read of **2616–2621 (count = 6)** replaces
    today's three separate voltage reads and additionally yields the currents.
- The EV charger reports its own measured per-phase current in `AppState`
  (`ev_current_a`, `ev_current_b`, `ev_current_c`), corrected by `correction_pct`. These
  measurements are used to: (1) detect which phases are actively drawing (FR-8), and
  (2) compute accurate per-phase baseline current (`I_base_N = I_grid_N − I_ev_N`). The
  charger distributes its setpoint equally across active phases, so these values will be
  approximately equal on all active phases under normal operation.
- The grid meter is at the main-breaker point, so grid per-phase current *is* the current the
  breaker experiences. The measurement inherently accounts for all internal generation
  (solar, battery discharge) — these reduce the measured import current. No separate modelling
  of solar/battery contribution is needed.
- Solar/battery systems internal to the household are variable: cloud cover or battery floor
  can cause sudden loss of internal generation, spiking grid import by 10–15 A within seconds.
  The algorithm must handle these transients (see FR-12, instantaneous cap override).

## 3. Definitions

| Symbol | Meaning |
|---|---|
| `I_brk` | Configured per-phase main breaker rating (A). |
| `SAFETY` | Fixed safety fraction of `I_brk` = **0.80** (industry-standard breaker derating; not configurable). |
| `I_grid_N` | Measured grid current on phase N (A), signed, **30 s rolling mean**. |
| `I_grid_N_raw` | Latest instantaneous measured grid current on phase N (A), signed, unaveraged. |
| `I_ev_N` | EV charger current on phase N (A). |
| `I_base_N` | Non-EV household baseline current on phase N = `I_grid_N − I_ev_N`. |
| `V_N` | Measured grid-meter per-phase voltage (`victron_l{1,2,3}_voltage_v`). |
| `n_ph` | Number of phases the charger currently draws on (detected from measured EV current). |
| `P_cap` | Maximum EV power that keeps every phase ≤ `SAFETY · I_brk`. |

## 4. Functional Requirements

- **FR-1** The controller SHALL read measured per-phase grid current from the Victron grid meter
  every control cycle (except in Standby, per AGENTS.md), storing it in `AppState`.
- **FR-2** The controller SHALL compute, each cycle, a per-phase current headroom (using the
  30 s rolling mean of measured grid current) and derive a maximum allowable EV power `P_cap`
  that keeps every phase at or below `SAFETY · I_brk`.
- **FR-3** The controller SHALL clamp the computed setpoint to `P_cap` **after** mode-specific
  setpoint computation, as a mode-independent safety limit (applies in Eco and Manual).
- **FR-4** When `P_cap` falls below the charger practical minimum (`_MIN_CHARGE_W`, 4400 W),
  the controller SHALL command a stop (setpoint 0) rather than charge above the limit.
- **FR-5** The per-phase breaker rating `I_brk` SHALL be configurable (persisted, editable from
  Home Assistant), defaulting to 32 A.
- **FR-6** The safety fraction SHALL be a **fixed constant of 0.80** (industry-standard breaker
  derating), defined in `app/control/constants.py`; it is NOT user-configurable. *(Resolved: OQ-2)*
- **FR-7** The feature SHALL be an **always-on, built-in safety mechanism**. It has no on/off
  toggle and is not exposed to Home Assistant as an enable/disable control. It is inactive only
  when required data is unavailable (EC-1) or in Standby (EC-5). *(Resolved: OQ-1)*
- **FR-8** The active number of phases (`n_ph`) and which phases are active SHALL be determined
  from the **measured EV per-phase current** (`ev_current_a/b/c`): a phase is considered active
  if its current exceeds `_PHASE_ACTIVE_THRESHOLD_A` (0.5 A). This is more robust than relying
  on the `SinglePhaseSwitching` config register, which only indicates the feature is *enabled*
  on the charger, not whether single-phase operation is currently in effect. *(Resolved: OQ-7)*
- **FR-9** The controller SHOULD expose the measured per-phase grid current as Home Assistant
  sensors for observability (complementary to FR-11).
- **FR-10** Per-phase grid current SHALL be smoothed with a **fixed 30 s rolling mean** before
  use in the cap computation, independent of the Eco `eco_mean_window_minutes` window.
  *(Resolved: OQ-4)*
- **FR-11** The controller SHALL expose, per phase, a **breaker-headroom diagnostic sensor** (%)
  to Home Assistant, computed from the same 30 s rolling-mean current used for scaling:
  `headroom_pct_N = (SAFETY·I_brk − I_grid_N) / (SAFETY·I_brk) · 100`.
  100 % = phase idle; 0 % = phase at the 80 % safety limit (e.g. 25.6 A on a 32 A breaker);
  negative = over budget (scaling active / external overload). Displayed value clamped to a
  maximum of 100 %; may be negative. *(Resolved: OQ-6)*
- **FR-12** The controller SHALL implement an **instantaneous cap override**: if any single raw
  (unaveraged) per-phase grid current reading exceeds `_BREAKER_INSTANT_THRESHOLD_FRACTION`
  (0.90) × `I_brk`, the cap computation for that cycle SHALL use the raw instantaneous reading
  instead of the 30 s rolling mean. This provides fast reaction to sudden load transients
  (e.g., solar dropout, battery inverter curtailment) while the rolling mean catches up.
  *(Resolved: OQ-8)*
- **FR-13** The controller SHALL implement **restart hysteresis** at the `_MIN_CHARGE_W`
  boundary: once the cap forces setpoint to 0 (FR-4), it SHALL NOT permit charging to resume
  until `P_cap` exceeds `_MIN_CHARGE_W + _BREAKER_CAP_RESTART_MARGIN_W` (default 500 W = 4900 W
  effective restart threshold). This prevents start/stop oscillation when headroom hovers near
  the charger hardware minimum. The hysteresis is a power threshold only — no timer is required.
  *(Resolved: OQ-9)*
- **FR-14** The controller SHALL **clear per-phase current fields to `None`** when a Victron
  read fails, ensuring stale data is never used for safety computations. The rolling-mean buffer
  naturally ages out stale samples (30 s window), but the instantaneous override (FR-12) must
  not use a stale `AppState` value. *(Resolved: OQ-10)*
- **FR-15** The controller SHALL **validate decoded per-phase current** for plausibility: if
  any decoded absolute current exceeds `_BREAKER_PLAUSIBILITY_LIMIT_A` (2 × `I_brk`), the
  reading SHALL be treated as a communication error and the field set to `None`. This guards
  against corrupt register data being used in safety calculations.
- **FR-16** The controller SHALL emit a **throttled log message** (at most once per 60 s) when
  the breaker cap actively reduces the setpoint, including: original setpoint, capped setpoint,
  binding phase, and measured current on that phase.

## 5. Non-Functional Requirements

- **NFR-1** No additional Modbus round-trips versus today: the widened 2616–2621 block read
  replaces the three existing single voltage reads.
- **NFR-2** The cap computation SHALL be pure/synchronous and safe to call with partial data.
- **NFR-3** In Standby mode, no EV or grid reads/behaviour change (respect AGENTS.md standby rules).
- **NFR-4** Deterministic and unit-testable without live hardware.

## 6. Algorithm

**Critical design constraint:** The GW22K charger distributes its power setpoint **equally**
across all active phases. It cannot shift load away from an overloaded phase — if one phase
has less headroom than others, the only option is to reduce the total setpoint (which reduces
all phases equally). The worst-case (most-loaded) phase therefore governs the entire cap.

Per-phase grid current is first smoothed with a **fixed 30 s rolling mean** (FR-10) to reject
spiky household loads. For each phase `N ∈ {1,2,3}`:

```
I_grid_N   = mean_30s(grid_current_reg_N / 10)   # signed A, rolling mean
I_ev_N     = ev_current_{a,b,c}                  # A (measured actual EV per-phase draw)
I_base_N   = I_grid_N - I_ev_N                   # non-EV baseline (household ± solar/battery)
I_ev_max_N = SAFETY * I_brk - I_base_N           # max EV current this phase can carry
```

### 6.1 Instantaneous Override (FR-12)

Before computing the cap, check whether any raw (latest single reading) per-phase grid current
exceeds the instantaneous threshold:

```
for each phase N:
    if I_grid_N_raw > _BREAKER_INSTANT_THRESHOLD_FRACTION * I_brk:
        use I_grid_N_raw in place of mean_30s for phase N in this cycle
```

This ensures fast reaction to solar/battery transients (seconds rather than 30 s). The 0.90
threshold is higher than the 0.80 steady-state limit, so the instantaneous path only activates
during genuine spikes — it doesn't cause oscillation under normal conditions.

### 6.2 Active Phase Detection (FR-8)

Determine which phases the charger is actively drawing on from measured EV current:

```
active_phases = [N for N in {1,2,3} if I_ev_N > _PHASE_ACTIVE_THRESHOLD_A]
n_ph = len(active_phases)  # typically 3, or 1 when single-phase active
if n_ph == 0: skip cap (charger not drawing — see §6.3 fallback)
```

### 6.3 Binding Phase and Cap Computation

Because the charger distributes power equally across active phases, reducing the setpoint
reduces current equally on all active phases. The binding phase (with the least headroom)
governs how much current can be added per phase:

```
headroom_A = max(0, min(I_ev_max_N for N in active_phases))
P_cap      = headroom_A * sum(V_N for N in active_phases)
setpoint   = min(setpoint, P_cap)
```

**When EV is idle (`n_ph == 0`) but mode requests charging (startup):** use the same formula
across all three phases (the charger will start on all three phases by default):

```
headroom_A = max(0, min(I_ev_max_N for N in {1,2,3}))
P_cap = headroom_A * sum(V_N for N in {1,2,3})
```

**Worked example (unbalanced household, balanced charger):**
```
I_brk=32A, SAFETY=0.80, limit=25.6A/phase, V=230V all phases

Grid meter (mean): L1=22A, L2=12A, L3=10A
EV charger draws:  L1=8A,  L2=8A,  L3=8A   (balanced across 3 phases)

I_base: L1=22-8=14A, L2=12-8=4A, L3=10-8=2A
I_ev_max: L1=25.6-14=11.6A, L2=25.6-4=21.6A, L3=25.6-2=23.6A

headroom_A = min(11.6, 21.6, 23.6) = 11.6A  (L1 is binding — household oven)
P_cap = 11.6 * (230+230+230) = 8004W

Mode wants 9000W → capped to 8004W.

Verification: at 8004W across 3 phases, each phase gets 8004/3 = 2668W = 11.6A
  L1 grid = 14 + 11.6 = 25.6A ✓ (exactly at limit)
  L2 grid =  4 + 11.6 = 15.6A ✓ (under limit)
  L3 grid =  2 + 11.6 = 13.6A ✓ (under limit)
```

**Single-phase example:**
```
Same household, charger on L1 only (single-phase switching active):
active_phases = [L1], I_ev_L1 = 24A

I_base_L1 = grid_L1 - 24 (let's say grid_L1=30A) → I_base_L1 = 6A
I_ev_max_L1 = 25.6 - 6 = 19.6A
headroom_A = 19.6A
P_cap = 19.6 * 230 = 4508W (just above _MIN_CHARGE_W)
```

### 6.4 Minimum Threshold and Restart Hysteresis (FR-4, FR-13)

```
if setpoint < _MIN_CHARGE_W:
    setpoint = 0
    _breaker_cap_tripped = True

# On subsequent cycles, require extra margin before restarting:
if _breaker_cap_tripped:
    if P_cap < _MIN_CHARGE_W + _BREAKER_CAP_RESTART_MARGIN_W:
        setpoint = 0   # hold at zero until headroom is confirmed
    else:
        _breaker_cap_tripped = False  # release, allow mode setpoint through
```

### 6.5 Notes

- `SAFETY = 0.80` is a fixed module constant, not user-configurable (FR-6).
- `V_N` are the grid-meter per-phase voltages already read for voltage-drop diagnostics
  (`victron_l{1,2,3}_voltage_v`) — no extra reads.
- Because the charger distributes power equally, `headroom_A × Σ V_N` correctly computes the
  maximum total power that keeps the binding phase within budget.
- For single-phase operation (`n_ph = 1`), the formula simplifies to:
  `P_cap = headroom_A × V_active` (only one phase).
- The cap only bites once a phase nears `0.80 · I_brk`, so below that it is a no-op
  (satisfies "scale back when draw reaches 80%").
- Feed-forward (single-step) rather than a slow ramp; converges immediately.
- Above `_MIN_CHARGE_W`, the cap is a continuous `min()` clamp: it naturally scales the
  setpoint up or down each cycle as household load and solar/battery change. No explicit
  ramp-up logic is needed — recovery is automatic as headroom improves.
- The restart hysteresis (FR-13) only applies at the stop/start boundary (`P_cap` crossing
  `_MIN_CHARGE_W`). It prevents cycling when the system cannot sustain even minimum charge,
  without interfering with normal continuous scaling above the minimum.
- The measured EV per-phase current (`ev_current_a/b/c`) serves two purposes:
  (1) active phase detection, and (2) accurate `I_base_N` isolation. It is NOT used to model
  an unbalanced charger distribution — the charger is assumed to be balanced across active
  phases.

## 7. Data Model Changes (`app/state/models.py`)

Transient readings (NOT persisted):
- `victron_l1_current_a: float | None = None`
- `victron_l2_current_a: float | None = None`
- `victron_l3_current_a: float | None = None`

Computed diagnostics (NOT persisted; alongside the existing `l{1,2,3}_voltage_drop_pct`),
published each cycle for FR-11:
- `l1_breaker_headroom_pct: float | None = None`
- `l2_breaker_headroom_pct: float | None = None`
- `l3_breaker_headroom_pct: float | None = None`

Configuration (add to dataclass AND `PERSISTED_FIELDS`):
- `grid_breaker_limit_a: float = 32.0`

The feature is always on (FR-7) — there is **no** enable flag in config. The 0.80 safety
fraction and the 30 s mean window are **constants**, not config:
- `_BREAKER_SAFETY_FRACTION = 0.80` in `app/control/constants.py` (FR-6).
- `_PHASE_CURRENT_MEAN_WINDOW_S = 30.0` in `app/control/constants.py` (FR-10).
- `_BREAKER_INSTANT_THRESHOLD_FRACTION = 0.90` in `app/control/constants.py` (FR-12).
- `_BREAKER_CAP_RESTART_MARGIN_W = 500.0` in `app/control/constants.py` (FR-13).
- `_PHASE_ACTIVE_THRESHOLD_A = 0.5` in `app/control/constants.py` (FR-8).
- `_BREAKER_PLAUSIBILITY_LIMIT_A` = computed as `2 * grid_breaker_limit_a` at runtime (FR-15).

## 8. Modbus Changes (`app/modbus/victron.py`)

- Replace the three single-register voltage reads (2616 / 2618 / 2620) with **one** block read
  of address 2616, count 6, device id = `victron_grid_meter_unit_id`.
- Decode: even offsets = voltages (÷10, uint16), odd offsets = currents (÷10, **signed int16**,
  reuse `_uint16_to_int16`).
- Populate `victron_l{1,2,3}_voltage_v` and `victron_l{1,2,3}_current_a`.
- **Plausibility check (FR-15):** after decoding each phase current, if
  `abs(value) > 2 * state.grid_breaker_limit_a`, set the field to `None` instead.
- **Staleness (FR-14):** on any read failure (`ModbusException` / `OSError`), set all three
  `victron_l{1,2,3}_current_a` fields to `None` before raising/closing.

## 9. Control Integration

- Update `SamplingLoopProtocol` in `app/control/protocols.py` to include the new per-phase
  current sample buffers and the hysteresis flag (required for type-safe access from helpers).
- New per-phase grid-current rolling-sample buffers on `ControlLoop`
  (`_l1_current_samples`, `_l2_current_samples`, `_l3_current_samples`), populated in
  `record_samples` and pruned to a fixed 30 s window (`_PHASE_CURRENT_MEAN_WINDOW_S`),
  independent of the Eco `eco_mean_window_minutes` buffers.
- New `_breaker_cap_tripped: bool = False` flag on `ControlLoop` for restart hysteresis (FR-13).
- New rolling-mean helpers in `app/control/power_utils.py` (mirroring `mean_grid_power`):
  `mean_phase_current(loop, phase) -> float | None`.
- New pure helper in `app/control/power_utils.py`, mirroring `limit_battery_discharge`:
  `limit_phase_current(loop, setpoint) -> float`.
  - Returns `setpoint` unchanged when required data (per-phase mean current/voltage) is
    missing/`None` (EC-1). No enable check — the feature is always on (FR-7).
  - Implements instantaneous override (FR-12): checks raw `AppState` current fields against
    `_BREAKER_INSTANT_THRESHOLD_FRACTION * grid_breaker_limit_a`; if exceeded, substitutes
    the raw value for the mean on that phase for this cycle.
  - Detects active phases from measured EV current (FR-8).
  - Manages `_breaker_cap_tripped` hysteresis flag (FR-13).
  - Emits throttled log when cap reduces setpoint (FR-16).
- Call site in `app/control/loop.py::run_loop`: apply as the **final safety clamp, immediately
  after `state_machine.apply_charging_events(setpoint)`** and before `_apply_ev_output`
  (Resolved: OQ-3). Because it only ever reduces the setpoint, a cap-induced 0 flows naturally
  into the existing stop path in `_apply_ev_output` (writes `_STOP_PRESET_W`, may issue stop).
- Per-phase breaker headroom (FR-11) is computed in `build_snapshot` from `mean_phase_current`
  and `grid_breaker_limit_a`, and stored on the snapshot for publishing.

## 10. Home Assistant Entities

Config (edit): add one `_number(...)` entry in `app/ha/entities.py::ENTITIES` and matching
`COMMAND_MAP` + `NUMBER_RANGES` entries in `app/ha/constants.py`:
- `grid_breaker_limit` → `grid_breaker_limit_a`, range (10, 100) A, step 1.
- No enable toggle and no safety-% entity (feature always on; 0.80 fixed).

Sensors (observability): add `_sensor(...)` entries + `StateSnapshot` fields + `build_snapshot`
wiring + publish mapping:
- **FR-11 (primary):** `breaker_headroom_l1/l2/l3` (%, no device_class, `measurement`,
  `entity_category="diagnostic"`) — per-phase headroom to the 80 % safety limit. Modelled on the
  existing `l{1,2,3}_voltage_drop_perc` diagnostic sensors.
- **FR-9 (complementary):** `grid_current_l1/l2/l3` (A, `current`, `measurement`).

## 11. Edge Cases & Failure Handling

- **EC-1** Any of `I_grid_N`, `I_ev_N`, or `V_N` is `None` → skip the cap (no clamp on unknown
  data); the corresponding headroom sensor (FR-11) publishes unavailable.
- **EC-2** Victron comms down → per-phase current fields cleared to `None` (FR-14) → cap is
  skipped (EC-1); existing Eco "Victron down" handling already pauses Eco charging. The
  rolling-mean buffer ages out within 30 s.
- **EC-3** Negative headroom (a phase already over budget) → `P_cap` clamps to 0 → stop (FR-4).
- **EC-4** Household baseline noise/oscillation → mitigated by the fixed 30 s rolling mean (FR-10).
  During the first ~30 s after startup the buffer is partial; the mean of available samples is
  used (even a single sample enables the cap). The cap is only skipped when zero samples exist
  for an active phase (EC-1).
- **EC-5** Standby mode → feature inactive along with all other EV/grid activity (NFR-3).
- **EC-6** Single-phase switching mid-session → active phases re-detected each cycle from
  measured EV current (FR-8). Cap adapts within one cycle.
- **EC-7** Solar/battery transient (cloud cover, battery hits floor SOC) → grid current spikes
  within seconds. The 30 s mean lags, but the instantaneous override (FR-12) catches any raw
  reading > 0.90 · I_brk and applies an immediate cap using the raw value. The 0.80 safety
  margin provides additional buffer for transients below the 0.90 instantaneous threshold.
- **EC-8** Headroom oscillation near `_MIN_CHARGE_W` → restart hysteresis (FR-13) prevents
  start/stop cycling. Once stopped, requires `P_cap > _MIN_CHARGE_W + 500 W` to restart.
  Above `_MIN_CHARGE_W`, the cap is continuous and self-correcting (no oscillation risk).
- **EC-9** Corrupt/implausible register data → plausibility check (FR-15) rejects values
  exceeding 2 × `I_brk` and sets field to `None`. Cap disengages via EC-1.
- **EC-10** EV charger not drawing current (`n_ph = 0`, all phase currents below threshold) →
  cap computation uses the balanced-assumption startup fallback (§6.3). If mode requests
  charging, the startup cap uses `min(I_ev_max_N) × Σ V_N` across all three phases. Once
  charging begins and EV phase currents are measurable, active phase detection refines which
  phases are considered.
- **EC-11** `correction_pct` interaction: EV currents in `AppState` are used as-is. The
  correction factor (currently 5.6%) means the formula slightly overestimates EV contribution
  to grid current, making `I_base_N` slightly conservative (lower). This results in a slightly
  conservative cap — acceptable for a safety feature (errs on the side of caution).
- **EC-12** Unbalanced household load: the binding-phase formula (`min(I_ev_max_N)`) correctly
  identifies the most-at-risk phase. Because the charger distributes power equally across
  active phases, reducing the total setpoint reduces current on all phases including the
  binding one. The phase with the heaviest non-EV baseline always governs the cap.

## 12. Testing Requirements

Tests are structured in three tiers to validate correctness from pure math through to
real-world temporal behaviour.

### Tier 1: Unit Tests (pure function logic, deterministic)

Test `limit_phase_current()`, `mean_phase_current()`, and Victron decode in isolation.

**Core cap computation:**

| ID | Scenario | I_grid (mean) | I_ev | I_brk | Expected |
|---|---|---|---|---|---|
| T-U1 | No cap — all phases well under limit | L1=10,L2=8,L3=6 | 5,5,5 | 32 | setpoint unchanged |
| T-U2 | L1 at exact safety limit (25.6A) | L1=25.6,L2=10,L3=10 | 8,8,8 | 32 | headroom_L1=0 → P_cap=0 → stop |
| T-U3 | L1 over limit (negative headroom) | L1=26,L2=10,L3=10 | 8,8,8 | 32 | P_cap clamps to 0 → stop |
| T-U4 | Partial reduction (L1 binding) | L1=20,L2=10,L3=10 | 8,8,8 | 32 | headroom_L1=13.6A → P_cap=9384W |
| T-U5 | Heavy household on one phase | L1=24,L2=5,L3=5 | 6,6,6 | 32 | headroom_L1=7.6A → P_cap=5244W |
| T-U6 | Solar exporting (all phases negative) | L1=-5,L2=-3,L3=-2 | 8,8,8 | 32 | All I_base negative → huge headroom → no cap |
| T-U7 | All phases equally loaded | L1=20,L2=20,L3=20 | 8,8,8 | 32 | headroom=13.6A → P_cap=9384W |

**Single-phase operation (FR-8):**

| ID | Scenario | Expected |
|---|---|---|
| T-U8 | Charger on L1 only (L2,L3 < 0.5A) | Only L1 headroom matters; P_cap = headroom_L1 × V_L1 |
| T-U9 | Charger on L2 only | Only L2 headroom matters; L1/L3 ignored |
| T-U10 | Switch from 3-phase to 1-phase mid-cycle | Active phases update; cap recalculates |

**Boundary conditions (FR-4, FR-13 hysteresis):**

| ID | Scenario | Expected |
|---|---|---|
| T-U11 | P_cap = 4500W (just above min), mode wants 7000W | setpoint = 4500W (capped, not stopped) |
| T-U12 | P_cap = 4300W (below min) | setpoint = 0, `_breaker_cap_tripped = True` |
| T-U13 | After trip: P_cap recovers to 4700W (< 4900W threshold) | setpoint stays 0 (hysteresis holds) |
| T-U14 | After trip: P_cap recovers to 5000W (> 4900W threshold) | trip released, mode setpoint flows |
| T-U15 | P_cap = exactly 4400W | setpoint = 4400W (at minimum, not tripped) |
| T-U16 | P_cap = 4399W | setpoint = 0 (below min, tripped) |

**Instantaneous override (FR-12):**

| ID | Scenario | Expected |
|---|---|---|
| T-U17 | Mean=20A, raw=28A (< 0.90×32=28.8A) | Uses mean → no override |
| T-U18 | Mean=20A, raw=29A (> 28.8A) | Uses raw for that phase → tighter cap |
| T-U19 | Mean=15A, raw=30A on L1; L2/L3 normal | Only L1 overridden; L2/L3 use mean |
| T-U20 | All phases raw > 28.8A simultaneously | All use raw → worst-case cap |

**Data-missing / skip conditions (EC-1):**

| ID | Scenario | Expected |
|---|---|---|
| T-U21 | One phase grid current = None | Skip cap (return setpoint unchanged) |
| T-U22 | One phase EV current = None | Skip cap |
| T-U23 | One phase voltage = None | Skip cap |
| T-U24 | All data present but `n_ph = 0` (EV idle) | Use startup fallback (all 3 phases) |
| T-U25 | Rolling mean buffer empty (no samples) | Skip cap |
| T-U26 | Rolling mean buffer has 1 sample (partial window) | Use 1 sample — cap active |

**Plausibility (FR-15):**

| ID | Scenario | Expected |
|---|---|---|
| T-U27 | Decoded current = 60A (< 2×32=64A) | Accepted, stored in state |
| T-U28 | Decoded current = 65A (> 2×32=64A) | Rejected, field set to None |
| T-U29 | Decoded current = -50A (export, abs < 64A) | Accepted |
| T-U30 | Decoded current = -70A (abs > 64A) | Rejected, field set to None |

**Victron block read decode (T-1 series):**

| ID | Scenario | Expected |
|---|---|---|
| T-U31 | Normal block read (6 registers) | Even offsets → voltages ÷10; odd → signed currents ÷10 |
| T-U32 | Negative current (export) | Correctly decoded as negative via `_uint16_to_int16` |
| T-U33 | Read failure (ModbusException) | All 3 current fields set to None (FR-14) |
| T-U34 | Different `grid_breaker_limit_a` values | Plausibility threshold scales with config |

**Configuration (FR-5):**

| ID | Scenario | Expected |
|---|---|---|
| T-U35 | `grid_breaker_limit_a` persisted in `PERSISTED_FIELDS` | Survives restart |
| T-U36 | HA command sets value within range (10–100) | Applied to state, persisted |
| T-U37 | HA command below range (e.g. 5A) | Rejected by NUMBER_RANGES validation |
| T-U38 | HA command above range (e.g. 150A) | Rejected by NUMBER_RANGES validation |
| T-U39 | Changed via MQTT → cap uses new value next cycle | Live config update |

### Tier 2: Integration Tests (loop-level, mocked Modbus)

Verify cap integrates correctly with control loop, state machine, and mode strategies.

| ID | Scenario | Validates |
|---|---|---|
| T-I1 | Eco day mode produces 7000W, cap limits to 5500W | Cap applied after mode computation; charger receives 5500W |
| T-I2 | Manual mode at 11000W, cap limits to 8000W | Cap works in Manual mode |
| T-I3 | Standby mode — cap produces no effect | NFR-3: no EV activity in standby |
| T-I4 | Cap forces setpoint to 0 → `_apply_ev_output` sends stop | Stop path end-to-end |
| T-I5 | Cap holds 0 (hysteresis active), mode wants to charge | Charger stays stopped |
| T-I6 | Cap releases (P_cap > 4900W after trip) → charger starts | Clean restart |
| T-I7 | Victron comms fail mid-session → cap disengages | FR-14, EC-2: fields cleared, graceful degradation |
| T-I8 | Victron comms restore → buffer fills → cap re-engages | Recovery path |
| T-I9 | State machine in STOPPING state, cap reduces further | Safety overrides hold-setpoint |
| T-I10 | `grid_breaker_limit_a` changed via MQTT mid-session | Cap uses new value immediately |

### Tier 3: Scenario-Based Tests (multi-cycle temporal sequences)

Simulate real-world sequences over multiple control loop iterations.

| ID | Scenario | Duration | Validates |
|---|---|---|---|
| T-S1 | **Solar dropout**: charging at 8kW, solar drops → grid spikes L1 from 15A to 29A | 3 cycles | Instantaneous override fires within 1 cycle; setpoint drops immediately |
| T-S2 | **Battery floor hit**: battery stops discharging → grid jumps 10A all phases | 5 cycles | Cap reacts; setpoint reduces proportionally |
| T-S3 | **Household load ramp**: oven turns on, L1 rises from 5A to 20A over 30s | ~3 cycles | Rolling mean smoothly reduces cap; no oscillation |
| T-S4 | **Oven off → recovery**: household load drops suddenly, headroom opens | 5 cycles | Setpoint naturally increases next cycle |
| T-S5 | **Borderline oscillation**: P_cap hovers at 4400W ± noise | 60s / 6 cycles | Hysteresis prevents start/stop cycling |
| T-S6 | **Cold startup**: boot with household at 20A L1, Eco mode, EV connected | 30s | First sample enables cap; startup fallback used; no unprotected charging |
| T-S7 | **Single-phase switch mid-charge**: 3-phase → 1-phase | 3 cycles | Active phases update; P_cap recalculated for single phase |
| T-S8 | **Grid export (sunny day)**: all phases negative | Steady | Cap is no-op; charger at full mode setpoint |
| T-S9 | **Sustained cap then clear**: cap active for 2 min, household drops, cap releases | 2 min | Verify setpoint tracks P_cap continuously, no hysteresis artefacts above min |

### Headroom Sensor Tests (FR-11)

| ID | Scenario | Expected |
|---|---|---|
| T-H1 | Grid idle (0A on all phases) | headroom = 100% per phase |
| T-H2 | Phase at safety limit (25.6A on 32A breaker) | headroom = 0% |
| T-H3 | Phase over safety limit (28A) | headroom negative (−9.4%) |
| T-H4 | Headroom > 100% (e.g. exporting) | Clamped to 100% |
| T-H5 | Data missing (grid current None) | Sensor publishes unavailable |

## 13. Out of Scope

- Actively rebalancing load across phases (hardware cannot).
- Predictive/ML load forecasting.
- Reacting to Victron/utility dynamic tariff or DynamicEss signals.
- Protecting anything other than the main incoming per-phase breaker.
- Modelling solar/battery contribution separately (the grid meter measurement already accounts
  for all internal generation/consumption).

## 14. Resolved Decisions

- **OQ-1 — RESOLVED:** Always-on, built-in safety mechanism. No toggle, not exposed to HA (FR-7).
- **OQ-2 — RESOLVED:** Safety fraction is a **fixed 0.80 constant**, not configurable (FR-6).
- **OQ-3 — RESOLVED:** Apply the cap **after** `state_machine.apply_charging_events`, as the
  final safety clamp (§9).
- **OQ-4 — RESOLVED:** Use a **fixed 30 s rolling mean** of per-phase current (FR-10).
- **OQ-5 — RESOLVED:** Use the **measured per-phase grid voltage** already read for voltage-drop
  diagnostics (`victron_l{1,2,3}_voltage_v`).
- **OQ-6 — RESOLVED:** Expose per-phase **breaker-headroom % diagnostic sensors** to HA (FR-11)
  rather than transient events; visible/graphable over time like the setpoint sensor.
- **OQ-7 — RESOLVED:** Detect active phases from **measured EV per-phase current** rather than
  the `SinglePhaseSwitching` register value (FR-8). A phase is active if current > 0.5 A.
- **OQ-8 — RESOLVED:** Add **instantaneous cap override** at 0.90 × I_brk (FR-12) to handle
  fast transients from solar/battery variability. The 0.80 safety margin handles steady-state;
  the 0.90 instantaneous threshold catches spikes within one control cycle.
- **OQ-9 — RESOLVED:** Add **restart hysteresis** of 500 W above `_MIN_CHARGE_W` (FR-13) to
  prevent start/stop oscillation at the charger minimum boundary. No timer needed — the
  hysteresis is purely a power threshold.
- **OQ-10 — RESOLVED:** Clear per-phase current to `None` on Victron read failure (FR-14).
  Stale data must never be used for safety computations.

## 15. Suggested Work Breakdown

1. Victron block read + store per-phase current + plausibility check + staleness clearing
   (FR-1, FR-14, FR-15, §8) + tests T-U31..T-U34.
2. `AppState` fields + config + persistence (§7) + tests T-U35..T-U39.
3. `mean_phase_current` + `limit_phase_current` helpers including instantaneous override,
   active phase detection, restart hysteresis, and logging (§6, §9, FR-8, FR-12, FR-13, FR-16)
   + tests T-U1..T-U30.
4. Loop integration — clamp after `apply_charging_events` (§9) + tests T-I1..T-I10.
5. HA config number `grid_breaker_limit` (§10) — wired in step 2; entity definition here.
6. HA observability sensors: breaker-headroom % (FR-11) + per-phase grid current (FR-9)
   + snapshot wiring (§10) + tests T-H1..T-H5.
7. Scenario-based integration tests (T-S1..T-S9) — validate end-to-end temporal behaviour.
8. Docs/README update + `config.yaml.example`.
