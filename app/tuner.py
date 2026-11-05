"""Design a fan curve from how this server actually behaves.

The first version turned "temperature → speed adaptive mode settled on"
pairs straight into a curve. That can't work: in adaptive mode the
temperature sits near the target and only the speed moves with the load, so
the pairs form a vertical column, and one odd moment (a jump to 100%) was
enough to flatten the whole curve at 88%.

This version learns a model instead, from every steady five-minute period
in the history - fixed-curve periods as well as adaptive ones:

    temperature - inlet temperature = (a + b * power) / (fan% + c)

i.e. heat in (a fixed part plus a part that grows with the server's power
draw) over airflow (fan speed, plus c for the airflow the chassis gets
anyway). Fitted per curve by least squares; it reports its own error.

With that model it *designs* the curve rather than copying data: from the
mildest conditions seen (low load, cool room) to the harshest (peak load
plus 10%, warmest inlet plus 1°C) it picks equilibrium points so the fans
sit at the minimum when it's easy and reach exactly what's needed to hold
the target in the worst case. Above the target it climbs to 100% before the
watchdog's trip point. It also predicts where the new curve will settle at
typical and peak load, so you can judge it before applying it.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .history_db import history_db

STEADY_TEMP_C = 1.5         # max swing of the temperature within a bucket
STEADY_FAN = 3.0            # ... and of the fan speed
MIN_ROWS = 24               # steady 5-minute periods needed (~2 hours)
MIN_FAN_SPREAD = 4.0        # p10..p90 of fan speed: below this airflow can't be learned
MAX_MAE_C = 2.0             # model must predict within this on average
STUCK_SHARE = 0.6           # sensor at one value this often = it's floored
DESIGN_STEPS = 5


def _pct(vals: Sequence[float], q: float) -> float:
    s = sorted(vals)
    return s[min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))]


def _buckets(server: str, kind: str, names: List[str], since: int, until: int, points: int,
             combine=max) -> Dict[int, Tuple[float, float, float]]:
    """{bucket ts: (value, min, max)} - several series combined bucket by bucket."""
    out: Dict[int, List[Tuple[float, float, float]]] = {}
    for n in names:
        for p in history_db.query(server, kind, n, since, until, points):
            out.setdefault(p["ts"], []).append((p["avg"], p["min"], p["max"]))
    res = {}
    for ts, vals in out.items():
        if combine is max:      # the hottest of several sensors
            res[ts] = max(vals, key=lambda v: v[0])
        else:                   # the mean of several fans
            n = len(vals)
            res[ts] = (sum(v[0] for v in vals) / n, min(v[1] for v in vals), max(v[2] for v in vals))
    return res


def fit(rows: List[Tuple[float, float, float]]) -> Optional[Dict[str, float]]:
    """rows: (temp rise over inlet, fan %, power W). Returns a, b, c and the error."""
    best = None
    for c10 in range(5, 801, 5):                       # c from 0.5 to 80 % in 0.5 steps
        c = c10 / 10
        u = [1 / (f + c) for _, f, _ in rows]
        v = [p / (f + c) for _, f, p in rows]
        y = [dt for dt, _, _ in rows]
        suu = sum(x * x for x in u); svv = sum(x * x for x in v); suv = sum(a * b for a, b in zip(u, v))
        suy = sum(a * b for a, b in zip(u, y)); svy = sum(a * b for a, b in zip(v, y))
        det = suu * svv - suv * suv
        a = b = None
        if det > 1e-12:
            a = (suy * svv - svy * suv) / det
            b = (svy * suu - suy * suv) / det
        if a is None or b < 0 or a < 0:                # heat can't go down with power
            a, b = suy / suu, 0.0
        if a <= 0:
            continue
        err = [abs(yi - (a + b * p) / (f + c)) for yi, (_, f, p) in zip(y, rows)]
        mae = sum(err) / len(err)
        if best is None or mae < best["mae"]:
            best = {"a": a, "b": b, "c": c, "mae": mae}
    return best


def _equilibrium(points: List[List[float]], d: float, tin: float, c: float) -> Tuple[float, float]:
    """Where a curve settles: the temperature T with T = tin + d / (curve(T) + c)."""
    from .curve import interpolate
    lo, hi = tin + 0.01, 130.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if tin + d / (interpolate(points, mid) + c) - mid > 0:
            lo = mid
        else:
            hi = mid
    t = (lo + hi) / 2
    return round(t, 1), round(interpolate(points, t), 1)


def suggest(server: str, sensors: List[str], fan_names: List[str], profile_name: str,
            target: Optional[float], limit: Optional[float], margin: float,
            min_floor: int = 0, days: int = 7) -> Dict[str, Any]:
    now = int(time.time())
    # A few minutes inside the window, so "2 days" never lands right on the 48-hour
    # raw-retention edge and flips between storage tiers from one second to the next.
    since = now - max(1, min(30, days)) * 86400 + 300
    pts = max(60, (now - since) // 300)                  # ~5-minute buckets
    temp = _buckets(server, "temp", sensors, since, now, pts)
    inlet_names = [n for n in history_db.names(server).get("temp", []) if "inlet" in n.lower()]
    inlet = _buckets(server, "temp", inlet_names[:1], since, now, pts)
    power = _buckets(server, "power", ["system"], since, now, pts)
    actual = _buckets(server, "fan", fan_names, since, now, pts, combine=None)
    ceiling = _buckets(server, "target", [profile_name], since, now, pts)

    rows, samples, stuck_vals = [], [], []
    for ts, (t, tmin, tmax) in temp.items():
        fan = actual.get(ts) or ceiling.get(ts)          # real speed when we have it
        if not fan or ts not in inlet:
            continue
        stuck_vals.append(round(t))
        if tmax - tmin > STEADY_TEMP_C or fan[2] - fan[1] > STEADY_FAN:
            continue                                     # not at equilibrium
        p = power.get(ts, (0.0, 0, 0))[0]
        rise = t - inlet[ts][0]
        if rise <= 0.5:
            continue
        rows.append((rise, fan[0], p, inlet[ts][0], t))
        samples.append({"t": round(t, 1), "p": round(fan[0], 1)})

    hours = round(len(rows) * (now - since) / pts / 3600, 1)
    out: Dict[str, Any] = {"pairs": len(rows), "hours": hours,
                           "samples": samples[:: max(1, len(samples) // 400)], "notes": []}
    if stuck_vals:
        top = max(set(stuck_vals), key=stuck_vals.count)
        if stuck_vals.count(top) / len(stuck_vals) >= STUCK_SHARE and \
                _pct(stuck_vals, .9) - _pct(stuck_vals, .1) <= 2:
            out["error"] = (f"This sensor sits at {top}°C almost all the time - iLO 4 reports CPUs as "
                            f"{top}° whenever they're cooler than that - so there's nothing to learn "
                            f"from. Keep your current curve; it only matters under real load.")
            return out
    if len(rows) < MIN_ROWS:
        out["error"] = (f"Not enough steady data yet ({len(rows)} of {MIN_ROWS} five-minute periods "
                        f"where temperature and fan speed both held still).")
        return out
    fans = [r[1] for r in rows]
    if _pct(fans, .9) - _pct(fans, .1) < MIN_FAN_SPREAD:
        out["error"] = (f"The fan speed has hardly changed ({_pct(fans, .1):.0f}–{_pct(fans, .9):.0f}%), "
                        f"so there's no way to tell how much difference airflow makes. Press Learn "
                        f"on this curve (about 30 minutes), then try again.")
        return out

    m = fit([(r[0], r[1], r[2]) for r in rows])
    if not m or m["mae"] > MAX_MAE_C:
        out["error"] = (f"The temperature doesn't follow fan speed and load closely enough to predict "
                        f"(error {m['mae']:.1f}°C)." if m else "Couldn't fit a model to this data.")
        return out
    a, b, c = m["a"], m["b"], m["c"]
    out["model"] = {"a": round(a, 1), "b": round(b, 3), "c": round(c, 1), "error_c": round(m["mae"], 2)}

    trip = (limit - margin) if limit else None
    if target is None:
        target = (trip - 5) if trip else _pct([r[4] for r in rows], .95) + 2
    if trip is not None:
        target = min(target, trip - 3)
    target = float(target)

    P = [r[2] for r in rows]; TIN = [r[3] for r in rows]
    mild = (a + b * _pct(P, .05), _pct(TIN, .05))
    harsh = ((a + b * max(P)) * 1.10, max(TIN) + 1.0)
    floor = float(max(min_floor, 1))
    fan_needed = harsh[0] / (target - harsh[1]) - c if target > harsh[1] else 999
    if fan_needed > 100:
        out["error"] = (f"Even 100% can't hold {target:.0f}°C at peak load in a warm room "
                        f"(the model says it needs ~{fan_needed:.0f}%). Pick a higher target.")
        return out

    t_mild = mild[1] + mild[0] / (floor + c)             # minimum speed, easy conditions
    if t_mild > target - 4:
        t_mild = target - 4
    curve: List[List[int]] = []
    for i in range(DESIGN_STEPS + 1):
        s = i / DESIGN_STEPS
        d = mild[0] + s * (harsh[0] - mild[0])
        tin = mild[1] + s * (harsh[1] - mild[1])
        t_eq = t_mild + s * (target - t_mild)
        f = d / (t_eq - tin) - c if t_eq > tin else 100
        curve.append([int(round(t_eq)), int(round(max(floor, min(100.0, f))))])
    curve.insert(0, [max(0, curve[0][0] - 8), curve[0][1]])
    top_t = trip - 1 if trip else target + 10
    last_f = curve[-1][1]
    curve.append([int(round((target + top_t) / 2)), int(round(last_f + (100 - last_f) * 0.45))])
    curve.append([int(round(top_t)), 100])

    clean: List[List[int]] = []
    for t, f in curve:                                   # strictly rising temps, never-falling speed
        t = max(0, min(120, t)); f = max(int(floor), min(100, f))
        if clean:
            f = max(f, clean[-1][1])
            if t <= clean[-1][0]:
                clean[-1][1] = max(clean[-1][1], f)
                continue
        clean.append([t, f])

    typ = (a + b * _pct(P, .5), _pct(TIN, .5))
    peak = (a + b * max(P), max(TIN))
    ty_t, ty_f = _equilibrium(clean, typ[0], typ[1], c)
    pk_t, pk_f = _equilibrium(clean, peak[0], peak[1], c)
    out.update(points=clean, target=round(target, 1), trip=round(trip, 1) if trip else None,
               observed=[round(min(r[4] for r in rows)), round(max(r[4] for r in rows))],
               predict={"typical": {"power": round(_pct(P, .5)), "inlet": round(typ[1], 1), "temp": ty_t, "fan": ty_f},
                        "peak": {"power": round(max(P)), "inlet": round(peak[1], 1), "temp": pk_t, "fan": pk_f}})
    if pk_t > target + 0.5:
        out["notes"].append(f"At peak load it would settle at {pk_t}°C, a little over the target.")
    return out
