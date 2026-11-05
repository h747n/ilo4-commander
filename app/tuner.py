"""Suggest a fixed fan curve from what adaptive mode has learned.

While a curve runs in adaptive mode, every poll records two things for it:
the temperature it steered by ("drive") and the fan speed it settled on
("target"). Over hours and days that maps out the quietest speed that was
actually enough at each temperature. This turns those pairs into a curve:

  * only steady periods count (temperature flat within a 5-minute bucket),
    so the moments just after a load spike don't pull the curve down;
  * per 2°C band it takes the 80th percentile, not the minimum - a little
    margin for the hotter afternoon or the busier job it hasn't seen yet;
  * it never goes down as temperature goes up;
  * above the hottest temperature it observed it adds a steep climb to
    100%, finishing before the watchdog's trip point.
"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

from .history_db import history_db

BIN_C = 2
MIN_PAIRS = 36              # ~3 hours of 5-minute buckets
MIN_PER_BIN = 3
STEADY_SPREAD_C = 2.0
PERCENTILE = 0.8
MAX_POINTS = 7


def suggest(server: str, profile_name: str, limit: Optional[float], margin: float,
            days: int = 7, min_floor: int = 0) -> Dict[str, Any]:
    now = int(time.time())
    since = now - max(1, min(30, days)) * 86400
    width_pts = 2000
    drive = history_db.query(server, "drive", profile_name, since, now, width_pts)
    speed = history_db.query(server, "target", profile_name, since, now, width_pts)
    by_ts = {p["ts"]: p for p in speed}
    pairs = [(d["avg"], by_ts[d["ts"]]["avg"]) for d in drive
             if d["ts"] in by_ts and d["max"] - d["min"] <= STEADY_SPREAD_C]
    samples = [{"t": round(t, 1), "p": round(p, 1)} for t, p in pairs]
    span_h = round((drive[-1]["ts"] - drive[0]["ts"]) / 3600, 1) if len(drive) > 1 else 0
    out: Dict[str, Any] = {"pairs": len(pairs), "hours": span_h,
                           "samples": samples[:: max(1, len(samples) // 400)]}
    if len(pairs) < MIN_PAIRS:
        out["error"] = (f"Not enough steady data yet ({len(pairs)} of {MIN_PAIRS} five-minute "
                        f"periods). Leave this curve in adaptive mode for a few hours, "
                        f"ideally through a busy time too.")
        return out

    bins: Dict[int, List[float]] = defaultdict(list)
    for t, p in pairs:
        bins[int(t // BIN_C) * BIN_C].append(p)
    pts: List[List[int]] = []
    for b in sorted(bins):
        vals = sorted(bins[b])
        if len(vals) < MIN_PER_BIN:
            continue
        pts.append([b + BIN_C // 2, int(round(vals[min(len(vals) - 1, int(len(vals) * PERCENTILE))]))])
    if len(pts) < 1:
        out["error"] = "The data never stayed steady long enough at any one temperature."
        return out

    for i in range(1, len(pts)):                     # never down as it gets hotter
        pts[i][1] = max(pts[i][1], pts[i - 1][1])
    if len(pts) > MAX_POINTS - 3:                    # keep it editable
        step = (len(pts) - 1) / (MAX_POINTS - 4)
        pts = [pts[round(i * step)] for i in range(MAX_POINTS - 3)]

    lo_t, lo_p = pts[0]
    hi_t, hi_p = pts[-1]
    trip = (limit - margin) if limit else hi_t + 15
    pts.insert(0, [max(0, lo_t - 5), lo_p])
    ramp_t = min(hi_t + 4, trip - 4)
    if ramp_t > hi_t:
        pts.append([int(ramp_t), min(100, hi_p + 15)])
    pts.append([int(max(trip - 1, pts[-1][0] + 1)), 100])

    clean: List[List[int]] = []
    for t, p in pts:                                 # strictly rising temperatures
        t = int(max(0, min(120, t)))
        p = int(max(min_floor, min(100, p)))
        if clean and t <= clean[-1][0]:
            clean[-1][1] = max(clean[-1][1], p)
            continue
        clean.append([t, p])
    out.update(points=clean, observed=[pts[1][0], hi_t], trip=round(trip, 1) if limit else None)
    return out
