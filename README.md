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
- A rolling mean of grid power and solar battery power is computed over a configurable window (default 5 minutes, adjustable 1–10 min via HA).
- Charging starts when the mean grid power drops to -1400 W or below (sustained solar export).
- The setpoint tracks instantaneous grid export, clamped to 4200–22000 W (charger hardware limits).
- If the solar battery starts discharging, the setpoint is reduced to prevent battery drain.
- Charging stops when the mean solar battery power exceeds +500 W, indicating the home battery is being drained.

**Inside the battery discharge window** (default 23:00–06:00, configurable):
- Charges at a fixed rate (Solar Battery Max EV Charge Power, default 5000 W), drawing from the solar battery and grid as needed.
- If the solar battery SOC drops to the discharge floor and the EV has reached its minimum SOC target, charging stops.
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
