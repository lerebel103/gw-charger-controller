---
name: ev-calibration
description: Check or retune the EV charger current correction (correction_pct) or the computed charge power using a Home Assistant history CSV and the Victron zero-sum. Use when the charger readings seem off, after a firmware change, or with a different car.
---
# EV calibration (zero-sum edge method)
Findings so far (2026-11-15, one 1-phase and one 3-phase car): the current correction of 5.6 % is within error of the true factor (1-phase about 1.08, 3-phase about 1.02-1.05; the data cannot separate them). Sum(V x corrected I) matches real power to about 3 % RMS. The charger register does not (about 10 % low on 1-phase, 100 W steps).
1. **Data**: an HA history CSV (`entity_id,state,last_changed`, UTC; see the dev-workflow steering for where to put it). Needed: charger currents, voltages and power; `victron_grid_*`, `victron_vebus_activein_*` and `victron_pvinverter_*` currents and powers per phase. Without the Multiplus (vebus) entities the battery covers the EV load and the grid does not move, so the method fails.
2. **Ignore** any window where the correction was being changed by hand (check `number.ev_charger_ev_measurement_correction`), sensors reading `unavailable`, and comms drops.
3. **Edges**: find start and stop steps of the charger current. Use 1 s last-value-hold series. Compare windows of at least 30-60 s before and after, skipping the first 12-25 s (charger sensors lag up to 6 s, the Multiplus drifts for 20-30 s). Avoid baselines near zero amps: grid and Multiplus values flip sign there.
4. **Equation** per phase: `EV = dGrid - dVebusIn + dPV` (signs in the domain-conventions steering). Check it on a 1-phase edge: the unused phases must come out near 0.
5. **Current factor** = true EV current / charger raw current (published / factor). **Power**: compare true real power with the register and with sum(V x I). Report mean, sd and n per vehicle, and the PV-disturbed edges separately.
6. **Caveats to state**: current is apparent, real power includes power factor (about 0.95 here); Victron meters are not calibrated references (1-2 %); house load noise is about 2-3 % per point. Computed power reads 0 W unless the charger status is 3 and register 10015 is above 0, so stop-edge polls with residual current show 0; at a stop edge compare sum(V x I) from the published currents, not the power sensor.
7. Do not retune on fewer than about 5 clean edges per vehicle. Changing `correction_pct` is a user decision.
