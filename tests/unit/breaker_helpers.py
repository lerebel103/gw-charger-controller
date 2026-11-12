"""Shared plant/cycle helpers for breaker-protection tests."""

from __future__ import annotations

from app.control.breaker import limit_phase_current, record_phase_samples
from app.state import AppState

V = 233.0
I_BRK = 32.0
I_LIM = 0.8 * I_BRK  # 25.6 A
P_LIM = I_LIM * V  # 5964.8 W: the current limit in watts at 233 V (for setpoint maths)


def make_state(**overrides) -> AppState:
    """AppState with grid/EV voltages populated and a 32 A breaker."""
    defaults = {
        "charge_mode": "Manual",
        "grid_breaker_limit_a": I_BRK,
        "victron_l1_voltage_v": V,
        "victron_l2_voltage_v": V,
        "victron_l3_voltage_v": V,
        "ev_voltage_l1_v": V,
        "ev_voltage_l2_v": V,
        "ev_voltage_l3_v": V,
    }
    defaults.update(overrides)
    return AppState(**defaults)


def set_inputs(
    state: AppState,
    p: tuple[float | None, float | None, float | None],
    i: tuple[float | None, float | None, float | None] | None = None,
    ev_w: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> None:
    """Set SIGNED grid current (+ import, - export) and the EV phase load (as raw current).

    ``p`` is shorthand for the signed grid load in W at unity power factor, converted to current as P / V.
    Pass ``i`` to set the signed currents explicitly (e.g. low power factor). The limiter only reads current.
    """
    if i is None:
        i = tuple(None if x is None else x / V for x in p)  # type: ignore[assignment]
    state.victron_l1_current_a, state.victron_l2_current_a, state.victron_l3_current_a = i
    state.ev_current_raw_a, state.ev_current_raw_b, state.ev_current_raw_c = (w / V for w in ev_w)
    k = 1.0 + state.correction_pct / 100.0  # calibrated currents (Eco follow guard)
    state.ev_current_a, state.ev_current_b, state.ev_current_c = (w / V * k for w in ev_w)


def run_cycle(loop, setpoint: float, t: float) -> float:
    """One control cycle at synthetic time *t*: record samples then limit."""
    record_phase_samples(loop, now=t)
    return limit_phase_current(loop, setpoint, now=t)
