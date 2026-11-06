"""Learn how each curve's part heats up, and design curves from it.

The model, per curve, from every steady five-minute period in the history
(fixed-curve, adaptive and Learn periods alike):

    temperature - inlet temperature = (a + b * power) / (fan% + c)

heat in (a fixed part plus a part that grows with the server's power draw)
over airflow (fan speed, plus c for the airflow the chassis gets anyway).

Only the fans that actually follow the curve count, every 5% band of fan
speed is weighted equally (so a 30-minute Learn run isn't drowned by weeks at
one speed), and the accuracy is checked honestly: every fifth hour is held
back and the model is graded on it. It is refitted in the
background every 20 minutes from up to 30 days of history (learner.py), and
adaptive mode uses it to go straight to the speed it predicts it needs.

From the model a curve is *designed*: minimum speed when conditions are
easy, just enough to hold the target at the harshest load and room
temperature seen (+10% / +1°C), then a climb to 100% before the watchdog
trip point - with predictions of where it will settle.

The first version copied "temperature -> speed adaptive mode settled on"
pairs into a curve; in adaptive mode the temperature sits near the target
and only the speed moves, so that produced a column, and one odd moment
flattened a whole suggestion at 88%. Hence all of the above.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .history_db import history_db

STEADY_TEMP_C = 1.5         # max swing of the temperature within a bucket
STEADY_FAN = 3.0            # ... and of the fan speed
MIN_ROWS = 24               # steady 5-minute periods needed (~2 hours)
MIN_FAN_SPREAD = 4.0        # fan speeds seen (each held >= 10 min) must span this much
FAN_BIN = 5.0               # rows are weighted so every 5% band of fan speed counts equally
MOVING_FAN = 3.0            # a fan whose speed never moves more than this isn't one we drive
GOOD_C = 1.5                # tested error for a model adaptive mode may lean on
MAX_ERR_C = 2.5             # beyond this it isn't used at all
STUCK_SHARE = 0.6           # sensor at one value this often = it's floored
DESIGN_STEPS = 5


def pick_inlet(names: List[str]) -> Optional[str]:
    """The chassis air-intake sensor: "Inlet Ambient", not a power supply's inlet."""
    inl = [n for n in names if "inlet" in n.lower()]
    return next((n for n in inl if "ambient" in n.lower()), inl[0] if inl else None)


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


def _driven_fans(server: str, fan_names: List[str], since: int, until: int,
                 points: int) -> Dict[int, Tuple[float, float, float]]:
    """The curve's real fan speed, bucket by bucket: (value, lo, hi).

    Only fans that actually follow the curve count. Some fans in a group sit
    at a floor set elsewhere (on a DL380p "Fan 5" stays at 5% while "Fan 6"
    follows the HD controller curve): averaging them in blurs the speed, and
    the gap between them made every bucket look unsteady. lo/hi is each fan's
    own swing within the bucket, not the gap between fans.
    """
    series: Dict[str, Dict[int, Tuple[float, float, float]]] = {}
    for n in fan_names:
        got = history_db.query(server, "fan", n, since, until, points)
        if got:
            series[n] = {p["ts"]: (p["avg"], p["min"], p["max"]) for p in got}
    if not series:
        return {}

    def spread(s: Dict[int, Tuple[float, float, float]]) -> float:
        v = [x[0] for x in s.values()]
        return _pct(v, .98) - _pct(v, .02)

    keep = [n for n, s in series.items() if spread(s) >= MOVING_FAN] or list(series)
    out: Dict[int, Tuple[float, float, float]] = {}
    for ts in set().union(*(series[n].keys() for n in keep)):
        vals = [series[n][ts] for n in keep if ts in series[n]]
        avg = sum(v[0] for v in vals) / len(vals)
        swing = max(v[2] - v[1] for v in vals)
        out[ts] = (avg, avg - swing / 2, avg + swing / 2)
    return out


