"""Which fan cools what: measured, not guessed.

A fan's number says little about where its air goes. On a DL380p the HD
controller gets most of its air from fans other than "its" Fan 6, and
history can't tell them apart, because the fans of a group always moved
together. So it's measured, once, with a "fan map" run (curve.py drives it):

  1. every fan is held at the speed it runs now (floor = ceiling), and the
     temperatures are left to settle: the baseline;
  2. one fan at a time goes up by BUMP_PCT, until the temperatures settle
     again, and every sensor's drop is recorded;
  3. a last baseline at the end, so slow drift over the run (load, room
     temperature) can be taken out: each step is compared with the baseline
     interpolated to its time.

From that: how many °C each fan takes off each sensor per +10%, which
physical fans each PWM channel really drives, a weighting the models use
("effective airflow" = a weighted average of all fans, so a curve's model
knows when other fans are helping), and - carefully - a better grouping.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

BUMP_PCT = 20               # how much one fan goes up for its step
STEP_MIN_S = 600            # each step: at least this long ...
STEP_SETTLED_S = 480        # ... then done once every temperature held within 1°C this long
STEP_MAX_S = 1500           # ... or after this, settled or not
MEASURE_S = 240             # a step's reading = the average over its last 4 minutes
GUARD_BELOW_TRIP = 5        # abort when anything gets this close to its watchdog trip
MIN_EFFECT_C = 0.6          # a +20% step must change a sensor at least this much to count
DRIVES_PCT = 8              # a fan whose real speed rose this much follows that PWM channel
MIN_SHARE = 0.15            # a curve whose own fans give < 15% of its cooling can't steer it


def plan(fan_count: int) -> List[Optional[int]]:
    """Steps: None = baseline, n = PWM channel n raised."""
    return [None] + list(range(fan_count)) + [None]


def _interp(b0: Dict[str, Any], b1: Dict[str, Any], t: float, key: str, name: str) -> Optional[float]:
    v0, v1 = b0[key].get(name), b1[key].get(name)
    if v0 is None or v1 is None:
        return v0 if v1 is None else v1
    span = (b1["t"] - b0["t"]) or 1.0
    return v0 + (v1 - v0) * (t - b0["t"]) / span


def analyze(steps: List[Dict[str, Any]], inlet: Optional[str] = None) -> Dict[str, Any]:
    """steps: [{"fan": n|None, "t": time, "temps": {name: °C}, "fans": {idx: %}}] in run order."""
    b0, b1 = steps[0], steps[-1]
    bumps = [s for s in steps[1:-1] if s["fan"] is not None]
    names = sorted({n for s in steps for n in s["temps"]} - ({inlet} if inlet else set()))
    sensors: Dict[str, Any] = {}
    for n in names:
        drift = abs((b1["temps"].get(n) or 0) - (b0["temps"].get(n) or 0))
        seen = [s["temps"][n] for s in steps if n in s["temps"]]
        flat = len(seen) >= 2 and max(seen) - min(seen) < 0.05
        noise = max(MIN_EFFECT_C, drift)
        eff: Dict[str, float] = {}
        for s in bumps:
            base = _interp(b0, b1, s["t"], "temps", n)
            if base is None or n not in s["temps"]:
                continue
            drop = base - s["temps"][n]
            if abs(drop) >= noise:
                eff[str(s["fan"])] = round(drop * 10 / BUMP_PCT, 2)     # °C per +10%
        sensors[n] = {"per10": eff, "drift": round(drift, 2), "flat": flat,
                      "temp": round(b0["temps"].get(n) or 0, 1)}
    drives: Dict[str, List[int]] = {}
    for s in bumps:
        hit = []
        for f, v in s["fans"].items():
            base = _interp(b0, b1, s["t"], "fans", f)
            if base is not None and v - base >= DRIVES_PCT:
                hit.append(int(f))
        drives[str(s["fan"])] = sorted(hit)
    return {"bump_pct": BUMP_PCT, "sensors": sensors, "drives": drives,
            "fans": [s["fan"] for s in bumps],
            "started_at": int(b0["t"] - MEASURE_S / 2), "finished_at": int(b1["t"] + MEASURE_S / 2)}


def weights(fmap: Optional[Dict[str, Any]], sensor: Optional[str]) -> Optional[Dict[str, float]]:
    """How much each fan (PWM channel) cools this sensor, as weights summing to 1."""
    if not fmap or not sensor:
        return None
    s = (fmap.get("sensors") or {}).get(sensor)
    if not s or s.get("flat"):
        return None
    pos = {k: v for k, v in s["per10"].items() if v > 0}
    total = sum(pos.values())
    if total < MIN_EFFECT_C * 10 / BUMP_PCT:
        return None
    return {k: round(v / total, 4) for k, v in pos.items()}


def suggest_assign(fmap: Dict[str, Any], profiles: List[Dict[str, Any]],
                   assign: Dict[str, str]) -> List[Dict[str, Any]]:
    """Moves that clearly help, and nothing else.

    profiles: [{"id", "name", "sensor": the sensor it follows now}]. A fan
    moves only when the measurement proves its current curve's sensor gets
    (almost) nothing from it while another curve's sensor clearly does. A
    curve whose sensor couldn't be measured (a CPU sitting at iLO's 40° floor)
    keeps its fans: no proof they don't matter to it."""
    sens = fmap.get("sensors") or {}
    eff = {p["id"]: (sens.get(p["sensor"]) or {}) for p in profiles}
    name = {p["id"]: p["name"] for p in profiles}
    moves = []
    assign = dict(assign)
    for fan, cur in sorted(assign.items(), key=lambda kv: int(kv[0])):
        here = eff.get(cur) or {}
        if not here or here.get("flat") or fan not in [str(f) for f in fmap.get("fans", [])]:
            continue
        mine = here.get("per10", {}).get(fan, 0.0)
        best, gain = None, 0.0
        for pid, e in eff.items():
            if pid == cur or not e or e.get("flat"):
                continue
            g = e.get("per10", {}).get(fan, 0.0)
            if g > gain:
                best, gain = pid, g
        if best and gain >= MIN_EFFECT_C * 10 / BUMP_PCT and mine <= gain * 0.25:
            # never leave a curve with no fans
            if sum(1 for v in assign.values() if v == cur) <= 1:
                continue
            assign[fan] = best
            moves.append({"fan": int(fan), "from": cur, "to": best,
                          "why": f"Fan {int(fan) + 1} takes {gain:.1f}°C per +10% off {name[best]}'s sensor "
                                 f"but only {max(mine, 0):.1f}°C off {name[cur]}'s."})
    return moves
