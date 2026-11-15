# GW Charger Controller

[![Docker Hub](https://img.shields.io/badge/Docker%20Hub-lerebel103%2Fgw--evcharger--controller-2496ED?logo=docker)](https://hub.docker.com/r/lerebel103/gw-evcharger-controller)
[![Release](https://img.shields.io/github/v/release/lerebel103/gw-charger-controller?label=Release)](https://github.com/lerebel103/gw-charger-controller/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/license/mit)

An intelligent EV charging controller that bridges a **GoodWe GW22K-HCA-20** charger and a **Victron GX** energy system with **Home Assistant** via MQTT. It dynamically adjusts the charge power setpoint to maximise solar self-consumption while protecting your main breaker from overload.

## What it does

- Reads real-time solar, battery, and grid data from the Victron GX over Modbus TCP
- Reads charger status and per-phase current/voltage from the GW22K over Modbus TCP
- Computes an optimal charge setpoint every few seconds based on the active charge mode
- Applies closed-loop, signed import-current based breaker protection to prevent main breaker overload
- Exposes all sensors, controls, and configuration to Home Assistant via MQTT discovery

## Charge Modes

| Mode | Behaviour |
|------|-----------|
| **Eco** | Maximises solar self-consumption. Daytime: ramps EV power up/down to track solar surplus. Night: draws from home battery at a fixed rate to reach a minimum EV SOC by morning. |
| **Manual** | Charges at a fixed user-configured power (4.4–22 kW). Resets to Eco on unplug. |
| **Standby** | Stops charging and suppresses all EV charger communication. |

### Auto-start below min SOC

An optional feature (toggle the "Auto-start below min SOC" switch in HA; formerly labelled "Eco Day Min Charge", the config key `eco_day_min_charge_enabled` and entity ID are unchanged) that ensures the EV reaches a minimum SOC as soon as it connects, day or night (Eco mode). It is a **floor**, not a cap. When enabled and the EV SOC is below the configured **Min EV SOC** threshold:

- The setpoint is never below the minimum power (4.4 kW). Grid power is drawn if solar is insufficient, so the car reaches a usable charge level
- With solar surplus the normal eco day ramp runs **above** 4.4 kW and ramps back down towards it, exactly as it does above Min EV SOC
- The eco gates (home-battery SOC gate, restart cooldown, start/stop conditions, battery-not-full minimum) cannot drop the setpoint below 4.4 kW or end the charging session while below Min EV SOC
- In the night (discharge) window it is min SOC first: the battery-discharge-limit trip, its cooldown and the grid fallback hold 4.4 kW instead of stopping; the night rate (`solar_battery_max_ev_charge_power_w`) is used when it is higher
- The breaker protection still applies and can cut below 4.4 kW or stop the charger if grid import would overload a phase
- When Min EV SOC is reached nothing resets: the same ramp simply continues and the gates apply again
- HA shows the mode sub-state `eco_day_min_charge` while held at the 4.4 kW floor and `eco_day_ramping` while ramping above it
- Disabled by default to preserve existing behaviour

### Eco Day Ramp
The eco day ramp is driven only by site signals: grid power, battery power and battery SOC (rolling means for start/stop, instantaneous battery power for the ramp direction). Each control cycle it adds one ramp step (`eco_day_ramp_step_w`) while battery power is above -200 W. When the battery discharges by more than 200 W it ramps down by one ramp step per cycle; a 5 minute mean battery discharge beyond `solar_battery_day_power_limit_w` stops the charge (or holds 4.4 kW while below Min EV SOC with the switch on). The car's actual draw is never used: the setpoint is a desired value, and a car that steps current in whole amps, lags or ignores it cannot stall the ramp. The ramp is bounded by 4.4 kW (or the floor above), the 22 kW charger maximum, the breaker limiter and the anti-windup clamp (see [docs/grid-power-scaling.md](docs/grid-power-scaling.md)).
## Breaker Protection

An always-on safety mechanism that keeps the 30 s mean grid **import current** on every phase below 80% of the configured breaker rating (32 A default, so 25.6 A). The Victron reports signed per-phase current (import positive, export negative), so export never counts as load. Current, not power, is used because a low power factor draws more current than the real power suggests. The cap is anchored to the measured EV current (the charger setpoint is a total over three phases). A raw reading above 90% drops to the minimum charge power immediately, and missing grid data fails safe to the minimum in every mode. See [docs/grid-power-scaling.md](docs/grid-power-scaling.md) for the full algorithm; `scripts/victron_probe.py` is a read-only tool to check the Victron registers.

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
