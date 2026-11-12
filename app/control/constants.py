"""Control-loop constants."""

# Charger hardware limits
_MIN_CHARGE_W = 4400.0
_MAX_CHARGE_W = 22000.0
_STOP_PRESET_W = 4200.0  # written before stop command to avoid writing setpoint=0 to charger

# Eco outside-window thresholds (applied to rolling means)
_GRID_EXPORT_START_THRESHOLD_W = -1400.0  # mean grid_power_w <= this -> start charging
_ECO_DAY_RAMP_STEP_W = 200.0  # ramp step per control loop iteration
_BATTERY_POWER_DEADBAND_W = 200.0  # ignore battery power within this range of zero
_ECO_DAY_COOLDOWN_S = 300.0  # 5 min cooldown after eco day charging stops before restarting
_RAMP_DEADBAND_W = 200.0  # ignore battery power within +/-200 W

_EV_MAX_SOC_DEFAULT = 80.0  # reset value on disconnect
# At 100% target, apply a 0.5% margin so the "stopping" event can be emitted before the
# EV itself cuts charging (which would be detected as an external stop rather than max_soc_reached).
# For any other target, no margin is used — the EV charger controls the cutoff cleanly.
_EV_MAX_SOC_MARGIN_PCT = 0.5  # only applied when ev_max_soc_pct == 100.0
_STOPPING_MIN_DELAY_S = 10.0  # min time between stopping and stopped events
_STOPPED_DELAY_S = 5.0  # delay after setpoint->0 before emitting stopped event
_EV_SOC_STALE_S = 300.0  # 5 minutes - treat SOC as unavailable if not updated
_EV_STATUS_STALE_S = 300.0  # 5 minutes - treat EV status as stale if not refreshed
_EXTERNAL_STOP_CONFIRM_TICKS = 2  # consecutive non-charging status ticks before external stop

# Breaker protection constants (closed-loop, import-power based; see docs/grid-power-scaling.md)
_BREAKER_SAFETY_FRACTION = 0.80  # mean import must stay below this fraction of the breaker rating
_BREAKER_INSTANT_THRESHOLD_FRACTION = 0.90  # raw import above this fraction -> immediate minimum setpoint
_PHASE_IMPORT_MEAN_WINDOW_S = 30.0  # rolling window for per-phase import / EV power means
_PHASE_ACTIVE_THRESHOLD_A = 0.5  # EV phase considered active if raw charger current exceeds this
_PHASE_COUNT = 3
_BREAKER_RELEASE_MARGIN_A = 2.0  # release when mean import < P_lim - 2 A * V
_BREAKER_CAP_RELEASE_STEP_W = 200.0  # gradual cap raise per cycle after release
_BREAKER_STOP_CYCLES = 3  # cycles still overloaded at minimum before the charger is stopped
_BREAKER_MISSING_DATA_CYCLES = 3  # consecutive invalid cycles before fail-safe minimum setpoint
_BREAKER_PLAUSIBILITY_RATIO = 1.05  # |P| must be <= 1.05 * V * I + tolerance
_BREAKER_PLAUSIBILITY_TOLERANCE_W = 50.0
_BREAKER_FALLBACK_VOLTAGE_V = 230.0  # used when grid voltage is missing or outside 100..300 V
