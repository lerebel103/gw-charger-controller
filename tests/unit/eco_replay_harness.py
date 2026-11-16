"""Replay harness: drives the REAL ControlLoop.run_loop with a fake clock, patched asyncio.sleep and mocked clients.

Two kinds of input:
* open-loop replay of recorded site + EV values (``recorded_scenario``), and
* a plant model plus a car model (``CAR_MODELS``) where the site signals respond to the car's draw.

The setpoint is a desired value; assertions in the tests are about the setpoint's response to site signals,
never about matching the car's draw.

The EV sample is taken before the site read in each cycle, so the site signals and the car draw are aligned.
"""

from __future__ import annotations

import asyncio
import math
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

from app.control import breaker, mode_strategies, power_utils, snapshot, state_machine
from app.control import loop as loop_module
from app.control.loop import ControlLoop
from app.state import AppState, ChargerStatus, ChargeSessionState

CYCLE_S = 5.0
CORRECTION = 1.056  # correction_pct 5.6
_RAW_MIN = 44  # write_setpoint(): 4.4 kW register minimum
RATED_V = 4400.0 / 18.0  # the charger rates 6 A as 4.4 kW
CAR_EFFICIENCY = 0.96  # 6 A draws 4224 W when 4400 W is commanded (recorded in both CSVs)
PHASE_OFFSETS = (0.0, -0.1, 0.1)  # reproduces the recorded 6.0192 / 5.9136 / 6.1248 A spread


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


# --------------------------------------------------------------------------------------------------
# Car models. respond(written_w, volts, cycle) -> (calibrated phase currents A, power W, nominal amps)
# --------------------------------------------------------------------------------------------------
def register_w(written_w: float) -> float:
    """What write_setpoint() stores: round(W / 100) x 100 W, minimum 4400 W."""
    return max(round(written_w / 100.0), _RAW_MIN) * 100.0


def _from_power(power_w: float, volts: float) -> tuple[float, float, float]:
    if power_w <= 0:
        return 0.0, 0.0, 0.0
    per_phase = power_w / (3.0 * volts)
    return tuple(max(0.0, per_phase + o) for o in PHASE_OFFSETS)  # type: ignore[return-value]


class Car:
    name = "car"

    def respond(self, written_w: float, volts: float, cycle: int) -> tuple[tuple[float, float, float], float, float]:
        raise NotImplementedError


class StepAmpCar(Car):
    """Whole-amp car: amps = floor(register W / (3 V)) clamped 6..32 A, draws 0.96 x the rated power of that step."""

    name = "step"

    def amps(self, written_w: float, volts: float) -> int:
        return int(min(32, max(6, math.floor(register_w(written_w) / (3.0 * volts)))))

    def respond(self, written_w, volts, cycle):
        amps = self.amps(written_w, volts)
        power = CAR_EFFICIENCY * amps * 3.0 * RATED_V
        return _from_power(power, volts), power, float(amps)


class UnevenStepCar(Car):
    """Illustrative uneven steps (min register W -> A). The real car's table is unknown."""

    name = "uneven"
    TABLE = (
        (4400, 6),
        (5300, 8),
        (6100, 9),
        (7300, 11),
        (9000, 14),
        (11000, 16),
        (14000, 20),
        (17000, 25),
        (22000, 32),
    )

    def respond(self, written_w, volts, cycle):
        reg = register_w(written_w)
        amps = 6
        for threshold, a in self.TABLE:
            if reg >= threshold:
                amps = a
        power = CAR_EFFICIENCY * amps * 3.0 * RATED_V
        return _from_power(power, volts), power, float(amps)


class IdealFollowerCar(Car):
    name = "ideal"

    def respond(self, written_w, volts, cycle):
        power = register_w(written_w)
        return _from_power(power, volts), power, power / (3.0 * volts)


class LaggingCar(Car):
    """Follows the inner model's response to the setpoint written ``lag`` cycles earlier."""

    name = "lagging"

    def __init__(self, lag: int = 3, inner: Car | None = None) -> None:
        self.lag = lag
        self.inner = inner or StepAmpCar()
        self._history: list[float] = []

    def respond(self, written_w, volts, cycle):
        self._history.append(written_w)
        old = self._history[max(0, len(self._history) - 1 - self.lag)]
        return self.inner.respond(old, volts, cycle)


class IgnoringCar(Car):
    """Stays at 6 A whatever is commanded; optionally becomes an ideal follower from cycle ``wake_at``."""

    name = "ignoring"

    def __init__(self, wake_at: int | None = None) -> None:
        self.wake_at = wake_at
        self._ideal = IdealFollowerCar()

    def respond(self, written_w, volts, cycle):
        if self.wake_at is not None and cycle >= self.wake_at:
            return self._ideal.respond(written_w, volts, cycle)
        power = CAR_EFFICIENCY * 6 * 3.0 * RATED_V
        return _from_power(power, volts), power, 6.0


CAR_MODELS: dict[str, Callable[[], Car]] = {
    "step": StepAmpCar,
    "uneven": UnevenStepCar,
    "lagging": LaggingCar,
    "ignoring": IgnoringCar,
    "ideal": IdealFollowerCar,
}


