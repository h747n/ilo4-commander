"""Host-side fan curve plus a thermal watchdog, for one server.

The patched iLO gives you registers, not a curve. The server's worker calls
`CurveEngine.step()` once per poll with fresh temperatures; this works out a
duty cycle from the user's points and writes it over SSH - with hysteresis
and ramp limits so the fans don't oscillate, and a watchdog that hands
control back to iLO when a sensor gets close to its caution threshold.

Unlike the old version, a watchdog trip no longer switches the curve off for
good. It parks it, and once every sensor has cooled down again the curve
picks up by itself.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .config import JsonStore
from .servers import server_dir

DEFAULT_POINTS = [[35, 10], [50, 18], [60, 30], [70, 50], [80, 80], [90, 100]]
LEGACY_KEYS = ("points", "sensors", "per_fan_floor")

DEFAULT_CURVE: Dict[str, Any] = {
    "enabled": False,
    "hysteresis_c": 2,
    "ramp_up_step": 12,
    "ramp_down_step": 4,
    "watchdog_auto_resume": True,
    # Each profile is its own curve: its own sensor and its own points. Every
    # fan is bound to exactly one profile.
    "profiles": [{"id": "default", "name": "All fans", "sensor": "__cpu__",
                  "points": [list(p) for p in DEFAULT_POINTS]}],
    "assign": {},           # fan index (as a string) -> profile id
}

RESUME_BELOW_C = 5          # extra degrees under the trip point before resuming
RESUME_TICKS = 3            # ...held for this many polls in a row
RESUME_MIN_S = 120          # ...and never sooner than this after the trip

# Adaptive ("target temperature") mode. Heat takes minutes to move through a
# chassis, so a controller that reacts every poll keeps adding speed long
# after the last increase would have been enough - and then takes ages to
# come back down. Everything here is in real seconds (not polls, so the
# server's poll interval doesn't change the behaviour) and looks at the
# temperature's trend as well as its level.
DEFAULT_TARGET_C = 65
TARGET_BELOW_TRIP_C = 3     # a target can never sit closer than this to the watchdog trip
ADAPT_UP_EVERY_S = 30       # normal pace of increases while over target...
ADAPT_URGENT_C = 4          # ...but every poll when this far over and still climbing
ADAPT_DOWN_EVERY_S = 60     # the temperature must sit low this long before each step down
ADAPT_DOWN_STEP = 1.0       # % per step down
ADAPT_TREND_S = 120         # history used to judge whether it's rising or falling
ADAPT_FALLING = -0.3        # °C/min: already cooling, the last increase is working
ADAPT_PANIC_C = 8           # this far over target: 100% at once

# "Learn": hold a curve's fans at a few known speeds so the curve designer can
# see how much airflow matters. Up first (only ever more cooling), then one
# step below normal - the useful bit for quietness - under a temperature guard.
LEARN_STEP_S = 600          # time at each speed; long enough to settle (~5 time constants)
LEARN_UP = (20, 10)         # % above the normal output
LEARN_DOWN = 5              # % below it for the last step
LEARN_GUARD_BELOW_TRIP = 5  # stop the moment the sensor gets this close to the watchdog trip


def normalize(cfg: Dict[str, Any], fan_count: int = 6) -> Dict[str, Any]:
    """Return a config that is always safe to iterate over.

    Also migrates the pre-2.1 shape (one set of points at the top level).
    """
    out = dict(DEFAULT_CURVE, **cfg)
    profiles = [p for p in (out.get("profiles") or []) if isinstance(p, dict)]
    if cfg.get("points") and not cfg.get("profiles"):
        profiles = [{"id": "default", "name": "All fans",
                     "sensor": (cfg.get("sensors") or ["__cpu__"])[0],
                     "points": cfg["points"]}]
    for key in LEGACY_KEYS + ("poll_seconds",):
        out.pop(key, None)
    if not profiles:
        profiles = [dict(DEFAULT_CURVE["profiles"][0])]

    clean, seen = [], set()
    for i, prof in enumerate(profiles):
        pid = str(prof.get("id") or f"p{i}")
        while pid in seen:
            pid += "_"
        seen.add(pid)
        pts = []
        for pair in (prof.get("points") or []):
            try:
                t, v = int(pair[0]), int(pair[1])
            except (TypeError, ValueError, IndexError):
                continue
            pts.append([max(0, min(120, t)), max(0, min(100, v))])
        pts.sort(key=lambda q: q[0])
        if len(pts) < 2:
            pts = [list(q) for q in DEFAULT_POINTS]
        try:
            target_c = int(prof.get("target_c", DEFAULT_TARGET_C))
        except (TypeError, ValueError):
            target_c = DEFAULT_TARGET_C
        clean.append({"id": pid, "name": str(prof.get("name") or pid),
                      "sensor": str(prof.get("sensor") or "__cpu__"), "points": pts,
                      "mode": "target" if prof.get("mode") == "target" else "curve",
                      "target_c": max(30, min(95, target_c))})
    out["profiles"] = clean

    ids = {p["id"] for p in clean}
    raw = out.get("assign") or {}
    out["assign"] = {str(i): (str(raw.get(str(i))) if str(raw.get(str(i))) in ids
                              else clean[0]["id"])
                     for i in range(max(1, int(fan_count)))}
    out["hysteresis_c"] = max(0.0, min(10.0, float(out.get("hysteresis_c", 2))))
    out["ramp_up_step"] = max(1, min(100, int(out.get("ramp_up_step", 12))))
    out["ramp_down_step"] = max(1, min(100, int(out.get("ramp_down_step", 4))))
    out["enabled"] = bool(out.get("enabled"))
    out["watchdog_auto_resume"] = bool(out.get("watchdog_auto_resume", True))
    return out


def interpolate(points: List[List[float]], temp: float) -> float:
    pts = sorted((float(t), float(p)) for t, p in points)
    if not pts:
        return 100.0
    if temp <= pts[0][0]:
        return pts[0][1]
    if temp >= pts[-1][0]:
        return pts[-1][1]
    for (t0, p0), (t1, p1) in zip(pts, pts[1:]):
        if t0 <= temp <= t1:
            return p0 + (p1 - p0) * (temp - t0) / ((t1 - t0) or 1.0)
    return pts[-1][1]


def hot_sensor(temps: List[Dict[str, Any]], wanted: str) -> Tuple[Optional[str], Optional[float]]:
    """Hottest sensor matching the selection.

    `__hottest__` takes anything, `__cpu__` takes the CPU sensors (HP names
    them like "02-CPU 1"), otherwise an exact name, then a substring match.
    """
    def pick(match) -> Tuple[Optional[str], Optional[float]]:
        name_, val_ = None, None
        for t in temps:
            v, n = t.get("reading"), (t.get("name") or "")
            if isinstance(v, (int, float)) and v > 0 and match(n) and (val_ is None or v > val_):
                name_, val_ = n, float(v)
        return name_, val_

    if not wanted or wanted == "__hottest__":
        return pick(lambda n: True)
    if wanted == "__cpu__":
        return pick(lambda n: "cpu" in n.lower() or "proc" in n.lower())
    hit = pick(lambda n: n.lower() == wanted.lower())
    return hit if hit[1] is not None else pick(lambda n: wanted.lower() in n.lower())


def sensor_limit(temps: List[Dict[str, Any]], name: Optional[str]) -> Optional[float]:
    """The threshold the watchdog uses for this sensor: caution, else critical."""
    for t in temps:
        if t.get("name") == name:
            lim = t.get("caution") or t.get("critical")
            return float(lim) if isinstance(lim, (int, float)) and lim else None
    return None


def effective_target(target_c: float, limit: Optional[float], margin: float) -> float:
    """The user's target, but never closer than TARGET_BELOW_TRIP_C to the trip point."""
    if limit is None:
        return float(target_c)
    return min(float(target_c), limit - margin - TARGET_BELOW_TRIP_C)


