# Goodwe HCA G2 EV Charger Controller

[![GitHub](https://img.shields.io/badge/GitHub-Repository-181717?logo=github)](https://github.com/lerebel103/gw-charger-controller)
[![Docker Hub](https://img.shields.io/badge/Docker%20Hub-lerebel103%2Fgw--charger--controller-2496ED?logo=docker)](https://hub.docker.com/r/lerebel103/gw-charger-controller)
[![Release](https://img.shields.io/github/v/release/lerebel103/gw-charger-controller?label=Release)](https://github.com/lerebel103/gw-charger-controller/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/license/mit)

A Docker-based integration that bridges a GW22K-HCA-20 EV charger and a Victron GX device (both over Modbus TCP) with Home Assistant via MQTT discovery.

## Features

- **Three charge modes** — Eco, Manual, and Standby
- **Rolling mean start/stop** — Eco mode uses configurable rolling averages (1–10 min) to decide when to start and stop charging, preventing rapid cycling
- **Full HA integration** — all sensors, controls, and configuration exposed via MQTT discovery (no manual HA YAML needed)
- **All 3-phase** voltage, current, and voltage drop sensors
- **Total lifetime energy** tracking (register 10065, U32)
- **Runtime configuration** — all settings adjustable from HA without restarting
- **Runtime charger memory controls** from HA for advanced charging mode, plug-and-charge auto start, single-phase switching, and max grid power draw
- **Diagnostics** for charger communication link bitfield and per-link online states

## Installation and Configuration (Docker First)

Docker Compose is the recommended and first-class way to run this project.

### Prerequisites

- **Docker & Docker Compose**
- **MQTT broker** (Home Assistant's built-in broker works fine)
- **GW22K-HCA-20 EV charger** with Modbus TCP enabled
- **Victron GX** device (Color Control GX, Venus GX, CCGX, or similar) with Modbus TCP
- All three on the same network with static IPs (or DHCP reservations)

### Recommended Setup with Docker Compose

The repository already includes a ready-to-use compose example in `docker-compose.yml`.

```bash
# Clone the repository
git clone https://github.com/lerebel103/gw-charger-controller.git
cd gw-charger-controller

# Create your runtime config
cp config.yaml.example config.yaml
# Edit with your MQTT broker and device IPs
nano config.yaml

# Start using the included compose file
docker compose up -d

# Follow logs
docker compose logs -f gw-evcharger-controller

# Stop
docker compose down
```

You can also use the convenience targets:

```bash
make up
make logs
make down
```

See **Configuration** below for required `config.yaml` fields.

### Alternative: Local Development Setup

```bash
# Create a Python 3.13+ virtual environment
python3 -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows

# Install dependencies
pip install -e '.[dev]'

# Run tests
make test

# Run linting
make lint

# Format code
make format
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

### Runtime Configuration

All other settings (charge mode, discharge window, floor %, max charge power, min EV SOC, eco mean window, etc.) are configurable from Home Assistant and persisted automatically. Changes take effect immediately without restarting.

### Power Correction

This charger appears to under-read current and active power by about 5.6%. Use `correction_pct` (0.0–10.0%, default 5.6%) to scale those reported values so Home Assistant readings are closer to real measurements. The correction is applied before publishing values to Home Assistant.

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

Stops EV charging and then suppresses regular EV Modbus connections/reads/writes. Use this to temporarily disable automated charging without changing other settings.

Standby includes a narrow user-initiated exception path for runtime charger memory controls only:
- Advanced Charging Mode (register 10032)
- Plug and Charge Auto Start (register 10019)
- Single Phase Switching (register 10023)
- Max Grid Power Draw (register 10039)

These standby exceptions are on-demand only and use a short-lived session (connect → write → readback → disconnect). See [AGENTS.md](AGENTS.md) for implementation details.

## Runtime EV Controls

The following controls are exposed in Home Assistant and are not persisted to config.yaml (they are charger runtime memory values):

- **Advanced Charging Mode** (`select`)
   - Fast charging
   - PV charging
   - PV + battery hybrid charging
- **Plug and Charge Auto Start** (`switch`)
   - Off
   - On
- **Single Phase Switching** (`switch`)
   - Off
   - On
- **Max Grid Power Draw** (`number`)
   - 4200-22000 W (register 10039 raw range 42-220)

Outside standby these values are polled and reflected continuously. In standby they can still be changed via the exception path above.

## Diagnostics

Additional diagnostics are exposed to Home Assistant:

- **Charger Connection Status** (`number`, raw U16 from register 10018)
- **Charger Serial Number** (`text`, from register 10040 ASCII)
- **Per-bit connectivity binary sensors** from register 10018:
  - Wi-Fi router connected
  - IoT cloud connected
  - Inverter online
  - MID meter online
  - GW meter online
  - EMS online

The charger serial number is read once at startup/connection and used as Home Assistant device metadata (`device.serial_number`) in MQTT discovery payloads.

## Development

### Make Targets

```bash
make help       # Show all available targets
make test       # Run unit and property tests
make test-cov   # Run tests with coverage report
make lint       # Lint with ruff
make format     # Auto-format and fix with ruff
make build      # Build local Docker image
make build-multi # Build multi-arch (amd64 + arm64) and push to registry
make up         # Start with docker-compose (requires config.yaml)
make down       # Stop docker-compose containers
make logs       # Tail logs from running container
make clean      # Remove Docker images and containers
```

### Testing

The project uses `pytest` for unit testing with high coverage:

```bash
# Run all tests
pytest tests/

# Run with coverage
pytest tests/ --cov=app --cov-report=term-missing

# Run specific test file
pytest tests/unit/test_control_loop.py -v

# Run matching a pattern
pytest tests/ -k "eco" -v
```

**Test organization:**
- `tests/unit/` — Unit tests for individual modules
- `tests/unit/helpers.py` — Shared test fixtures (fake loop, mock clients)
- No external dependencies required (mocked Modbus and MQTT)

### Code Organization

See [AGENTS.md - Project Architecture](AGENTS.md#project-architecture) for a detailed overview of module responsibilities and data flow.

The codebase is split by domain to keep runtime behavior and integration boundaries clear:

- **`app/control/`** — Core charging policy and orchestration
  - `loop.py`: Master control loop (reads state, computes setpoint, publishes)
  - `state_machine.py`: Session and mode state transitions
  - `mode_strategies.py`: OO charge mode handlers (Eco, Manual, Standby)
  - `power_utils.py`: Rolling averages, grid fallback, battery limits
  - `snapshot.py`: State snapshot building for MQTT
  - `protocols.py`: Structural type hints for all helper functions
  - `constants.py`: Tunable thresholds (min/max power, cooldowns, etc.)

- **`app/modbus/`** — Device protocol clients
  - `ev.py`: GW22K-HCA-20 charger client (register I/O, state decoding)
  - `victron.py`: Victron GX client (system readings, grid meter)
  - Reconnect/backoff logic, protocol validation, status decoding

- **`app/ha/`** — Home Assistant MQTT integration
  - `client.py`: MQTT publish/subscribe and command handling
  - `entities.py`: Entity definitions for MQTT discovery
  - `parsers.py`: Payload parsing for HA commands
  - `device.py`: Device and discovery payload construction

- **`app/state/`** — Shared models and types
  - `models.py`: `AppState` (mutable runtime state) and `StateSnapshot` (immutable outputs)
  - `enums.py`: Charge modes, session states, charger status codes

- **`app/config.py`** — Configuration lifecycle (load YAML, validate, persist)

Cross-cutting helpers (e.g. exponential backoff, logging) stay outside domain packages.

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

Published when the setpoint is actually set to zero and charging has stopped. This event is delayed by at least 5 seconds after the setpoint goes to zero, ensuring the charger has fully wound down before other systems react.

```json
{"event": "stopped", "mode": "Eco", "reason": "max_soc_reached", "session_energy_wh": 12400, "ev_soc_pct": 79.9}
```

When EV SOC is unavailable, `ev_soc_pct` is `null`.

### Stop Reasons

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

## CI/CD

The project uses GitHub Actions for continuous integration, Docker image builds, and releases.

### Workflows

| Workflow | Trigger | What it does |
|---|---|---|
| **CI** | Every push and PR to `main` | Lints with `ruff`, runs `pytest` |
| **Build & Push Docker** | Every push (all branches and tags) | Multi-arch Docker build (amd64 + arm64). Pushes to DockerHub on `main` and version tags only. |
| **Release** | Tag push (`v*`) | Creates a GitHub Release with auto-generated changelog and Docker pull instructions |

### Required GitHub Secrets

Add these in your repo under **Settings → Secrets and variables → Actions**:

| Secret | Description |
|---|---|
| `DOCKERHUB_USERNAME` | Your DockerHub username |
| `DOCKERHUB_TOKEN` | A DockerHub access token (not your password) |

### Creating a Release

```bash
git tag -a v0.2.0 -m "Description of changes"
git push origin v0.2.0
```

This triggers all three workflows: CI runs lint + tests, the Docker image is built and pushed with the version tag, and a GitHub Release is created automatically.

### Branch Strategy

- `main` is protected — no direct pushes
- Work on feature branches, merge via pull requests
- Every branch push gets CI checks and a Docker build (but only `main` and tags push images to DockerHub)

## Hardware

- **EV Charger**: GoodWe GW22K-HCA-20 (Modbus TCP, slave ID 247, practical setpoint range 4400–22000 W for active charging). Note: the documented minimum is 4200 W (raw 42), which is used as a pre-stop register value before issuing an explicit stop command.
- **Inverter/Battery**: Victron GX (Modbus TCP, unit ID 100 for system, configurable for grid meter)

## License

MIT
