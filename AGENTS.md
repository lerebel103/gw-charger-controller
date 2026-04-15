# Agent Instructions for GW Charger Controller

## Project Architecture

This document describes the implementation conventions and architectural decisions for agents working on this codebase.

### Data Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                     Home Assistant (MQTT)                       │
│  - Entity discovery                                             │
│  - State subscriptions (sensors, switches, selects, numbers)    │
│  - Command subscriptions (set charge mode, max SOC, etc.)       │
└────────────────────────┬──────────────────────────────────────┘
                         │ publish / subscribe
                         │
                    ┌────▼─────────────────────────────────┐
                    │  app/ha/client.py (MQTTClient)       │
                    │  - MQTT discovery payloads           │
                    │  - Command parsing & validation      │
                    │  - State publishing (app → HA)       │
                    └────┬──────────────────────────────────┘
                         │ read/write state
                         │
        ┌────────────────┼────────────────┐
        │                │                │
        │         ┌──────▼──────────┐     │
        │         │   app/state/    │     │
        │         │  (AppState)     │     │
        │         │  Read-write     │     │
        │         │  mutable state  │     │
        │         └────┬──────┬─────┘     │
        │              │      │           │
        │         ┌────▼──────▼────────────┐
        │         │  app/control/loop.py   │
        │         │  (ControlLoop)         │
        │         │  - Poll modbus clients │
        │         │  - Run state machine   │
        │         │  - Compute setpoint    │
        │         │  - Build snapshots     │
        │         └────┬──────┬────────────┘
        │              │      │
   ┌────▼──────┐  ┌───▼──┐ ┌─▼──────────────┐
   │ app/ha/   │  │state │ │ app/control/   │
   │ entities. │  │_mach │ │ snapshot.py    │
   │ py        │  │ine   │ │ (StateSnapshot)│
   │(discovery)│  └──────┘ │ Read-only      │
   └───────────┘           │ immutable      │
                           └────┬───────────┘
                                │ publish
                                │
        ┌───────────────────────▼──────────┐
        │  app/modbus/ev.py                │
        │  (EVChargerModbusClient)         │
        │  - Register reads/writes         │
        │  - Status decoding               │
        │  - Reconnect/backoff logic       │
        └────┬────────────────────────────┐
             │  Modbus TCP                 │
             │                             │
        ┌────▼─────────────┐       ┌──────▼───────────┐
        │  GW22K-HCA-20    │       │  Victron GX      │
        │  EV Charger      │       │  Inverter/       │
        │  (Slave 247)     │       │  Battery System  │
        └──────────────────┘       └──────────────────┘
```

### Module Responsibilities

#### `app/control/loop.py` (ControlLoop)
- **Purpose**: Master orchestrator for the charging control algorithm
- **Cycle**: Runs periodically (every few seconds)
  1. Read EV charger status, Victron readings, and Home Assistant state
  2. Run session state machine (Idle → Charging → Stopping → Stopped)
  3. Call mode-specific setpoint strategy (Eco, Manual, or Standby)
  4. Publish sensor updates and charging events to MQTT
  5. Write setpoint to charger via Modbus

#### `app/control/state_machine.py` (ChargingStateMachine)
- **Purpose**: Session lifecycle management (when does charging start/stop?)
- **Instance variables**: `ChargeSessionState` (Idle/Charging/Stopping/Stopped)
- **Key methods**:
  - `transition_to_charging()`: Check charger is ready, emit `started` event
  - `apply_charging_events()`: Manage stop conditions and grace periods
  - `determine_stop_reason()`: Why is charging stopping? (SOC reached, vehicle disconnected, etc.)

#### `app/control/mode_strategies.py` (Mode Handlers)
- **Purpose**: Compute setpoint power based on charge mode and current conditions
- **Classes**: `EcoModeHandler`, `ManualModeHandler`, `StandbyModeHandler`, etc.
- **Pattern**: OO strategy; each mode has a `compute(loop) -> float` method
- **Dispatch**: `compute_setpoint()` routes to the appropriate handler based on `loop._state.charge_mode`

#### `app/control/power_utils.py` (Utility Functions)
- **Purpose**: Helpers for rolling means, grid fallback calculation, battery discharge limiting
- **Consumers**: mode_strategy handlers and the state machine

#### `app/control/snapshot.py` (StateSnapshot)
- **Purpose**: Build immutable view of loop state for publishing to MQTT
- **Immutability**: Snapshot is read-only; allows safe concurrent publishing while loop modifies state

#### `app/modbus/ev.py` (EVChargerModbusClient)
- **Purpose**: Modbus register I/O for the charger
- **Register operations**:
  - Read: status, active power, current, voltage, SOC request result, lifetime energy
  - Write: setpoint (first), start command, stop command
- **Reconnect/Backoff**: Exponential backoff on comms failure

#### `app/modbus/victron.py` (VictronModbusClient)
- **Purpose**: Modbus register I/O for Victron GX
- **Read**: Battery SOC, power, grid power; voltage/current per phase
- **Backoff**: Exponential backoff on comms failure

#### `app/ha/client.py` (MQTTClient)
- **Purpose**: Bridge between AppState and Home Assistant
- **Discovery**: Publish MQTT discovery payloads on startup
- **Publishing**: Poll loop state every cycle, publish sensor values
- **Commands**: Subscribe to charger commands (mode, max SOC, etc.), validate, apply to state
- **Exception**: Standby mode allows on-demand charger register writes (register 10032, 10019, 10023, 10039)

#### `app/state/models.py` (AppState)
- **Structure**: Mutable dataclass holding all runtime state
- **Persisted fields**: Subset saved to config.yaml (charge mode, times, thresholds)
- **Transient fields**: EV charger status, power readings, timestamps (not persisted)

---

## Standby Mode - No Modbus Interactions

When the controller is in **standby mode** (`charge_mode == "Standby"`):

- **No modbus reads** should be performed (except configuration reads)
- **No modbus writes** should be performed (except the one-time standby transition sequence to stop charging)
- **No connection attempts** should be made to the EV charger

Once the mode selection to standby is achieved and the setpoint reaches 0, the charger should be left completely untouched until the mode is changed away from standby.

### Standby Exception - User-Initiated Runtime Commands

An explicit exception is allowed for user-initiated Home Assistant commands that update charger runtime memory while in standby:

- Register **10032** (Advanced Charging Mode)
- Register **10019** (Plug and Charge auto start)
- Register **10023** (Single phase switching)
- Register **10039** (Max grid power draw)

Constraints for this exception:
- Must be **on-demand only** (no periodic polling/writing)
- Must use a short-lived session: connect, write, read-back/confirm, disconnect
- Must not re-enable normal control-loop EV Modbus activity in standby

### Rationale

Standby mode is meant to completely disable the integration with the EV charger. Performing any reads or writes, or maintaining a connection, defeats the purpose of a true "standby" state. This ensures:
- Minimal power consumption
- No interference with manual charger operation
- Clean separation between automated and manual control

## Charger Status Register (10017)

Register 10017 contains the current charger status. This should be:
- **Read every cycle** (except in standby mode)
- **Propagated to Home Assistant** as a sensor (except in standby mode)
- **Used internally** to track charger operational state for future logic improvements

The status values indicate the charger's current operational mode and should be properly decoded and exposed for monitoring and diagnostics.