def _weights(fans: List[float]) -> List[float]:
    """Every 5% band of fan speed counts about equally (a band needs 3 rows to count fully)."""
    count: Dict[int, int] = {}
    for f in fans:
        count[int(f // FAN_BIN)] = count.get(int(f // FAN_BIN), 0) + 1
    return [1.0 / max(3, count[int(f // FAN_BIN)]) for f in fans]


def _fan_levels(fans: List[float]) -> List[float]:
    """Median speed of each 5% band held for at least 10 minutes (2 rows)."""
    bands: Dict[int, List[float]] = {}
    for f in fans:
        bands.setdefault(int(f // FAN_BIN), []).append(f)
    return sorted(_pct(v, .5) for v in bands.values() if len(v) >= 2)


# ---------------------------------------------------------------------- #
# Data
# ---------------------------------------------------------------------- #
def _effective_fans(server: str, airflow: Dict[str, Any], since: int, until: int,
                    points: int) -> Dict[int, Tuple[float, float, float, float]]:
    """Effective airflow per bucket, from a fan map: (sum of w*speed over every fan
    that cools this part, lo, hi, the part of it from other curves' fans)."""
    own = {str(f) for f in airflow["own"]}
    series = {}
    for k, w in airflow["w"].items():
        name = airflow["names"].get(str(k))
        got = history_db.query(server, "fan", name, since, until, points) if name and w > 0 else []
        if not got:
            return {}                    # a fan without history: can't weigh it in
        series[k] = {p["ts"]: (p["avg"], p["min"], p["max"]) for p in got}
    if not series:
        return {}
    out = {}
    for ts in set.intersection(*(set(s) for s in series.values())):
        eff = sum(airflow["w"][k] * series[k][ts][0] for k in series)
        swing = sum(airflow["w"][k] * (series[k][ts][2] - series[k][ts][1]) for k in series)
        oth = sum(airflow["w"][k] * series[k][ts][0] for k in series if k not in own)
        out[ts] = (eff, eff - swing / 2, eff + swing / 2, oth)
    return out


def collect(server: str, sensors: List[str], fan_names: List[str], profile_name: str,
            days: int = 30, airflow: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Steady periods as rows: ts, temp, rise over inlet, fan %, power, inlet.

    With `airflow` (from a fan map: {"names": {idx: fan name}, "w": {idx: weight},
    "own": [this curve's fan idx]}) "fan" is the effective airflow - every fan
    weighted by how much it cools this part - and "oth" the share of it from
    fans this curve doesn't drive."""
    now = int(time.time())
    # A few minutes inside the window, so it never lands right on the raw-retention
    # edge and flips between storage tiers from one second to the next.
    since = now - max(1, min(30, days)) * 86400 + 300
    pts = max(60, (now - since) // 300)                  # ~5-minute buckets
    temp = _buckets(server, "temp", sensors, since, now, pts)
    inlet_name = pick_inlet(history_db.names(server).get("temp", []))
    inlet = _buckets(server, "temp", [inlet_name] if inlet_name else [], since, now, pts)
    power = _buckets(server, "power", ["system"], since, now, pts)
    actual = _effective_fans(server, airflow, since, now, pts) if airflow else {}
    if not actual:
        airflow = None
        actual = _driven_fans(server, fan_names, since, now, pts)
    ceiling = _buckets(server, "target", [profile_name], since, now, pts) if not airflow else {}

    rows, values = [], []
    for ts in sorted(temp):
        t, tmin, tmax = temp[ts]
        fan = actual.get(ts) or ceiling.get(ts)          # the real speed when we have it
        if not fan or ts not in inlet:
            continue
        values.append(round(t))
        if tmax - tmin > STEADY_TEMP_C or fan[2] - fan[1] > STEADY_FAN:
            continue                                     # not at equilibrium
        rise = t - inlet[ts][0]
        if rise <= 0.5:
            continue
        row = {"ts": ts, "t": t, "rise": rise, "fan": fan[0],
               "p": power.get(ts, (0.0, 0, 0))[0], "tin": inlet[ts][0]}
        if airflow:
            row["oth"] = fan[3]
        rows.append(row)
    stuck = None
    if values:
        top = max(set(values), key=values.count)
        if values.count(top) / len(values) >= STUCK_SHARE and _pct(values, .9) - _pct(values, .1) <= 2:
            stuck = top
    out = {"rows": rows, "stuck": stuck, "bucket_s": (now - since) / pts}
    if airflow:
        out["airflow"] = airflow
    return out


# ---------------------------------------------------------------------- #
# Model
# ---------------------------------------------------------------------- #
def fit(rows: List[Tuple[float, float, float]],
        weights: Optional[List[float]] = None) -> Optional[Dict[str, float]]:
    """rows: (temp rise over inlet, fan %, power W). Returns a, b, c and the error.

    weights make rare fan speeds (a Learn run) count as much as the weeks
    spent at one speed; the error returned is weighted the same way."""
    w = weights or [1.0] * len(rows)
    scale = len(rows) / sum(w)                         # keep sums at their usual size
    w = [k * scale for k in w]
    y = [dt for dt, _, _ in rows]

    def at(c: float) -> Optional[Dict[str, float]]:
        u = [1 / (f + c) for _, f, _ in rows]
        v = [p / (f + c) for _, f, p in rows]
        suu = sum(k * x * x for k, x in zip(w, u)); svv = sum(k * x * x for k, x in zip(w, v))
        suv = sum(k * a * b for k, a, b in zip(w, u, v))
        suy = sum(k * a * b for k, a, b in zip(w, u, y)); svy = sum(k * a * b for k, a, b in zip(w, v, y))
        det = suu * svv - suv * suv
        a = b = None
        if det > 1e-12:
            a = (suy * svv - svy * suv) / det
            b = (svy * suu - suy * suv) / det
        if a is None or b < 0 or a < 0:                # heat can't go down with power
            a, b = suy / suu, 0.0
        if a <= 0:
            return None
        mae = sum(k * abs(yi - (a + b * p) / (f + c)) for k, yi, (_, f, p) in zip(w, y, rows)) / len(rows)
        return {"a": a, "b": b, "c": c, "mae": mae}

    def better(x, y_):
        return y_ if (x is None or (y_ is not None and y_["mae"] < x["mae"])) else x

    # c (the airflow the chassis gets with the fan at 0%) from 0.5 to 200: coarse, then fine.
    best = None
    for c2 in range(1, 401, 4):
        best = better(best, at(c2 / 2))
    if best:
        c0 = best["c"]
        for d4 in range(-8, 9):
            c = c0 + d4 / 4
            if c >= 0.5:
                best = better(best, at(c))
    return best


def _rise(m: Dict[str, float], power: float, fan: float) -> float:
    return (m["a"] + m["b"] * power) / (fan + m["c"])


def predict_temp(m: Dict[str, float], power: float, inlet: float, fan: float, bias: float = 0.0) -> float:
    return inlet + _rise(m, power, fan) + bias


def needed_fan(m: Dict[str, float], power: float, inlet: float, temp: float, bias: float = 0.0) -> float:
    """Fan % the model says holds `temp` (999 when it can't be done)."""
    room = temp - inlet - bias
    return (m["a"] + m["b"] * power) / room - m["c"] if room > 0.5 else 999.0


def learn(data: Dict[str, Any]) -> Dict[str, Any]:
    """A model, or {"error": why not} - in plain words."""
    rows = data["rows"]
    if data.get("stuck") is not None:
        top = data["stuck"]
        return {"error": f"This sensor sits at {top}°C almost all the time - iLO 4 reports CPUs as {top}° "
                         f"whenever they're cooler than that - so there's nothing to learn from yet. It "
                         f"will learn by itself once the server sees real load.", "kind": "stuck"}
    if len(rows) < MIN_ROWS:
        return {"error": f"Not enough steady data yet ({len(rows)} of {MIN_ROWS} five-minute periods where "
                         f"temperature and fan speed both held still).", "kind": "short"}
    fans = [r["fan"] for r in rows]
    levels = _fan_levels(fans)
    if not levels or levels[-1] - levels[0] < MIN_FAN_SPREAD:
        lo, hi = (levels[0], levels[-1]) if levels else (min(fans), max(fans))
        return {"error": f"The fan speed has hardly changed ({lo:.0f}–{hi:.0f}%), so "
                         f"there's no way to tell how much difference airflow makes. Press Learn on this curve "
                         f"(about 30 minutes) - or let adaptive mode move the fans for a while.", "kind": "flat"}
    as_fit = lambda rs: [(r["rise"], r["fan"], r["p"]) for r in rs]
    wts = _weights(fans)
    m = fit(as_fit(rows), wts)
    if not m:
        return {"error": "Couldn't fit a model to this data.", "kind": "bad"}
    # Honest grade on data it hasn't seen: every fifth hour is held back, so the
    # test covers old and new data and every fan speed (a Learn run is recent and
    # short - "the newest 20%" would have been nothing but Learn, never trained on).
    tested = m["mae"]
    if len(rows) >= 40:
        hold = [(r["ts"] // 3600) % 5 == 0 for r in rows]
        tr = [i for i, h in enumerate(hold) if not h]
        te = [i for i, h in enumerate(hold) if h]
        if len(te) >= 5 and len(tr) >= MIN_ROWS:
            train = fit(as_fit([rows[i] for i in tr]), _weights([fans[i] for i in tr]))
            if train:
                tw = _weights([fans[i] for i in te])
                tested = sum(k * abs(rows[i]["rise"] - _rise(train, rows[i]["p"], rows[i]["fan"]))
                             for k, i in zip(tw, te)) / sum(tw)
    if tested > MAX_ERR_C:
        return {"error": f"The temperature doesn't follow fan speed and load closely enough to predict "
                         f"(it missed recent data by {tested:.1f}°C on average).", "kind": "bad"}
    P = [r["p"] for r in rows]; TIN = [r["tin"] for r in rows]
    a, b, c = m["a"], m["b"], m["c"]
    eff = None
    seen = levels
    af = data.get("airflow")
    if af:
        # Fitted on effective airflow; everything downstream (designer, adaptive,
        # predictions) thinks in this curve's own fan speed, so convert, with the
        # other fans at their typical contribution. The controller redoes this
        # live with the other fans' real speeds (curve._with_live_airflow).
        s = sum(af["w"].get(str(k), 0.0) for k in af["own"])
        oth = _pct([r["oth"] for r in rows], .5)
        eff = {"a": round(a, 3), "b": round(b, 5), "c": round(c, 2), "s": round(s, 4),
               "w": af["w"], "own": [int(k) for k in af["own"]], "others_typical": round(oth, 2)}
        sc = max(s, 0.05)
        a, b, c = a / sc, b / sc, (c + oth) / sc
        seen = _fan_levels([(r["fan"] - r["oth"]) / sc for r in rows]) or levels
    out = {"a": round(a, 3), "b": round(b, 5), "c": round(c, 2),
            "error_c": round(m["mae"], 2), "tested_c": round(tested, 2),
            "quality": "good" if tested <= GOOD_C else "rough",
            "n": len(rows), "hours": round(len(rows) * data["bucket_s"] / 3600, 1),
            "fan_seen": [round(seen[0]), round(seen[-1])],
            "power": [round(_pct(P, .05)), round(_pct(P, .5)), round(max(P))],
            "inlet": [round(_pct(TIN, .05), 1), round(_pct(TIN, .5), 1), round(max(TIN), 1)],
            "temp_seen": [round(min(r["t"] for r in rows)), round(max(r["t"] for r in rows))],
            "fitted_at": int(time.time())}
    if eff:
        out["eff"] = eff
    return out


# ---------------------------------------------------------------------- #
# Design
# ---------------------------------------------------------------------- #
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


def settle(m: Dict[str, Any], points: List[List[float]], power: float, inlet: float) -> Tuple[float, float]:
    """Where `points` would settle at this load and room temperature, per the model."""
    return _equilibrium(points, m["a"] + m["b"] * power, inlet, m["c"])


def design(m: Dict[str, Any], target: Optional[float], limit: Optional[float], margin: float,
           floor_pct: int) -> Dict[str, Any]:
    a, b, c = m["a"], m["b"], m["c"]
    trip = (limit - margin) if limit else None
    if target is None:
        target = (trip - 5) if trip else m["temp_seen"][1] + 2
    if trip is not None:
        target = min(target, trip - 3)
    target = float(target)
    p_lo, p_mid, p_hi = m["power"]; t_lo, t_mid, t_hi = m["inlet"]
    mild = (a + b * p_lo, t_lo)
    harsh = ((a + b * p_hi) * 1.10, t_hi + 1.0)
    floor = float(max(floor_pct, 1))
    need = harsh[0] / (target - harsh[1]) - c if target > harsh[1] else 999
    if need > 100:
        return {"error": f"Even 100% can't hold {target:.0f}°C at peak load in a warm room (the model says it "
                         f"needs ~{need:.0f}%). Pick a higher target.", "target": round(target, 1)}
    t_mild = min(mild[1] + mild[0] / (floor + c), target - 4)   # minimum speed, easy conditions
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
    ty_t, ty_f = settle(m, clean, p_mid, t_mid)
    pk_t, pk_f = settle(m, clean, p_hi, t_hi)
    hot_in = t_hi + 5                                     # a room warmer than ever seen
    hr_t, hr_f = settle(m, clean, p_mid, hot_in)
    out = {"points": clean, "target": round(target, 1), "trip": round(trip, 1) if trip else None, "notes": [],
           "predict": {"typical": {"power": p_mid, "inlet": t_mid, "temp": ty_t, "fan": ty_f},
                       "peak": {"power": p_hi, "inlet": t_hi, "temp": pk_t, "fan": pk_f},
                       "hot_room": {"power": p_mid, "inlet": hot_in, "temp": hr_t, "fan": hr_f}}}
    # Adaptive mode in the same conditions: it runs whatever each moment needs to
    # hold just under the target, instead of a curve that must already cover a hot day.
    goal = target - 1.0
    def held(p: float, tin: float) -> Dict[str, Any]:
        f = max(floor, min(100.0, needed_fan(m, p, tin, goal)))
        t = goal if floor < f < 100 else predict_temp(m, p, tin, f)   # at a limit: wherever that leaves it
        return {"temp": round(t, 1), "fan": round(f, 1)}
    out["adaptive"] = {"typical": held(p_mid, t_mid), "peak": held(p_hi, t_hi), "hot_room": held(p_mid, hot_in)}
    ad_f = out["adaptive"]["typical"]["fan"]
    if ad_f < ty_f - 3:
        out["prefer"] = "adaptive"
        per10 = predict_temp(m, p_mid, t_mid, ty_f) - predict_temp(m, p_mid, t_mid, min(100.0, ty_f + 10))
        weak = per10 < 3
        out["notes"].append(
            (f"This part responds weakly to its fans (+10% cools it only ~{per10:.1f}°C), so any fixed curve "
             f"that is safe on a hot day is loud on a normal one. " if weak else "") +
            f"Adaptive mode with a {target:.0f}° maximum would be quieter: about {ad_f:.0f}% at typical load "
            f"instead of {ty_f:.0f}%, speeding up only when conditions really call for it.")
    if pk_t > target + 0.5:
        out["notes"].append(f"At peak load it would settle at {pk_t}°C, a little over the target.")
    if hr_t > target + 0.5:
        out["notes"].append(f"If the room reached {hot_in:g}°C it would settle at {hr_t}°C; the watchdog and "
                            f"the climb to 100% cover that, but adaptive mode handles it more quietly.")
    return out


def suggest(server: str, sensors: List[str], fan_names: List[str], profile_name: str,
            target: Optional[float], limit: Optional[float], margin: float,
            min_floor: int = 0, days: int = 30, current: Optional[List[List[float]]] = None,
            airflow: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    data = collect(server, sensors, fan_names, profile_name, days, airflow)
    rows = data["rows"]
    out: Dict[str, Any] = {"pairs": len(rows), "hours": round(len(rows) * data["bucket_s"] / 3600, 1),
                           "samples": [{"t": round(r["t"], 1), "p": round(r["fan"], 1)}
                                       for r in rows[:: max(1, len(rows) // 400)]], "notes": []}
    m = learn(data)
    if m.get("error"):
        out["error"] = m["error"]
        return out
    out["model"] = m
    out["observed"] = m["temp_seen"]
    d = design(m, target, limit, margin, min_floor)
    out.update(d)
    if current and d.get("points"):
        now_t, now_f = settle(m, current, m["power"][1], m["inlet"][1])
        out["current"] = {"temp": now_t, "fan": now_f}
    return out
