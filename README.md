# Goodwe HCA G2 EV Charger Controller

A Docker-based integration that bridges a GW22K-HCA-20 EV charger and a Victron GX device (both over Modbus TCP) with Home Assistant via MQTT discovery.

## Features

- **Three charge modes** — Eco, Manual, and Standby
- **Rolling mean start/stop** — Eco mode uses configurable rolling averages (1–10 min) to decide when to start and stop charging, preventing rapid cycling
- **Full HA integration** — all sensors, controls, and configuration exposed via MQTT discovery (no manual HA YAML needed)
- **All 3-phase** voltage, current, and voltage drop sensors
- **Total lifetime energy** tracking (register 10065, U32)
- **Runtime configuration** — all settings adjustable from HA without restarting

## Charge Modes

### Eco Mode

Eco mode maximises the use of free solar energy. It behaves differently depending on the time of day:

**Outside the battery discharge window** (daytime):
- Home battery SOC below 90% (configurable via HA): no EV charging. Battery gets full priority.
- Home battery SOC 90-99%: EV charges at minimum power (4400 W) only, preserving home battery charging capacity.
- Home battery SOC 100%: full ramp mode kicks in.
  - A rolling mean of grid power is computed over a configurable window (default 5 min).
  - Charging starts when the mean grid power drops to -1400 W or below (sustained solar export).
  - The setpoint ramps up from minimum, using home battery power as feedback to find the max sustainable rate.
  - If the home battery starts discharging, the setpoint is reduced to prevent home battery drain.
  - Charging stops when the mean home battery power indicates sustained discharge.
  - After stopping, a 5-minute cooldown prevents restarting to avoid rapid on/off cycling from clouds or transient house loads.

**Inside the battery discharge window** (default 23:00–06:00, configurable):
- Charges at a fixed rate (Solar Battery Max EV Charge Power, default 5000 W), drawing from the home battery and grid as needed.
- If the home battery SOC drops to the discharge floor and the EV has reached its minimum SOC target, charging stops.
- If the EV hasn't reached its minimum SOC, charging continues even if that means importing from the grid.

### Manual Mode

Charges at a fixed power level configured via Home Assistant (4200–22000 W). Intended for one-off fast charges. Automatically resets to Eco mode when the EV is unplugged, so you don't accidentally leave it in Manual for the next session.

### Standby Mode

Sets the charge power to zero. No EV charging takes place. Use this to temporarily disable all charging without changing other settings.

## Getting Started

1. Copy and edit the config file:
   ```bash
   cp config.yaml.example config.yaml
   # Edit with your MQTT broker and device IPs
   ```

2. Run with Docker Compose:
   ```bash
   make up
   ```

## Configuration

Minimal `config.yaml`:
```yaml
mqtt_host: "192.168.1.10"
mqtt_port: 1883
mqtt_username: "ha_user"
mqtt_password: "secret"
ev_charger_ip: "192.168.1.20"
ev_charger_port: 502
victron_ip: "192.168.1.30"
victron_port: 502
```

All other settings (charge mode, discharge window, floor %, max charge power, min EV SOC, eco mean window, etc.) are configurable from Home Assistant and persisted automatically.

## Development

```bash
make test      # Run all tests
make lint      # Lint with ruff
make format    # Auto-format with ruff
make build     # Build Docker image
make push      # Build & push multi-arch (amd64 + arm64)
```

## Charging Events

The controller publishes JSON events to `ev_charger/event/charging` to notify other systems of charging state changes. This is useful for Home Assistant automations that need to react to power draw changes.

**Topic:** `ev_charger/event/charging`

### Event: started

Published when charging begins (setpoint goes from 0 to a positive value).

```json
{"event": "started", "mode": "Eco", "setpoint_w": 4400}
```

### Event: stopping

Published at least 10 seconds before charging actually stops. The charger continues at the current setpoint during this grace period, giving other systems time to prepare for the power change.

```json
{"event": "stopping", "mode": "Eco", "reason": "max_soc_reached", "setpoint_w": 6200, "active_power_w": 5800}
```

If the stop condition clears during the 10-second grace period (e.g. a cloud passes), the stop is cancelled and a new `started` event is emitted.

### Event: stopped

Published when the setpoint is actually set to zero and charging has stopped.

```json
{"event": "stopped", "mode": "Eco", "reason": "max_soc_reached", "session_energy_wh": 12400, "ev_soc_pct": 79.9}
```

When EV SOC is unavailable, `ev_soc_pct` is `null`.

### Stop reasons

| Reason | Description |
|---|---|
| `max_soc_reached` | EV SOC reached the max charge target |
| `vehicle_disconnected` | EV was unplugged |
| `standby` | User switched to Standby mode |
| `victron_down` | Victron GX communications lost (Eco mode only) |
| `eco_day_soc_gate` | Home battery SOC dropped below the daytime threshold |
| `eco_day_mean_battery` | Sustained home battery discharge detected |
| `eco_day_conditions` | Daytime solar conditions no longer sufficient |
| `eco_night_floor` | Home battery at discharge floor, EV target met |

## Vehicle SOC Input

The GW22K-HCA-20 does not expose the vehicle's state of charge over Modbus. To use SOC-aware features (like stopping charging when the EV reaches a target SOC during the battery discharge window), feed the SOC in externally via MQTT.

**Topic:** `ev_charger/vehicle/soc/set`
**Payload:** a plain number representing the SOC percentage (0–100)

Example:
```bash
mosquitto_pub -h 192.168.1.10 -t ev_charger/vehicle/soc/set -m "72"
```

This can be automated from Home Assistant using an automation that publishes the vehicle's SOC (from a car integration) to this topic on a regular interval. If no SOC update is received for 5 minutes, the value is treated as unavailable and the controller assumes the EV has not yet reached its minimum SOC target.

## Hardware

- **EV Charger**: GoodWe GW22K-HCA-20 (Modbus TCP, slave ID 247, setpoint range 4400–22000 W or 0 for pause). Note: the documented minimum is 4200 W (raw 42) but in practice the charger rejects values below 4400 W (raw 44).
- **Inverter/Battery**: Victron GX (Modbus TCP, unit ID 100 for system, configurable for grid meter)

## License

MIT
