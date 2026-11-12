# Incident 2026-11-07: breaker cap wrongly throttled a single-phase EV

## What happened

In Eco-day mode a **single-phase car on L1** was charging while PV was balanced across the three
phases. The old limiter treated the meter current as signed, anchored its cap to the instantaneous EV
current and wrote a single-phase current limit (31 A x ~233 V = ~7.45 kW) as the **total** charger
setpoint. The setpoint is a total over three phases, so the car received about a third of it (~2.3 kW)
and the Eco-day ramp could never recover.

The data shows the breaker was nowhere near saturation: L1 imported only **+300..+1022 W** (the 80 %
import limit is ~5.96 kW at 233 V) while L2/L3 were **exporting** about -0.8..-1.3 kW. All meter
currents are positive because the meter reports unsigned magnitudes. The EV current on L1 rose from
0 to ~9.6 A.

## Files

| File | Source | Time base |
| --- | --- | --- |
| `ha_history_per_phase.csv` | Home Assistant history export (`entity_id,state,last_changed`) | **UTC** (`Z`), 2026-11-07 06:32:00 to 06:59:59 |
| `grafana_power_overview.csv` | Grafana table export (`_time,Consumption,Solar,EV,Grid,Battery Power,Battery Charge`) | **Local time, UTC+8**, 14:32 to 15:22 local (= 06:32 to 07:22 UTC) |

Both are copies of the originals the user provided (`history-3.csv` and the Grafana `-data-...csv`).

## Columns

`ha_history_per_phase.csv` (long format, one row per state change):

- `sensor.em540_energy_meter_bridge_power_l1..l3`: signed grid power per phase in W (+ import, - export)
- `sensor.em540_energy_meter_bridge_current_l1..l3`: **unsigned** grid current per phase in A
- `sensor.ev_charger_ev_current_l1..l3`: EV charger phase current in A (only L1 carries load)
- `sensor.victron_*`: Victron-side views of the same grid power and the PV inverter per phase

`grafana_power_overview.csv`: values are strings with units (`1.8 kW`, `626.9 W`) or bare `0`; blank cells
mean no value at that instant. Use it for the whole-site picture (consumption, solar, EV, grid, battery).

## Caveats

- The HA export ends at 07:00 UTC; the Grafana export runs about 22 minutes longer.
- Grid voltage is not in the HA export; replays use 233 V (the ratio of P to I at the time).
- Sensors update at slightly different instants, so replay tests resample to 10 s control cycles with a
  zero-order hold (`tests/unit/incident_data.py`).
