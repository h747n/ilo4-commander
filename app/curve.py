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
ADAPT_LEAD_PCT = 20         # output may run at most this far ahead of the fans' real speed
BOOST_PCT = 15              # watchdog, first step: this much extra on every fan's real speed ...
BOOST_HANDBACK_C = 2        # ... and only this far past the trip point hand everything to iLO
BOOST_RELEASE_C = 3         # the extra goes once everything is this far under the trip point

# "Learn": hold a curve's fans at a few known speeds so the curve designer can
# see how much airflow matters. Up first (only ever more cooling), then one
# step below normal - the useful bit for quietness - under a temperature guard.
LEARN_STEP_S = 600          # minimum time at each speed ...
LEARN_SETTLED_S = 480       # ... then move on once the temperature held within 1°C this long
LEARN_MAX_STEP_S = 1800     # ... or after this, settled or not (big heatsinks are slow:
                            # a DL360 HD controller was still climbing 66->84° after 10 min)
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
                      "target_c": max(30, min(95, target_c)),
                      # True once the user picked a maximum themselves; until then the
                      # designer and the mode switch choose a sensible one.
                      "target_set": bool(prof.get("target_set"))})
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
        self.fan_actual: Dict[int, float] = {}           # real speed per fan, set by the worker
        # What the learner (learner.py) has worked out for each curve, kept on disk
        # so it's there straight after a restart; plus the live inputs it needs.
        self.models_store = JsonStore(server_dir(sid) / "models.json", {})
        self.models: Dict[str, Dict[str, Any]] = self.models_store.read()
        self.live: Dict[str, float] = {}                 # power (W) and inlet (°C), set by the worker
        self.bias: Dict[str, float] = {}                 # live correction of each model, °C
        self.track: Dict[str, List[Tuple[float, float, float]]] = {}   # (time, temp, model error)
        self.anomaly_at: Dict[str, float] = {}
        self.relearn_wanted = False                      # ask the learner to refit soon
        # Floors a Learn run has set, kept on disk: if the service stops mid-run,
        # the first poll after it starts again clears them.
        self.fan_floor: Dict[int, int] = {int(k): v for k, v in self.learned.read().get("_floors", {}).items()}
        # Fan map (fanmap.py): which fan cools which sensor, measured by a run that
        # raises one fan at a time. The run in progress, and the last result.
        self.mapping: Optional[Dict[str, Any]] = None
        self.fanmap_store = JsonStore(server_dir(sid) / "fanmap.json", {})
        self.fanmap: Optional[Dict[str, Any]] = {k: v for k, v in self.fanmap_store.read().items()
                                                 if k != "_run"} or None
        # Floors as last sent to the iLO; forgotten by reset_applied, so a reassert
        # (e.g. after an iLO reset, which drops them) sends them again.
        self.floor_sent: Dict[int, int] = {}
        self.boost: Optional[Dict[str, Any]] = None       # watchdog step one: +15% pinned on every fan
        self._resume_mapping()

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
        if self.mapping is not None and not clean["enabled"]:
            self.mapping = None
            self._save_mapping()
            self._clear_floors()        # no curve step will run to clear them
            self.log("tune", "Fan map stopped: the curve was switched off.")
        return clean

    def reset_applied(self) -> None:
        """Forget what we think is on the fans, so the next step rewrites them."""
        self.applied.clear()
        self.temps_seen.clear()
        self.fan_applied.clear()
        self.floor_sent.clear()

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
            # Two steps instead of one jump to iLO's full-speed "rocket" mode:
            # at the trip point every fan gets +BOOST_PCT on its real speed (pinned);
            # only BOOST_HANDBACK_C past it does iLO get everything back.
            if managed and -BOOST_HANDBACK_C < hottest_gap <= 0:
                if self.boost is None:
                    self._start_boost(culprit, margin, over)
                self._cool_ticks = 0
                return
            if self.boost is not None and hottest_gap > 0:
                self._cool_ticks = self._cool_ticks + 1 if hottest_gap >= BOOST_RELEASE_C else 0
                if self._cool_ticks >= RESUME_TICKS and time.time() - self.boost["since"] >= RESUME_MIN_S:
                    name, val, _kind, limit = culprit
                    mins = int((time.time() - self.boost["since"]) // 60)
                    self.boost = None
                    self._cool_ticks = 0
                    self.reset_applied()
                    self.log("watchdog", f"Cooled down after {mins} min with the extra {BOOST_PCT}% (closest now: "
                                         f"{name} {val}°C, trip point {limit - margin:g}°C) — normal speeds back.")
                return
            if hottest_gap <= -BOOST_HANDBACK_C and managed:
                name, val, kind, limit = culprit
                boosted = self.boost is not None
                self.boost = None
                self.tripped_at = time.time()
                if self.learning:
                    self.learning.clear()
                    self.log("tune", "Learning stopped: the watchdog took over.")
                if self.mapping is not None:
                    self.mapping = None
                    self._save_mapping()
                    self._clear_floors()
                    self.log("tune", "Fan map stopped: the watchdog took over.")
                self.trip_reason = (f"{name} reached {val}°C — {BOOST_HANDBACK_C:g}°C past the trip point "
                                    f"{limit - margin:g}°C ({kind} {limit:g}°C minus {margin:g}°C margin)"
                                    + (f", even with {BOOST_PCT}% extra on every fan" if boosted else ""))
                others = [o for o in over if not o.startswith(f"{name} ")]
                self._cool_ticks = 0
                restore_err = ""
                try:
                    self.fans.restore_auto()
                    if self.fan_floor:            # restore_auto dropped them on the iLO too
                        self.fan_floor.clear()
                        self._save_floors()
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
            if self.mapping is not None:
                raise ValueError("A fan map is running; wait for it to finish.")
            base = self.applied.get(pid)
            if base is None:
                raise ValueError("Let this curve run for a poll or two first.")
            # Start from what the fans really do: under a ceiling iLO often runs
            # them far lower (23% under a 54% ceiling), and +20% on the ceiling
            # would mean a needless jump to 74%.
            mine = [int(f) for f, p in cfg["assign"].items() if p == pid]
            real = [self.fan_actual[f] for f in mine if isinstance(self.fan_actual.get(f), (int, float))]
            if real:
                base = int(round(min(base, sum(real) / len(real))))
            floor = int(getattr(self.fans, "cfg", {}).get("min_pwm_percent", 0))
            levels: List[int] = []
            for v in [min(100, base + u) for u in LEARN_UP] + [max(floor, base - LEARN_DOWN)]:
                if v != base and v not in levels:
                    levels.append(v)
            if not levels:
                raise ValueError("This curve is already at 100%; there's nothing to vary.")
            now = self.clock()
            self.learning[pid] = {"levels": levels, "i": 0, "base": base, "step_started": now, "recent": []}
            self.log("tune", f"Learning started for '{prof['name']}': fans held at "
                             f"{', '.join(f'{v}%' for v in levels)}, each until the temperature settles "
                             f"({LEARN_STEP_S // 60}-{LEARN_MAX_STEP_S // 60} min) (normal output {base}%).")
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
        # Steps above normal only add cooling, so only the hard safety limit applies
        # (a curve sitting right at its adaptive target must not stop the test).
        # The step below normal also stops a few degrees over the target.
        down_step = run["levels"][min(run["i"], len(run["levels"]) - 1)] < run["base"]
        guard = min(x for x in ((limit - margin - LEARN_GUARD_BELOW_TRIP) if limit else None,
                                 (goal + 3) if (goal is not None and down_step) else None,
                                 1e9) if x is not None)
        if temp >= guard:
            self.learning.pop(pid, None)
            self.log("tune", f"Learning for '{prof['name']}' stopped early: {sensor} reached {temp:g}°C "
                             f"(the test's limit is {guard:g}°C). Normal control resumed.")
            return None
        now = self.clock()
        recent = run.setdefault("recent", [])
        recent.append((now, temp))
        while recent and now - recent[0][0] > LEARN_SETTLED_S:
            recent.pop(0)
        held = now - run["step_started"]
        settled = (held >= LEARN_STEP_S and recent and now - recent[0][0] >= LEARN_SETTLED_S - 15
                   and max(t for _, t in recent) - min(t for _, t in recent) <= 1.0)
        if settled or held >= LEARN_MAX_STEP_S:
            run["i"] += 1
            run["step_started"] = now
            recent.clear()
        if run["i"] >= len(run["levels"]):
            self.learning.pop(pid, None)
            self.log("tune", f"Learning for '{prof['name']}' finished — the model is being updated with it now.")
            self.relearn_wanted = True
            return None
        return run["levels"][run["i"]]

    # ------------------------------------------------------------------ #
    # Fan map: hold every fan, raise one at a time, see what cools down
    # ------------------------------------------------------------------ #
    def start_mapping(self, fan_count: int) -> Dict[str, Any]:
        from . import fanmap as FM
        with self._lock:
            cfg = self.config(fan_count)
            if not cfg["enabled"] or self.tripped:
                raise ValueError("Switch the automatic curve on first (and wait until the watchdog isn't active).")
            if self.learning:
                raise ValueError("A Learn run is in progress; wait for it to finish.")
            if self.mapping is not None:
                raise ValueError("A fan map is already running.")
            floor = int(getattr(self.fans, "cfg", {}).get("min_pwm_percent", 0))
            base: Dict[int, int] = {}
            for f in range(fan_count):
                v = self.fan_actual.get(f)
                if not isinstance(v, (int, float)):
                    v = self.fan_applied.get(f)
                if v is None:
                    raise ValueError("Let the curve run for a poll or two first, so the fans' speeds are known.")
                base[f] = max(floor, min(100 - FM.BUMP_PCT, int(round(v))))
            now = self.clock()
            self.mapping = {"plan": FM.plan(fan_count), "i": 0, "base": base, "step_started": now,
                            "samples": [], "steps": [], "started": now}
            self._save_mapping()
            n = len(self.mapping["plan"])
            self.log("tune", f"Fan map started: every fan held where it is "
                             f"({', '.join(f'{f + 1}: {v}%' for f, v in base.items())}), then one at a time "
                             f"+{FM.BUMP_PCT}% until the temperatures settle - {n} steps, about "
                             f"{n * FM.STEP_FIT_MIN_S // 60}-{n * FM.STEP_MAX_S // 60} min (usually 5-10 per step).")
            return self.mapping

    def stop_mapping(self, reason: str = "stopped by hand") -> None:
        with self._lock:
            if self.mapping is not None:
                self.mapping = None
                self._save_mapping()
                self.reset_applied()
                self.log("tune", f"Fan map {reason}. Normal control resumed.")

    def _map_step(self, temps: List[Dict[str, Any]]) -> None:
        from . import fanmap as FM
        from .tuner import pick_inlet
        run = self.mapping
        now = self.clock()
        margin = float(getattr(self.fans, "cfg", {}).get("watchdog_margin_c", 5))
        # Each sensor's limit for the run: GUARD_BELOW_TRIP under its watchdog trip -
        # or, for one that already sits that close (a DL360 "HD Cntlr Zone" lives at
        # 55° with its trip at 60), 2° over where it started, still under the trip.
        # The run only ever adds airflow, so nothing should get hotter than at the start.
        start = run.setdefault("start_temps", {t.get("name"): t.get("reading") for t in temps
                                               if isinstance(t.get("reading"), (int, float))})
        for t in temps:
            v, lim = t.get("reading"), t.get("caution") or t.get("critical")
            if not (isinstance(v, (int, float)) and isinstance(lim, (int, float)) and lim):
                continue
            trip = lim - margin
            guard = trip - FM.GUARD_BELOW_TRIP
            s0 = start.get(t.get("name"))
            if isinstance(s0, (int, float)) and s0 + 2 > guard:
                guard = min(trip - 1, s0 + 2)
            if v >= guard:
                self.mapping = None
                self._save_mapping()
                self.reset_applied()
                self.log("tune", f"Fan map stopped early: {t.get('name')} reached {v:g}°C (the run's limit is "
                                 f"{guard:g}°C). Normal control resumed.")
                self.profile_state = {}
                return
        k = run["plan"][run["i"]]
        levels = dict(run["base"])
        if k is not None:
            levels[k] = min(100, levels[k] + FM.BUMP_PCT)
        clamp = getattr(self.fans, "_clamp", lambda p: int(p))
        for fan, lvl in sorted(levels.items()):
            self._drive(fan, clamp(lvl), True)
        readings = {t["name"]: float(t["reading"]) for t in temps
                    if isinstance(t.get("reading"), (int, float)) and t["reading"] > 0 and t.get("name")}
        run["samples"].append((now, readings, {str(f): float(v) for f, v in self.fan_actual.items()}))
        run["samples"] = [s for s in run["samples"] if now - s[0] <= FM.STEP_MAX_S]
        inlet = pick_inlet(list(readings))
        held = now - run["step_started"]
        # Done when every sensor's end value is known: either it has settled, or its
        # settling curve has been seen long enough to extrapolate (fanmap.step_reading).
        temps_now: Dict[str, float] = {}
        # Once a sensor's end value has been worked out it stays worked out for this
        # step (its latest estimate is used): with ~30 whole-degree sensors flipping
        # between two values, "all known in the same poll" almost never happened.
        seen_known = run.setdefault("known", set())
        for n in readings:
            series = [(s[0], s[1][n]) for s in run["samples"] if n in s[1]]
            val, ok = FM.step_reading(series, held)
            if val is not None:
                temps_now[n] = val
            if ok and held >= FM.STEP_FIT_MIN_S:
                seen_known.add(n)
        known = held >= FM.STEP_FIT_MIN_S and all(n in seen_known for n in readings if n != inlet)
        if not known and held >= FM.STEP_MIN_S:      # the old rule still counts
            window = [s for s in run["samples"] if now - s[0] <= FM.STEP_SETTLED_S]
            known = now - window[0][0] >= FM.STEP_SETTLED_S - 15 and all(
                max(v) - min(v) <= 1.0 for v in
                ([s[1][n] for s in window if n in s[1]] for n in readings if n != inlet) if v)
        if known or held >= FM.STEP_MAX_S:
            last = [s for s in run["samples"] if now - s[0] <= min(FM.MEASURE_S, held / 2)] or run["samples"][-1:]
            fans_avg = {n: sum(s[2][n] for s in last if n in s[2]) / sum(1 for s in last if n in s[2])
                        for n in {n for s in last for n in s[2]}}
            run["steps"].append({"fan": k, "t": now, "temps": temps_now, "fans": fans_avg,
                                 "held_s": int(held)})
            run["i"] += 1
            run["step_started"] = now
            run["samples"] = []
            run["known"] = set()
            if run["i"] >= len(run["plan"]):
                self._finish_mapping(FM.analyze(run["steps"], inlet))
            else:
                self._save_mapping()
        self.profile_state = {}

    # A run survives a restart of the service (an upgrade, say): what it has
    # measured is kept on disk and it carries on with the step it was in.
    MAPPING_RESUME_MAX_S = 3600

    def _save_mapping(self) -> None:
        run = self.mapping
        data = self.fanmap_store.read()
        if run is None:
            data.pop("_run", None)
        else:
            data["_run"] = {k: run[k] for k in ("plan", "i", "steps", "started")}
            data["_run"]["base"] = {str(f): v for f, v in run["base"].items()}
            data["_run"]["start_temps"] = run.get("start_temps") or {}
            data["_run"]["saved_at"] = self.clock()
        self.fanmap_store.write(data)

    def _resume_mapping(self) -> None:
        saved = (self.fanmap_store.read() or {}).get("_run")
        if not saved:
            return
        if self.clock() - saved.get("saved_at", 0) > self.MAPPING_RESUME_MAX_S:
            self._save_mapping()                       # too old to carry on: drop it
            self.log("tune", "A fan map was interrupted too long ago to carry on; start it again if needed.")
            return
        now = self.clock()
        self.mapping = {"plan": saved["plan"], "i": saved["i"], "steps": saved["steps"],
                        "started": saved["started"], "base": {int(f): v for f, v in saved["base"].items()},
                        "start_temps": saved.get("start_temps") or {}, "step_started": now, "samples": []}
        self.log("tune", f"Fan map carried on after a restart, at step {saved['i'] + 1} of {len(saved['plan'])}.")

    def _finish_mapping(self, result: Dict[str, Any]) -> None:
        result["base_pct"] = {str(f): v for f, v in self.mapping["base"].items()}
        self.fanmap = result
        self.fanmap_store.write(result)
        self.mapping = None
        self.reset_applied()
        self.relearn_wanted = True
        mins = (result["finished_at"] - result["started_at"]) // 60
        odd = [f"PWM {int(k) + 1} moved fan(s) {', '.join(str(x + 1) for x in v) or 'none'}"
               for k, v in result["drives"].items() if v != [int(k)]]
        self.log("tune", f"Fan map finished after {mins} min — see the Fans tab for which fan cools what."
                         + (f" Note: {'; '.join(odd)}." if odd else ""))

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
        if self.mapping is not None:
            self._map_step(temps)
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
            obs = self._observe(pid, prof, temp, fan_list)
            f_pred = None
            m = self.usable_model(pid)
            from .fanmap import MIN_SHARE
            steerable = not (m or {}).get("eff") or m["eff"]["s"] >= MIN_SHARE
            if prof.get("mode") == "target" and m and m.get("quality") == "good" and steerable:
                from .tuner import needed_fan
                # Aim a little under the target so the feedback loop has room.
                f_pred = needed_fan(m, self.live.get("power", 0.0), self.live["inlet"],
                                    goal - max(hyst, 1.0) / 2, self.bias.get(pid, 0.0))
            hot_room = None
            ceiling_c = goal if goal is not None else (prof["target_c"] if prof.get("target_set") else None)
            if m and ceiling_c is not None:
                from .tuner import needed_fan
                warm = self.live["inlet"] + 5
                need = needed_fan(m, self.live.get("power", 0.0), warm, ceiling_c, self.bias.get(pid, 0.0))
                hot_room = {"inlet": round(warm, 1), "fan": round(min(need, 999.0), 1)}
            if prof.get("mode") == "target":
                if learn is None:
                    real = [self.fan_actual[f] for f in fan_list if isinstance(self.fan_actual.get(f), (int, float))]
                    target = self._adaptive(pid, prof, temp, goal, hyst, up, f_pred,
                                            actual=(sum(real) / len(real)) if real else None)
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
                # Exactly the step's speed: comparing with the normal output would
                # compare against a ceiling iLO may not even be using (54% ceiling,
                # 23% real). The temperature guard is what keeps a step safe.
                target = learn
            # Report what actually reaches the fans: set_pwm never goes below the floor.
            clamp = getattr(self.fans, "_clamp", None)
            if clamp:
                target = clamp(target)
            # A ceiling only stops iLO going faster; on these iLOs it often runs well
            # below it. A Learn step that adds airflow therefore also sets a floor,
            # so the fans really turn at that speed; the floor goes once it's over.
            # Adaptive mode pins its fans the same way: with only a ceiling, raising
            # it changed nothing while iLO kept the fan at 20%, and the output wound
            # up to 100% overnight (DL380p HD controller) with no control at all.
            want_floor = ((learn is not None and learn >= self.learning[pid]["base"])
                          or (learn is None and prof.get("mode") == "target"))
            for fan in sorted(fan_list):
                pct, pin = target, want_floor
                b = (self.boost or {}).get("levels", {}).get(fan)
                if b is not None and b > pct:     # watchdog's first step: +15%, pinned
                    pct, pin = b, True
                self._drive(fan, pct, pin)
            self.applied[pid] = target
            if prev is None or target != prev:
                self.temps_seen[pid] = temp
            live[pid] = {"name": prof["name"], "sensor": name, "temp": round(temp, 1),
                         "applied": target, "fans": sorted(fan_list),
                         "mode": prof.get("mode", "curve"),
                         "goal": round(goal, 1) if goal is not None else None,
                         "model": obs,
                         "predicted_need": (round(min(100.0, f_pred), 1) if f_pred is not None else None),
                         "hot_room": hot_room}
            run = self.learning.get(pid)
            if run:
                live[pid]["learning"] = {
                    "step": run["i"] + 1, "of": len(run["levels"]), "level": run["levels"][run["i"]],
                    # least it can take; each step may run up to LEARN_MAX_STEP_S
                    "left_s": int(max(0, LEARN_STEP_S - (self.clock() - run["step_started"]))
                                  + LEARN_STEP_S * (len(run["levels"]) - run["i"] - 1)),
                    "max_left_s": int(max(0, LEARN_MAX_STEP_S - (self.clock() - run["step_started"]))
                                      + LEARN_MAX_STEP_S * (len(run["levels"]) - run["i"] - 1))}
        self.profile_state = live
        self.last_error = None

    def _adaptive(self, pid: str, prof: Dict[str, Any], temp: float, goal: float,
                  hyst: float, up: int, f_pred: Optional[float] = None,
                  actual: Optional[float] = None) -> int:
        """Find the lowest fan speed that holds `temp` at or under `goal`.

        With a good learned model (`f_pred`: the speed it predicts is needed right
        now, for the current load and room temperature) it heads straight there
        instead of feeling its way - faster down when the model says there's room,
        straight up to the prediction when over target. The temperature feedback
        below still has the final say, so a wrong model can't overheat anything.
        """
        now = self.clock()
        if f_pred is not None:
            f_pred = max(0.0, min(100.0, f_pred))
        a = self.adapt.get(pid)
        if a is None:
            # Carry on from what it learned before a restart; otherwise start from the
            # model's prediction or from what the curve gives, so switching isn't a jolt.
            saved = self.learned.read().get(pid)
            start = (float(saved) if isinstance(saved, (int, float))
                     else f_pred if f_pred is not None else float(interpolate(prof["points"], temp)))
            if actual is not None:
                # Never start above what the fans really do now: the output is
                # pinned (a floor too), and a saved ceiling iLO ignored would jolt them.
                start = min(start, actual)
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
                if f_pred is not None and f_pred > a["out"]:
                    # The model knows roughly how much is needed: go there in one
                    # move (capped, in case the model is off).
                    a["out"] = min(f_pred, a["out"] + 2 * up)
                a["last_up"] = now
            a["last_down"] = now        # no stepping down right after being over
        elif err < -band:
            recent = [v for t, v in a["hist"] if now - t <= ADAPT_DOWN_EVERY_S]
            steady_long_enough = span >= ADAPT_DOWN_EVERY_S * 0.9
            if (steady_long_enough and max(recent) < goal - band and slope <= 0.2
                    and now - a["last_down"] >= ADAPT_DOWN_EVERY_S):
                step = ADAPT_DOWN_STEP
                if f_pred is not None and a["out"] > f_pred + 2:
                    step = min(3.0, a["out"] - f_pred)     # the model says there's room
                a["out"] -= step
                a["last_down"] = now
        floor = float(getattr(self.fans, "cfg", {}).get("min_pwm_percent", 0))
        if actual is not None and err < ADAPT_PANIC_C and a["out"] > actual + ADAPT_LEAD_PCT:
            # Never run far ahead of what the fans really do (anti wind-up): if they
            # don't follow, more output only stores up a jolt for later.
            a["out"] = max(actual + ADAPT_LEAD_PCT, floor)
        a["out"] = max(floor, min(100.0, a["out"]))
        result = int(round(a["out"]))
        if a.get("saved") != result:    # remember it across restarts (changes are rare)
            data = self.learned.read()
            data[pid] = result
            self.learned.write(data)
            a["saved"] = result
        return result

    # ------------------------------------------------------------------ #
    # Learned models
    # ------------------------------------------------------------------ #
    def set_model(self, pid: str, model: Dict[str, Any]) -> None:
        self.models[pid] = model
        self.models_store.write(self.models)

    def usable_model(self, pid: str) -> Optional[Dict[str, Any]]:
        m = self.models.get(pid)
        if not m or m.get("error") or "inlet" not in self.live:
            return None
        return self._with_live_airflow(m)

    def _with_live_airflow(self, m: Dict[str, Any]) -> Dict[str, Any]:
        """A model fitted on effective airflow (all fans, weighted by the fan map) in
        terms of this curve's own fans, with the other fans at their speed right now:
        rise = (a + bP) / (s*own + others + c)  =  (a/s + b/s*P) / (own + (others + c)/s)."""
        e = m.get("eff")
        if not e:
            return m
        own = {str(f) for f in e["own"]}
        others = 0.0
        for k, w in e["w"].items():
            if k in own:
                continue
            v = self.fan_actual.get(int(k))
            if not isinstance(v, (int, float)):
                v = self.fan_applied.get(int(k))
            if v is None:
                return m                    # stored conversion (typical other fans) then
            others += w * float(v)
        s = max(e["s"], 0.05)
        return dict(m, a=e["a"] / s, b=e["b"] / s, c=(e["c"] + others) / s)

    def own_speed(self, m: Dict[str, Any], fans: List[int]) -> Optional[float]:
        """This curve's fan speed as the model counts it (weighted when it has a fan map)."""
        e = m.get("eff")
        if e and e["s"] > 0:
            num = 0.0
            for k in e["own"]:
                w = e["w"].get(str(k), 0.0)
                v = self.fan_actual.get(int(k))
                if w and not isinstance(v, (int, float)):
                    break
                num += w * (v or 0.0)
            else:
                return num / e["s"]
        real = [self.fan_actual[f] for f in fans if isinstance(self.fan_actual.get(f), (int, float))]
        return sum(real) / len(real) if real else None

    def _observe(self, pid: str, prof: Dict[str, Any], temp: float, fans: List[int]) -> Optional[Dict[str, Any]]:
        """Compare reality with the model: keep its live bias up to date, and flag a
        part that runs much hotter than it should for this load and airflow."""
        m = self.usable_model(pid)
        speed = self.own_speed(m, fans) if m else None
        if not m or speed is None:
            return None
        from .tuner import predict_temp
        now = self.clock()
        expect = predict_temp(m, self.live.get("power", 0.0), self.live["inlet"], speed)
        miss = temp - expect
        hist = [x for x in self.track.get(pid, []) if now - x[0] <= 600] + [(now, temp, miss)]
        self.track[pid] = hist
        steady = len(hist) >= 3 and max(h[1] for h in hist[-6:]) - min(h[1] for h in hist[-6:]) <= 1.0
        if steady:
            # Slow, bounded correction: the model is a week's average, today may differ a bit.
            b = self.bias.get(pid, 0.0)
            self.bias[pid] = max(-3.0, min(3.0, b + 0.02 * (miss - b)))
        span = now - hist[0][0]
        avg_miss = sum(h[2] for h in hist) / len(hist)
        limit = max(4.0, 3 * m.get("tested_c", 1.0))
        if span >= 540 and steady and avg_miss > limit and now - self.anomaly_at.get(pid, 0) > 6 * 3600:
            self.anomaly_at[pid] = now
            self.log("anomaly", f"{prof['name']}: running {avg_miss:.1f}°C hotter than expected for this load, "
                                f"room temperature and fan speed (expected ~{expect:.0f}°C, seeing {temp:g}°C). "
                                f"Worth checking for dust, a weak fan or blocked airflow.")
        return {"expect": round(expect, 1), "bias": round(self.bias.get(pid, 0.0), 2)}

    def _start_boost(self, culprit, margin: float, over: List[str]) -> None:
        name, val, kind, limit = culprit
        clamp = getattr(self.fans, "_clamp", lambda p: int(p))
        levels = {}
        for f in sorted(set(self.fan_actual) | set(self.fan_applied)):
            now = max(float(self.fan_actual.get(f) or 0), float(self.fan_applied.get(f) or 0))
            levels[f] = clamp(min(100, now + BOOST_PCT))
        self.boost = {"since": time.time(), "levels": levels,
                      "reason": f"{name} reached {val}°C (trip point {limit - margin:g}°C)"}
        if self.learning:
            self.learning.clear()
            self.log("tune", "Learning stopped: the watchdog stepped in.")
        if self.mapping is not None:
            self.mapping = None
            self._save_mapping()
            self.log("tune", "Fan map stopped: the watchdog stepped in.")
        self.reset_applied()
        others = [o for o in over if not o.startswith(f"{name} ")]
        self.log("watchdog", f"Watchdog: {self.boost['reason']} — every fan +{BOOST_PCT}% "
                             f"({', '.join(f'{f + 1}: {v}%' for f, v in levels.items())})."
                             + (f" Also over: {', '.join(others)}." if others else "")
                             + f" Fans go back to iLO only if it reaches {limit - margin + BOOST_HANDBACK_C:g}°C.")

    def _drive(self, fan: int, pct: int, pin: bool) -> None:
        """Put one fan at `pct`: always as its ceiling, and also as its floor when
        pinned (so iLO can't run it slower). The floor is never left above the
        ceiling: going down, the floor moves first."""
        floor_now = self.fan_floor.get(fan)
        def ceiling() -> None:
            if self.fan_applied.get(fan) != pct:
                self.fans.set_pwm(fan, pct, "max", remember=False)
                self.fan_applied[fan] = pct
        if pin:
            down = floor_now is not None and pct < floor_now
            if not down:
                ceiling()
            if self.floor_sent.get(fan) != pct or floor_now != pct:
                self.fans.set_pwm(fan, pct, "min", remember=False)
                self.fan_floor[fan] = self.floor_sent[fan] = pct
                if floor_now != pct:
                    self._save_floors()
            if down:
                ceiling()
        else:
            if floor_now is not None:
                getattr(self.fans, "clear_floor", lambda f: None)(fan)
                self.fan_floor.pop(fan, None)
                self.floor_sent.pop(fan, None)
                self._save_floors()
            ceiling()

    def _clear_floors(self) -> None:
        for fan in list(self.fan_floor):
            try:
                getattr(self.fans, "clear_floor", lambda f: None)(fan)
            except Exception:             # noqa: BLE001 - the next successful poll retries
                return
            self.floor_sent.pop(fan, None)
            self.fan_floor.pop(fan, None)
        self._save_floors()

    def _save_floors(self) -> None:
        data = self.learned.read()
        data["_floors"] = {str(k): v for k, v in self.fan_floor.items()}
        self.learned.write(data)

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
            "models": self.models,
            "mapping": self._mapping_status(),
            "fanmap": self.fanmap,
            "boost": ({"since": self.boost["since"], "reason": self.boost["reason"],
                       "levels": {str(f): v for f, v in self.boost["levels"].items()}} if self.boost else None),
        }

    def _mapping_status(self) -> Optional[Dict[str, Any]]:
        from . import fanmap as FM
        run = self.mapping
        if run is None:
            return None
        left = len(run["plan"]) - run["i"] - 1
        held = self.clock() - run["step_started"]
        return {"step": run["i"] + 1, "of": len(run["plan"]), "fan": run["plan"][run["i"]],
                "base": {str(f): v for f, v in run["base"].items()}, "bump_pct": FM.BUMP_PCT,
                "left_s": int(max(0, FM.STEP_FIT_MIN_S - held) + FM.STEP_FIT_MIN_S * left),
                "max_left_s": int(max(0, FM.STEP_MAX_S - held) + FM.STEP_MAX_S * left)}
