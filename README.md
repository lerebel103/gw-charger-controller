# Goodwe HCA G2 EV Charger Controller

A Docker-based integration that bridges a GW22K-HCA-20 EV charger and a Victron GX device (both over Modbus TCP) with Home Assistant via MQTT discovery.

## Features

- **Eco mode** — charges from excess solar, with overnight battery discharge window support
- **Manual mode** — fixed power charge, auto-resets to Eco on EV disconnect
- **5-minute hysteresis** on Eco pause/resume to prevent rapid charger cycling
- **Full HA integration** — all sensors, controls, and configuration exposed via MQTT discovery (no manual HA YAML needed)
- **All 3-phase** voltage, current, and voltage drop sensors
- **Total lifetime energy** tracking (register 10065, U32)
- **Runtime configuration** — all settings adjustable from HA without restarting

## Charge Modes

### Eco Mode

Eco mode maximises the use of free solar energy. It behaves differently depending on the time of day:

**Outside the battery discharge window** (daytime): the charger follows grid export power. When excess solar pushes more than 1400 W back to the grid, the charger ramps up to absorb it (clamped to 1380–11000 W). If the solar battery starts discharging or grid export drops below the threshold, charging continues at minimum power for a 5-minute grace period before actually stopping — this rides through transient cloud cover without cycling the charger.

**Inside the battery discharge window** (overnight): the charger runs at a fixed rate (Solar Battery Max Charge Power, default 5000 W), drawing from the solar battery and grid as needed. If the solar battery SOC drops to the discharge floor and the EV has reached its minimum SOC target, charging stops. If the EV hasn't reached its minimum SOC, charging continues even if that means importing from the grid.

### Manual Mode

Manual mode charges at a fixed power level configured via Home Assistant (1380–11000 W). It is intended for one-off fast charges and automatically resets to Eco mode when the EV is unplugged, so you don't accidentally leave it in Manual for the next session.

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

All other settings (charge mode, discharge window, floor %, max charge power, min EV SOC, etc.) are configurable from Home Assistant and persisted automatically.

## Development

```bash
make test      # Run all tests
make lint      # Lint with ruff
make format    # Auto-format with ruff
make build     # Build Docker image
make push      # Build & push multi-arch (amd64 + arm64)
```

## Vehicle SOC Input

The GW22K-HCA-20 does not expose the vehicle's state of charge over Modbus. To use SOC-aware features (like stopping charging when the EV reaches a target SOC during the battery discharge window), you need to feed the SOC in externally via MQTT.

**Topic:** `ev_charger/vehicle/soc/set`
**Payload:** a plain number representing the SOC percentage (0–100)
**QoS:** 0 or 1

Example (using `mosquitto_pub`):
```bash
mosquitto_pub -h 192.168.1.10 -t ev_charger/vehicle/soc/set -m "72"
```

This can be automated from Home Assistant using an automation that publishes the vehicle's SOC (e.g. from a car integration) to this topic on a regular interval. Most vehicle manufacturers provide a cloud API that exposes SOC — check if a Home Assistant integration exists for your car (e.g. Tesla, Hyundai/Kia Connect, BMW Connected Drive, etc.) and set up a simple automation to forward the SOC value to this topic whenever it updates.

If no SOC update is received for 5 minutes, the value is treated as unavailable and the controller assumes the EV has not yet reached its minimum SOC target (charging continues).

## Hardware

- **EV Charger**: GoodWe GW22K-HCA-20 (Modbus TCP, slave ID 247)
- **Inverter/Battery**: Victron GX (Modbus TCP, unit ID 100 for system, configurable for grid meter)

## License

MIT
