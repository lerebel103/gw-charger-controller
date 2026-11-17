"""Outage simulation for the EV SOC estimator, driven by the history-22 recording.

Drives the REAL ``SocEstimator``, ``resolve_ev_soc`` and ``max_soc_stop_reason`` with a fake clock, mirroring
``ControlLoop._update_soc_estimator``. Recorded: reported SOC steps, session energy, session boundaries.
SIMULATED: the outage (``freeze``: the reported value stops changing while HA keeps republishing it, ``stale``: no
messages at all, so the value is stale after 300 s), the 10 s cycle, the limit, the pre-learned k, and a tail that
extends each session past its last recorded step (constant power, constant kWh per SOC percent, 0.5 pp steps).
"True SOC" is the reported SOC linearly interpolated between step times (it ignores the 0.5 pp quantisation).
See tests/fixtures/incidents/2026-11-01-ev-soc-history-22/README.md.
"""

from __future__ import annotations

import bisect
import csv
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.control import power_utils
from app.control.power_utils import ev_soc_is_fresh, max_soc_stop_reason, resolve_ev_soc
from app.control.soc_estimator import SocEstimator, effective_k
from app.state import AppState

HISTORY22_CSV = (
    Path(__file__).parent.parent / "fixtures" / "incidents" / "2026-11-01-ev-soc-history-22" / "ev_soc_energy.csv"
)
CAPACITY_KWH = 82.0
CYCLE_S = 10.0
_TAIL_TO_PCT = 90.0
_SOC = "sensor.ev_charger_ev_soc"
_ENERGY = "sensor.ev_charger_session_energy"

Points = list[tuple[float, float]]