# --------------------------------------------------------------------------------------------------
# Plant (site) models: (cycle, car_power_w) -> AppState site fields
# --------------------------------------------------------------------------------------------------
def site_fields(
    *,
    battery_power_w: float,
    battery_soc_pct: float,
    grid_power_w: float,
    volts: float = 240.0,
    extra_import_a: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> dict:
    per_phase = grid_power_w / (3.0 * volts)
    return {
        "solar_battery_power_w": battery_power_w,
        "solar_battery_soc_pct": battery_soc_pct,
        "grid_power_w": grid_power_w + sum(extra_import_a) * volts,
        "victron_l1_current_a": per_phase + extra_import_a[0],
        "victron_l2_current_a": per_phase + extra_import_a[1],
        "victron_l3_current_a": per_phase + extra_import_a[2],
        "victron_l1_voltage_v": volts,
        "victron_l2_voltage_v": volts,
        "victron_l3_voltage_v": volts,
    }


def constant_site(
    battery_power_w: float = 85.0, battery_soc_pct: float = 99.0, grid_power_w: float = -3000.0, **kw
) -> Callable[[int, float], dict]:
    """Fixed site signals regardless of what the car does (CSV-like replays where the battery signal is held)."""
    return lambda i, car_w: site_fields(
        battery_power_w=battery_power_w, battery_soc_pct=battery_soc_pct, grid_power_w=grid_power_w, **kw
    )


def surplus_site(
    surplus_w: float | Callable[[int], float], battery_soc_pct: float = 99.0, **kw
) -> Callable[[int, float], dict]:
    """Energy balance: net = surplus - car power. Full battery: net >= 0 exports (battery +85 W);
    net < 0 is covered by the battery (discharge) with no grid import."""

    def site(i: int, car_w: float) -> dict:
        surplus = surplus_w(i) if callable(surplus_w) else surplus_w
        net = surplus - car_w
        if net >= 0:
            return site_fields(battery_power_w=85.0, battery_soc_pct=battery_soc_pct, grid_power_w=-net, **kw)
        return site_fields(battery_power_w=net, battery_soc_pct=battery_soc_pct, grid_power_w=0.0, **kw)

    return site


# --------------------------------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------------------------------
@dataclass
class CycleRecord:
    i: int
    t: float
    commanded_w: float
    written_w: float | None
    mode_state: object
    ramp_w: float
    battery_power_w: float | None
    car_amps: float
    ev_power_w: float | None
    eco_charging: bool
    session_state: ChargeSessionState


@dataclass
class Result:
    records: list[CycleRecord]
    loop: ControlLoop
    state: AppState
    ev: AsyncMock
    victron: MagicMock
    writes: list[tuple[int, float]] = field(default_factory=list)

    @property
    def commanded(self) -> list[float]:
        return [r.commanded_w for r in self.records]

    @property
    def ramp(self) -> list[float]:
        return [r.ramp_w for r in self.records]


def user_state(**overrides) -> AppState:
    """AppState with the user's eco configuration (docker config dump)."""
    defaults = {
        "charge_mode": "Eco",
        "control_loop_interval_s": CYCLE_S,
        "eco_mean_window_minutes": 5,
        "eco_day_ramp_step_w": 40.0,
        "eco_day_grid_export_charge_start_w": -1400.0,
        "eco_day_solar_battery_charge_start_w": 5500.0,
        "eco_day_min_solar_battery_soc_pct": 92.0,
        "eco_day_solar_battery_full_pct": 96.0,
        "eco_day_solar_battery_full_exit_pct": 94.0,
        "eco_day_min_charge_enabled": True,
        "ev_min_soc_pct": 50.0,
        "ev_max_soc_pct": 80.0,
        "solar_battery_day_power_limit_w": -3000.0,
        "solar_battery_max_discharge_w": 15000.0,
        "solar_battery_max_ev_charge_power_w": 4400.0,
        "grid_breaker_limit_a": 32.0,
        "correction_pct": 5.6,
        "ev_connected": True,
        "ev_charger_status_enum": ChargerStatus.CHARGING_IN_PROGRESS,
    }
    defaults.update(overrides)
    return AppState(**defaults)


async def run_scenario(
    monkeypatch,
    *,
    n_cycles: int,
    site: Callable[[int, float], dict],
    car: Car | None = None,
    ev_fields: Callable[[int, AppState], dict] | None = None,
    state: AppState | None = None,
    ev_soc_pct: float | None = None,
    session: ChargeSessionState | None = ChargeSessionState.CHARGING,
    eco_charging: bool = True,
    ramp_w: float = 4400.0,
    on_cycle: Callable[[int, AppState, ControlLoop], None] | None = None,
    cycle_s: float = CYCLE_S,
    night: bool = False,
    soc_republish: Callable[[int], bool] | None = None,
) -> Result:
    """Run ``n_cycles`` iterations of ControlLoop.run_loop and record one CycleRecord per cycle.

    ``car`` models the EV (closed loop); ``ev_fields(i, state)`` instead feeds recorded EV values (open loop).
    ``night`` selects the Eco night (discharge) window instead of Eco day.
    ``soc_republish(i)`` returning False skips the ``ev_soc_pct_updated_at`` re-stamp in cycle ``i`` (a dead
    integration: the value stays but goes stale). The default re-stamps every cycle, like HA republishing.
    ``cycle_s`` is the fake-clock advance per cycle (the real loop period is the interval plus Modbus time).
    """
    clock = FakeClock()
    fake_time = types.SimpleNamespace(monotonic=clock)
    for mod in (mode_strategies, power_utils, state_machine, breaker, loop_module, snapshot):
        monkeypatch.setattr(mod, "_time", fake_time)
    monkeypatch.setattr(mode_strategies, "is_within_discharge_window", lambda s: night)
    monkeypatch.setattr(state_machine, "is_within_discharge_window", lambda s: night)

    state = state or user_state()
    if ev_soc_pct is not None:
        state.ev_soc_pct = ev_soc_pct
        state.ev_soc_pct_updated_at = clock()
    car = car or StepAmpCar()

    written: dict[str, float | None] = {"last": ramp_w, "this_cycle": None}
    car_out = {"amps": 0.0, "power": 0.0, "stopped": False}
    cycle_no = {"i": 0}
    writes: list[tuple[int, float]] = []

    victron = MagicMock()
    victron.connected = True
    victron.ensure_connected = AsyncMock()

    def sample_ev():
        """Sample the car once per cycle, before the site read, so site and car are aligned."""
        i = cycle_no["i"]
        if cycle_no.get("sampled") == i:
            return
        cycle_no["sampled"] = i
        if ev_fields is not None:
            for key, value in ev_fields(i, state).items():
                setattr(state, key, value)
            car_out["power"] = state.ev_active_power_w or 0.0
        else:
            volts = 240.0
            if car_out["stopped"]:
                cal, power, amps = (0.0, 0.0, 0.0), 0.0, 0.0
            else:
                cal, power, amps = car.respond(written["last"], volts, i)
            car_out["amps"], car_out["power"] = amps, power
            state.ev_current_a, state.ev_current_b, state.ev_current_c = cal
            state.ev_current_raw_a, state.ev_current_raw_b, state.ev_current_raw_c = (c / CORRECTION for c in cal)
            state.ev_active_power_w = power
            state.ev_voltage_l1_v = state.ev_voltage_l2_v = state.ev_voltage_l3_v = volts
            state.ev_charger_status_enum = (
                ChargerStatus.IDLE_CONNECTOR_PLUGGED if car_out["stopped"] else ChargerStatus.CHARGING_IN_PROGRESS
            )

    async def victron_read():
        sample_ev()
        for key, value in site(cycle_no["i"], car_out["power"]).items():
            setattr(state, key, value)
        if state.ev_soc_pct is not None and ev_soc_pct is not None:
            if soc_republish is not None and not soc_republish(cycle_no["i"]):
                return
            state.ev_soc_pct_updated_at = clock()

    victron.read = AsyncMock(side_effect=victron_read)

    ev = AsyncMock()
    ev.connected = True
    ev.ensure_connected = AsyncMock()

    async def ev_read():
        sample_ev()
        state.ev_comm_healthy = True
        state.ev_last_read_ok_at = clock()

    ev.read = AsyncMock(side_effect=ev_read)

    async def write_setpoint(power_w):
        written["last"] = power_w
        written["this_cycle"] = power_w
        writes.append((cycle_no["i"], power_w))
        return True

    async def stop_charging():
        car_out["stopped"] = True

    async def start_charging():
        car_out["stopped"] = False

    ev.write_setpoint = AsyncMock(side_effect=write_setpoint)
    ev.stop_charging = AsyncMock(side_effect=stop_charging)
    ev.start_charging = AsyncMock(side_effect=start_charging)

    loop = ControlLoop(state, victron, ev, asyncio.Queue())
    loop._eco_charging = eco_charging
    loop._eco_day_setpoint_w = ramp_w
    if session is not None:
        loop._charging_session_state = session
        loop._session_origin_mode = state.charge_mode
        loop._last_positive_setpoint = ramp_w

    records: list[CycleRecord] = []

    async def fake_sleep(_delay):
        i = cycle_no["i"]
        records.append(
            CycleRecord(
                i=i,
                t=clock(),
                commanded_w=state.commanded_setpoint_w,
                written_w=written["this_cycle"],
                mode_state=loop._charge_mode_state,
                ramp_w=loop._eco_day_setpoint_w,
                battery_power_w=state.solar_battery_power_w,
                car_amps=car_out["amps"],
                ev_power_w=state.ev_active_power_w,
                eco_charging=loop._eco_charging,
                session_state=loop._charging_session_state,
            )
        )
        written["this_cycle"] = None
        if on_cycle is not None:
            on_cycle(i, state, loop)
        cycle_no["i"] += 1
        clock.advance(cycle_s)
        if cycle_no["i"] >= n_cycles:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    try:
        await loop.run_loop()
    except asyncio.CancelledError:
        pass
    return Result(records=records, loop=loop, state=state, ev=ev, victron=victron, writes=writes)
