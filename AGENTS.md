# Agent Instructions for GW Charger Controller

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