class FakeClock:
    """Replaces ``power_utils._time`` (monotonic only)."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


CLOCK = FakeClock()


def install_clock(monkeypatch) -> None:
    monkeypatch.setattr(power_utils, "_time", CLOCK)


class Step:
    """Zero-order hold of ``(t, value)`` points."""

    def __init__(self, points: Points) -> None:
        self.t = [p[0] for p in points]
        self.v = [p[1] for p in points]

    def at(self, t: float) -> float | None:
        i = bisect.bisect_right(self.t, t) - 1
        return self.v[i] if i >= 0 else None


def _epoch(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC).timestamp()


def load_history22() -> tuple[Points, Points]:
    """Numeric SOC and session-energy (Wh) points of the fixture (non-numeric states are skipped)."""
    soc: Points = []
    energy: Points = []
    with HISTORY22_CSV.open(newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                value = float(row["state"])
            except ValueError:
                continue
            if row["entity_id"] == _SOC:
                soc.append((_epoch(row["last_changed"]), value))
            elif row["entity_id"] == _ENERGY:
                energy.append((_epoch(row["last_changed"]), value))
    return soc, energy


@dataclass
class Session:
    """One energy-register session: starts at a reset (value drops), ends at the last energy change."""

    start_t: float
    end_t: float
    end_wh: float


def segment_sessions(energy: Points) -> list[Session]:
    sessions: list[Session] = []
    cur: list[tuple[float, float]] | None = None
    for i in range(1, len(energy)):
        t, v = energy[i]
        if v < energy[i - 1][1]:
            if cur:
                sessions.append(_close(cur))
            cur = [(t, v)]
        elif cur is not None:
            cur.append((t, v))
    if cur:
        sessions.append(_close(cur))
    return sessions


def _close(points: list[tuple[float, float]]) -> Session:
    changes = [points[0]] + [p for p, q in zip(points[1:], points[:-1], strict=True) if p[1] != q[1]]
    return Session(start_t=points[0][0], end_t=changes[-1][0], end_wh=changes[-1][1])


def _ols(x: list[float], y: list[float]) -> float:
    n = len(x)
    mx, my = sum(x) / n, sum(y) / n
    return sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True)) / sum((a - mx) ** 2 for a in x)


_DATA: tuple[Points, Points, list[Session]] | None = None


def history22() -> tuple[Points, Points, list[Session]]:
    global _DATA
    if _DATA is None:
        soc, energy = load_history22()
        _DATA = (soc, energy, segment_sessions(energy))
    return _DATA


class Timeline:
    """Recorded session plus an extrapolated tail past the last recorded SOC step."""

    def __init__(self, session: Session) -> None:
        soc, energy, _ = history22()
        self.soc_step, self.en_step = Step(soc), Step(energy)
        self.start, self.end = session.start_t, session.end_t
        changes = [(t, v) for t, v in soc if self.start <= t <= self.end + 240]
        self.rep_t = [t for t, _ in changes]
        self.rep_v = [v for _, v in changes]
        e_at = [self.en_step.at(t) / 1000.0 for t in self.rep_t]
        self.slope = _ols(self.rep_v, e_at) if len(set(self.rep_v)) > 1 else 0.85  # kWh per SOC pp
        t_a = self.start + 0.6 * (self.end - self.start)
        self.power_w = (self.en_step.at(self.end) - self.en_step.at(t_a)) / (self.end - t_a) * 3600
        wh_per_s = self.power_w / 3600
        e_end, t_end = self.en_step.at(self.end), self.end
        self._ext_en_t: list[float] = []
        self._ext_en_v: list[float] = []
        n = 1
        while True:
            tt = t_end + 100 * n / wh_per_s
            self._ext_en_t.append(tt)
            self._ext_en_v.append(e_end + 100 * n)
            n += 1
            if tt > t_end + 6 * 3600:
                break
        x, e = self.rep_v[-1], e_at[-1] * 1000.0
        ext_t: list[float] = []
        ext_v: list[float] = []
        while x < _TAIL_TO_PCT:
            x += 0.5
            e += 0.5 * self.slope * 1000
            ext_t.append(self.end + (e - e_end) / wh_per_s if e > e_end else self.end)
            ext_v.append(x)
        self.all_t = self.rep_t + ext_t
        self.all_v = self.rep_v + ext_v

    def energy(self, t: float) -> float:
        if t <= self.end:
            return self.en_step.at(t)
        i = bisect.bisect_right(self._ext_en_t, t) - 1
        return self._ext_en_v[i] if i >= 0 else self.en_step.at(self.end)

    def reported(self, t: float) -> float:
        i = bisect.bisect_right(self.all_t, t) - 1
        return self.all_v[i] if i >= 0 else self.soc_step.at(t)

    def true(self, t: float) -> float:
        ts, vs = self.all_t, self.all_v
        if t <= ts[0]:
            return vs[0] - (ts[0] - t) * (vs[1] - vs[0]) / (ts[1] - ts[0])
        if t >= ts[-1]:
            return vs[-1]
        i = bisect.bisect_right(ts, t) - 1
        return vs[i] + (vs[i + 1] - vs[i]) * (t - ts[i]) / (ts[i + 1] - ts[i])

    def last_change_before(self, t: float) -> float:
        return self.all_t[bisect.bisect_right(self.all_t, t) - 1]

    def outage_starts(self, *, step_every: int = 1, first_fraction: float = 1 / 3) -> list[float]:
        """Every genuine SOC change (+1 s) and the middle of each step, from ``first_fraction`` of the session
        on, up to the last recorded change. ``step_every`` subsamples the changes."""
        t0 = self.start + first_fraction * (self.end - self.start)
        genuine = [t for t in self.rep_t if t >= t0]
        points: list[float] = []
        for a, b in zip(genuine[::step_every], genuine[1::step_every], strict=False):
            points += [a + 1.0, (a + b) / 2]
        return points


@dataclass
class Stub:
    """Minimal stand-in for ControlLoop (what resolve_ev_soc and max_soc_stop_reason read)."""

    _state: AppState
    _soc_estimator: SocEstimator = field(default_factory=SocEstimator)

    @classmethod
    def make(cls, limit: float, k0: float | None) -> Stub:
        state = AppState(ev_battery_capacity_kwh=CAPACITY_KWH, ev_max_soc_pct=limit, ev_connected=False)
        if k0 is not None:
            state.ev_soc_k = k0
            state.ev_soc_k_capacity_kwh = CAPACITY_KWH
        return cls(state)

    def update(self) -> None:
        st = self._state
        result = self._soc_estimator.observe(
            soc_pct=st.ev_soc_pct,
            soc_fresh=ev_soc_is_fresh(self),
            energy_wh=st.ev_session_energy_wh,
            capacity_kwh=CAPACITY_KWH,
            k=effective_k(st),
        )
        if result is not None:
            st.ev_soc_k = result.k_new
            st.ev_soc_k_capacity_kwh = CAPACITY_KWH


@dataclass
class Outcome:
    stop_t: float | None = None
    reason: str | None = None
    source: str | None = None
    true_soc: float | None = None
    first_stall_t: float | None = None
    k_final: float | None = None
    trace: list[tuple[float, float, float | None, bool]] = field(default_factory=list)  # t, reported, est, stalled


def run(
    tl: Timeline,
    limit: float,
    k0: float | None,
    mode: str = "healthy",
    outage_t: float | None = None,
    *,
    cold: bool = False,
    until: float | None = None,
) -> Outcome:
    """Replay one session. ``mode``: healthy | freeze | stale. ``cold``: the app starts with the car already
    connected (no SOC seen before the plug-in, so the anchor is REPORTED, not LAST_KNOWN)."""
    stub = Stub.make(limit, k0)
    st = stub._state
    t = (tl.start + 10) if cold else (tl.start - 180)
    connected_at = (tl.start + 10) if cold else (tl.start - 90)
    end_t = until if until is not None else tl.all_t[-1] + 60
    frozen = tl.reported(outage_t) if outage_t is not None else None
    out = Outcome()
    while t <= end_t:
        CLOCK.now = t
        outage = mode != "healthy" and outage_t is not None and t >= outage_t
        rep = frozen if outage else (tl.reported(t) if t >= tl.start else tl.soc_step.at(t))
        st.ev_soc_pct = rep
        if not (mode == "stale" and outage):
            st.ev_soc_pct_updated_at = t
        st.ev_session_energy_wh = tl.energy(t)
        if t >= connected_at and not st.ev_connected:
            st.ev_connected = True
            stub._soc_estimator.begin_session()
        stub.update()
        res = resolve_ev_soc(stub)
        if res.stalled and out.first_stall_t is None and (outage_t is None or t >= outage_t):
            out.first_stall_t = t
        if t >= tl.start:
            out.trace.append((t, rep, res.estimated_pct, res.stalled))
        reason = max_soc_stop_reason(stub) if st.ev_connected else None
        if reason:
            out.stop_t, out.reason, out.source = t, reason, res.source.value
            out.true_soc = tl.true(t)
            out.k_final = effective_k(st)
            return out
        t += CYCLE_S
    out.k_final = effective_k(st)
    return out
