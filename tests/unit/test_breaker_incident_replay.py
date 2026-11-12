"""Replay of the 2026-11-07 breaker-cap incident (see tests/fixtures/incidents/2026-11-07-breaker-cap/README.md).

A single-phase car on L1 charged in Eco-day while PV was balanced across phases. L1 imported only
~1.3-4.4 A (limit 25.6 A) yet the old limiter capped the TOTAL setpoint at ~7.45 kW (a single-phase
current limit) and the car, receiving a third of that, locked at ~2.3 kW.

The HA export holds the EM540's UNSIGNED currents; the Victron proxies SIGNED currents, so the replay
reconstructs them as copysign(I, P) per phase (sign from the same phase's real power).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.control import breaker
from app.control.breaker import limit_phase_current, mean_phase_import_a, record_phase_samples
from app.control.constants import _ECO_DAY_RAMP_STEP_W, _MAX_CHARGE_W, _MIN_CHARGE_W
from app.log_throttle import LogThrottle
from tests.unit.breaker_helpers import I_LIM, make_state
from tests.unit.helpers import make_ns_loop
from tests.unit.incident_data import load_ha_history, resample_cycles, signed_current

_P = "sensor.em540_energy_meter_bridge_power_l{n}"
_I = "sensor.em540_energy_meter_bridge_current_l{n}"
_EV = "sensor.ev_charger_ev_current_l{n}"
_START = datetime(2026, 11, 7, 6, 32, 10, tzinfo=UTC)
_END = datetime(2026, 11, 7, 6, 59, 50, tzinfo=UTC)


@pytest.fixture(scope="module")
def cycles():
    series = load_ha_history()
    wanted = {k: v for k, v in series.items() if k.startswith(("sensor.em540", "sensor.ev_charger_ev_current"))}
    return resample_cycles(wanted, _START, _END, step_s=10.0)


@pytest.fixture(autouse=True)
def _fresh_throttle(monkeypatch):
    monkeypatch.setattr(breaker, "_throttle", LogThrottle(breaker.logger))


def test_incident_replay_never_engages_cap(cycles):
    loop = make_ns_loop(make_state(charge_mode="Eco"))
    state = loop._state
    requested = _MIN_CHARGE_W
    max_mean_l1 = 0.0
    max_raw_l1 = 0.0
    assert len(cycles) > 150
    for c in cycles:
        t = c["t"]
        state.victron_l1_current_a, state.victron_l2_current_a, state.victron_l3_current_a = (
            signed_current(c[_I.format(n=n)], c[_P.format(n=n)]) for n in (1, 2, 3)
        )
        state.ev_current_raw_a, state.ev_current_raw_b, state.ev_current_raw_c = (
            c[_EV.format(n=1)],
            c[_EV.format(n=2)],
            c[_EV.format(n=3)],
        )
        assert None not in (state.victron_l1_current_a, state.ev_current_raw_a)

        record_phase_samples(loop, now=t)
        out = limit_phase_current(loop, requested, now=t)

        assert out == requested, f"cap engaged at t={t}: {out} vs {requested}"
        assert loop._breaker.cap_w is None
        assert loop._breaker.missing_cycles == 0  # nothing is missing
        assert not loop._breaker.stopped
        max_mean_l1 = max(max_mean_l1, mean_phase_import_a(loop, 1, now=t))
        max_raw_l1 = max(max_raw_l1, state.victron_l1_current_a)
        requested = min(requested + _ECO_DAY_RAMP_STEP_W, _MAX_CHARGE_W)

    # Documents the incident: breaker was nowhere near saturation
    assert requested == _MAX_CHARGE_W
    assert max_mean_l1 < 0.25 * I_LIM
    assert max_raw_l1 < 0.9 * 32 * 0.25
    assert max_raw_l1 > 3.9  # data really contains the ~1 kW (about +4.1 A) L1 import peak


def test_incident_data_has_unsigned_currents_and_signed_power(cycles):
    """Guards the data-model assumption: the HA export shows positive current even while exporting."""
    exporting = [c for c in cycles if c[_P.format(n=2)] is not None and c[_P.format(n=2)] < -500]
    assert exporting
    assert all(c[_I.format(n=2)] > 0 for c in exporting)


def test_signed_current_reconstruction_takes_sign_from_power(cycles):
    exporting = [c for c in cycles if c[_P.format(n=2)] is not None and c[_P.format(n=2)] < -500]
    assert exporting
    assert all(signed_current(c[_I.format(n=2)], c[_P.format(n=2)]) < 0 for c in exporting)
