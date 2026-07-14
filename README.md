# GW Charger Controller

[![Docker Hub](https://img.shields.io/badge/Docker%20Hub-lerebel103%2Fgw--evcharger--controller-2496ED?logo=docker)](https://hub.docker.com/r/lerebel103/gw-evcharger-controller)
[![Release](https://img.shields.io/github/v/release/lerebel103/gw-charger-controller?label=Release)](https://github.com/lerebel103/gw-charger-controller/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/license/mit)

An intelligent EV charging controller that bridges a **GoodWe GW22K-HCA-20** charger and a **Victron GX** energy system with **Home Assistant** via MQTT. It dynamically adjusts the charge power setpoint to maximise solar self-consumption while protecting your main breaker from overload.

## What it does

- Reads real-time solar, battery, and grid data from the Victron GX over Modbus TCP
- Reads charger status and per-phase current/voltage from the GW22K over Modbus TCP
- Computes an optimal charge setpoint every few seconds based on the active charge mode
- Applies a per-phase breaker protection cap to prevent main breaker overload
- Exposes all sensors, controls, and configuration to Home Assistant via MQTT discovery

## Charge Modes

| Mode | Behaviour |
|------|-----------|
| **Eco** | Maximises solar self-consumption. Daytime: ramps EV power up/down to track solar surplus. Night: draws from home battery at a fixed rate to reach a minimum EV SOC by morning. |
| **Manual** | Charges at a fixed user-configured power (4.4–22 kW). Resets to Eco on unplug. |
| **Standby** | Stops charging and suppresses all EV charger communication. |

## Breaker Protection

An always-on safety mechanism that dynamically caps the charge setpoint to keep every grid phase below 80% of the configured breaker rating. Uses a 30s rolling mean for smooth operation and an instantaneous override at 90% for fast transients (e.g. solar dropout). See [docs/grid-power-scaling.md](docs/grid-power-scaling.md) for the full algorithm.

## Quick Start

```bash
git clone https://github.com/lerebel103/gw-charger-controller.git
cd gw-charger-controller

cp config.yaml.example config.yaml
# Edit config.yaml with your MQTT broker and device IPs

docker compose up -d
docker compose logs -f gw-evcharger-controller
```

## Configuration

Minimal `config.yaml`:

```yaml
mqtt_host: "192.168.1.10"
mqtt_port: 1883
mqtt_username: "ha_user"
mqtt_password: "secret"
ev_charger_ip: "192.168.1.20"
victron_ip: "192.168.1.30"
```

All other settings (charge mode, thresholds, timings, breaker limit) are configurable from Home Assistant at runtime and persisted automatically.

## Prerequisites

- Docker and Docker Compose
- MQTT broker (Home Assistant's built-in Mosquitto works)
- GW22K-HCA-20 with Modbus TCP enabled
- Victron GX device with Modbus TCP enabled
- All devices on the same network

## Home Assistant Integration

The controller auto-discovers in HA via MQTT — no manual YAML needed. You get:

- **Sensors**: charger power, per-phase voltage/current, grid current, breaker headroom %, EV SOC, session energy, total energy, voltage drop, uptime
- **Controls**: charge mode select, manual power slider, EV SOC targets, breaker limit, discharge window times, all eco thresholds
- **Diagnostics**: charger status, communication link states, serial number
- **Events**: charging started/stopping/stopped with reason and energy totals

## Vehicle SOC

The GW22K doesn't expose EV battery SOC over Modbus. Feed it externally via MQTT:

```
Topic: ev_charger/vehicle/soc/set
Payload: 72
```

Automate this from your car integration in Home Assistant. If no update arrives within 5 minutes, SOC is treated as unavailable.

## Development

```bash
# Install uv (https://docs.astral.sh/uv/)
uv sync --group dev

# Run tests
uv run pytest tests/ -v

# Lint + format
uv run ruff check app/ tests/
uv run ruff format app/ tests/
```

See [AGENTS.md](AGENTS.md) for architecture details and module responsibilities.

## Hardware

- **EV Charger**: GoodWe GW22K-HCA-20 (Modbus TCP, slave ID 247, setpoint range 4.4–22 kW)
- **Energy System**: Victron GX (Modbus TCP, unit ID 100 for system, configurable for grid meter)

## License

MIT
