# Agent Instructions for GW Charger Controller

## Standby Mode - No Modbus Interactions

When the controller is in **standby mode** (`charge_mode == "Standby"`):

- **No modbus reads** should be performed (except configuration reads)
- **No modbus writes** should be performed (except to set setpoint to zero)
- **No connection attempts** should be made to the EV charger

Once the mode selection to standby is achieved and the setpoint reaches 0, the charger should be left completely untouched until the mode is changed away from standby.

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