class CurveEngine:
    def __init__(self, sid: str, fans, log) -> None:
        self.store = JsonStore(server_dir(sid) / "curve.json", DEFAULT_CURVE)
        self.fans = fans                 # FanControl
        self.log = log                   # callable(kind, message)
        self.profile_state: Dict[str, Dict[str, Any]] = {}
        self.applied: Dict[str, int] = {}
        self.temps_seen: Dict[str, float] = {}
        self.fan_applied: Dict[int, int] = {}
        self.last_error: Optional[str] = None
        self.last_run: Optional[float] = None
        self.tripped_at: Optional[float] = None
        self.trip_reason: Optional[str] = None
        self._cool_ticks = 0
        self._lock = threading.RLock()
        # Adaptive mode, per profile: the output it has worked its way to, recent
        # (time, temperature) samples, and when it last stepped up or down.
        self.adapt: Dict[str, Dict[str, Any]] = {}
        # The output itself is also kept on disk, so a service restart (or an
        # upgrade) carries on from there instead of relearning from the curve.
        self.learned = JsonStore(server_dir(sid) / "adaptive.json", {})
        self.clock = time.time                # replaceable in tests
        self.learning: Dict[str, Dict[str, Any]] = {}   # profile id -> Learn run in progress

    # ------------------------------------------------------------------ #
    def config(self, fan_count: int) -> Dict[str, Any]:
        return normalize(self.store.read(), fan_count)

    def save(self, cfg: Dict[str, Any], fan_count: int) -> Dict[str, Any]:
        merged = dict(self.store.read(), **cfg)
        clean = normalize(merged, fan_count)
        self.store.write(clean)
        self.reset_applied()
        ids = {p["id"] for p in clean["profiles"]}
        for pid in list(self.learning):
            if not clean["enabled"] or pid not in ids:
                self.learning.pop(pid, None)
                self.log("tune", "Learning stopped: the curve was switched off or removed.")
        return clean

    def reset_applied(self) -> None:
        """Forget what we think is on the fans, so the next step rewrites them."""
        self.applied.clear()
        self.temps_seen.clear()
        self.fan_applied.clear()

    @property
    def tripped(self) -> bool:
        return self.tripped_at is not None

    # ------------------------------------------------------------------ #
    def watchdog(self, temps: List[Dict[str, Any]], srv: Dict[str, Any],
                 managed: bool) -> None:
        """Trip when any sensor gets within the margin of its caution threshold."""
        if not srv.get("watchdog_enabled", True):
            self.tripped_at = None
            return
        margin = float(srv.get("watchdog_margin_c", 5))
        hottest_gap, culprit, over = None, None, []
        for t in temps:
            val = t.get("reading")
            kind, limit = ("caution", t.get("caution")) if t.get("caution") else ("critical", t.get("critical"))
            if not isinstance(val, (int, float)) or not isinstance(limit, (int, float)) or not limit:
                continue
            gap = (limit - margin) - val            # <= 0 means trip
            if gap <= 0:
                over.append(f"{t.get('name')} {val}°C")
            if hottest_gap is None or gap < hottest_gap:
                hottest_gap, culprit = gap, (t.get("name"), val, kind, limit)

        if hottest_gap is None:
            return
        if not self.tripped:
            if hottest_gap <= 0 and managed:
                name, val, kind, limit = culprit
                self.tripped_at = time.time()
                if self.learning:
                    self.learning.clear()
                    self.log("tune", "Learning stopped: the watchdog took over.")
                self.trip_reason = (f"{name} reached {val}°C — trip point {limit - margin:g}°C "
                                    f"({kind} {limit:g}°C minus {margin:g}°C margin)")
                others = [o for o in over if not o.startswith(f"{name} ")]
                self._cool_ticks = 0
                restore_err = ""
                try:
                    self.fans.restore_auto()
                except Exception as exc:      # noqa: BLE001
                    restore_err = f" Handing back FAILED: {exc}"
                self.reset_applied()
                self.log("watchdog", f"Watchdog: {self.trip_reason}."
                         + (f" Also over: {', '.join(others)}." if others else "")
                         + (restore_err or " All fans handed back to iLO."))
            return

        # Tripped: wait for everything to cool down, then resume.
        if hottest_gap >= RESUME_BELOW_C:
            self._cool_ticks += 1
        else:
            self._cool_ticks = 0
        cfg = self.store.read()
        if (cfg.get("watchdog_auto_resume", True) and self._cool_ticks >= RESUME_TICKS
                and time.time() - (self.tripped_at or 0) >= RESUME_MIN_S):
            name, val, _kind, limit = culprit
            mins = int((time.time() - (self.tripped_at or time.time())) // 60)
            self.tripped_at = self.trip_reason = None
            self.reset_applied()
            self.log("watchdog", f"Cooled down after {mins} min (closest now: {name} {val}°C, "
                                 f"trip point {limit - margin:g}°C) — curve control resumed.")

    # ------------------------------------------------------------------ #
    def start_learning(self, pid: str, fan_count: int) -> Dict[str, Any]:
        with self._lock:
            cfg = self.config(fan_count)
            prof = next((p for p in cfg["profiles"] if p["id"] == pid), None)
            if not prof:
                raise ValueError("No such curve.")
            if not cfg["enabled"] or self.tripped:
                raise ValueError("Switch the automatic curve on first (and wait until the watchdog isn't active).")
            base = self.applied.get(pid)
            if base is None:
                raise ValueError("Let this curve run for a poll or two first.")
            floor = int(getattr(self.fans, "cfg", {}).get("min_pwm_percent", 0))
            levels: List[int] = []
            for v in [min(100, base + u) for u in LEARN_UP] + [max(floor, base - LEARN_DOWN)]:
                if v != base and v not in levels:
                    levels.append(v)
            if not levels:
                raise ValueError("This curve is already at 100%; there's nothing to vary.")
            now = self.clock()
            self.learning[pid] = {"levels": levels, "i": 0, "base": base, "step_started": now}
            self.log("tune", f"Learning started for '{prof['name']}': fans held at "
                             f"{', '.join(f'{v}%' for v in levels)} for {LEARN_STEP_S // 60} minutes each "
                             f"(normal output {base}%).")
            return self.learning[pid]

    def stop_learning(self, pid: str, reason: str = "stopped by hand") -> None:
        with self._lock:
            if self.learning.pop(pid, None) is not None:
                self.reset_applied()
                self.log("tune", f"Learning {reason}.")

    def _learn_level(self, pid: str, prof: Dict[str, Any], sensor: Optional[str], temp: float,
                     temps: List[Dict[str, Any]], goal: Optional[float]) -> Optional[int]:
        """The speed a Learn run wants right now, or None when there's no run."""
        run = self.learning.get(pid)
        if not run:
            return None
        margin = float(getattr(self.fans, "cfg", {}).get("watchdog_margin_c", 5))
        limit = sensor_limit(temps, sensor)
        guard = min(x for x in (goal, (limit - margin - LEARN_GUARD_BELOW_TRIP) if limit else None,
                                 1e9) if x is not None)
        if temp >= guard:
            self.learning.pop(pid, None)
            self.log("tune", f"Learning for '{prof['name']}' stopped early: {sensor} reached {temp:g}°C "
                             f"(the test's limit is {guard:g}°C). Normal control resumed.")
            return None
        now = self.clock()
        if now - run["step_started"] >= LEARN_STEP_S:
            run["i"] += 1
            run["step_started"] = now
        if run["i"] >= len(run["levels"]):
            self.learning.pop(pid, None)
            self.log("tune", f"Learning for '{prof['name']}' finished — Suggest curve can use it now.")
            return None
        return run["levels"][run["i"]]

    def clear_trip(self) -> None:
        if self.tripped:
            self.tripped_at = self.trip_reason = None
            self.reset_applied()
            self.log("watchdog", "Watchdog cleared by hand.")

    # ------------------------------------------------------------------ #
    def step(self, temps: List[Dict[str, Any]], fan_count: int) -> None:
        # The worker's poll and an API "apply now" can both get here.
        with self._lock:
            self._step(temps, fan_count)

    def _step(self, temps: List[Dict[str, Any]], fan_count: int) -> None:
        cfg = self.config(fan_count)
        self.last_run = time.time()
        if not cfg["enabled"] or self.tripped:
            self.profile_state = {}
            return

        by_profile: Dict[str, List[int]] = {}
        for fan, pid in cfg["assign"].items():
            by_profile.setdefault(pid, []).append(int(fan))
        profiles = {p["id"]: p for p in cfg["profiles"]}
        hyst, up, down = cfg["hysteresis_c"], cfg["ramp_up_step"], cfg["ramp_down_step"]
        live: Dict[str, Dict[str, Any]] = {}

        for pid, fan_list in by_profile.items():
            prof = profiles.get(pid)
            if not prof:
                continue
            name, temp = hot_sensor(temps, prof["sensor"])
            if temp is None:
                name, temp = hot_sensor(temps, "__hottest__")
            if temp is None:
                continue
            prev, seen = self.applied.get(pid), self.temps_seen.get(pid)
            goal = None
            if prof.get("mode") == "target":
                margin = float(getattr(self.fans, "cfg", {}).get("watchdog_margin_c", 5))
                goal = effective_target(prof["target_c"], sensor_limit(temps, name), margin)
            learn = self._learn_level(pid, prof, name, temp, temps, goal)
            if prof.get("mode") == "target":
                if learn is None:
                    target = self._adaptive(pid, prof, temp, goal, hyst, up)
                else:   # hold what it had learned while the test runs
                    target = int(round(self.adapt.get(pid, {}).get("out", learn)))
            else:
                self._forget_learned(pid)
                target = int(round(interpolate(prof["points"], temp)))
                if prev is not None:
                    if seen is not None and abs(temp - seen) < hyst and abs(target - prev) < 5:
                        target = prev
                    target = min(prev + up, max(prev - down, target))
            if learn is not None:
                base = self.learning[pid]["base"]
                # Steps above normal never cool *less* than normal control would.
                target = max(learn, target) if learn >= base else learn
            # Report what actually reaches the fans: set_pwm never goes below the floor.
            clamp = getattr(self.fans, "_clamp", None)
            if clamp:
                target = clamp(target)
            for fan in sorted(fan_list):
                if self.fan_applied.get(fan) != target:
                    self.fans.set_pwm(fan, target, "max", remember=False)
                    self.fan_applied[fan] = target
            self.applied[pid] = target
            if prev is None or target != prev:
                self.temps_seen[pid] = temp
            live[pid] = {"name": prof["name"], "sensor": name, "temp": round(temp, 1),
                         "applied": target, "fans": sorted(fan_list),
                         "mode": prof.get("mode", "curve"),
                         "goal": round(goal, 1) if goal is not None else None}
            run = self.learning.get(pid)
            if run:
                live[pid]["learning"] = {
                    "step": run["i"] + 1, "of": len(run["levels"]), "level": run["levels"][run["i"]],
                    "left_s": int(max(0, LEARN_STEP_S - (self.clock() - run["step_started"])
                                      + LEARN_STEP_S * (len(run["levels"]) - run["i"] - 1)))}
        self.profile_state = live
        self.last_error = None

    def _adaptive(self, pid: str, prof: Dict[str, Any], temp: float, goal: float,
                  hyst: float, up: int) -> int:
        """Find the lowest fan speed that holds `temp` at or under `goal`."""
        now = self.clock()
        a = self.adapt.get(pid)
        if a is None:
            # Carry on from what it learned before a restart; otherwise start from
            # what the curve would give, so switching modes isn't a jolt.
            saved = self.learned.read().get(pid)
            start = float(saved) if isinstance(saved, (int, float)) else float(interpolate(prof["points"], temp))
            a = self.adapt[pid] = {"out": start, "hist": [], "last_up": 0.0, "last_down": now}
        a["hist"] = [(t, v) for t, v in a["hist"] if now - t <= ADAPT_TREND_S] + [(now, temp)]
        first_t, first_v = a["hist"][0]
        span = now - first_t
        slope = (temp - first_v) / (span / 60.0) if span >= 20 else 0.0      # °C per minute
        err = temp - goal
        band = max(hyst, 1.0)

        if err >= ADAPT_PANIC_C:
            a["out"], a["last_up"] = 100.0, now
        elif err > 0:
            if slope <= ADAPT_FALLING:
                pass                    # already cooling: give the last increase time to work
            elif now - a["last_up"] >= ADAPT_UP_EVERY_S or (err >= ADAPT_URGENT_C and slope > 0):
                # Size the step by how far over it is and how fast it's still climbing.
                step = err * 1.5 + max(0.0, slope) * 2.0
                a["out"] += min(float(up), max(1.0, step))
                a["last_up"] = now
            a["last_down"] = now        # no stepping down right after being over
        elif err < -band:
            recent = [v for t, v in a["hist"] if now - t <= ADAPT_DOWN_EVERY_S]
            steady_long_enough = span >= ADAPT_DOWN_EVERY_S * 0.9
            if (steady_long_enough and max(recent) < goal - band and slope <= 0.2
                    and now - a["last_down"] >= ADAPT_DOWN_EVERY_S):
                a["out"] -= ADAPT_DOWN_STEP
                a["last_down"] = now
        floor = float(getattr(self.fans, "cfg", {}).get("min_pwm_percent", 0))
        a["out"] = max(floor, min(100.0, a["out"]))
        result = int(round(a["out"]))
        if a.get("saved") != result:    # remember it across restarts (changes are rare)
            data = self.learned.read()
            data[pid] = result
            self.learned.write(data)
            a["saved"] = result
        return result

    def _forget_learned(self, pid: str) -> None:
        if pid in self.adapt:
            self.adapt.pop(pid, None)
            data = self.learned.read()
            if data.pop(pid, None) is not None:
                self.learned.write(data)

    def status(self, fan_count: int) -> Dict[str, Any]:
        return {
            "config": self.config(fan_count),
            "profile_state": self.profile_state,
            "fan_applied": {str(k): v for k, v in self.fan_applied.items()},
            "last_run": self.last_run,
            "last_error": self.last_error,
            "tripped_at": self.tripped_at,
            "trip_reason": self.trip_reason,
        }
