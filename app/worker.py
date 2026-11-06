"""One background worker per server, and the fleet that owns them.

The worker is the only thing that talks to its iLO on a schedule. Browsers
and the phone app read the snapshot it leaves behind, so opening more tabs
no longer means more load on the iLO.

Keeping fan settings alive across reboots
-----------------------------------------
iLO forgets every patched fan setting when the host goes through a power
cycle or POST, and when the iLO itself restarts. The old version cached
"I already wrote 30% to fan 2" and never wrote it again, which is why the
curve had to be switched off and on after every reboot. The worker now
re-sends the settings (curve or manual) whenever it sees:

  * the host's PowerState or PostState change (off -> on, POST -> finished),
    plus two follow-up passes 1 and 3 minutes after the host comes up;
  * the SSH session to the iLO having to log in again;
  * fans running well above the ceiling we set for several polls in a row;
  * and, as a safety net, every `reassert_seconds` (default 5 minutes).
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from .curve import CurveEngine
from .fans import FanControl
from .history_db import history_db
from .influx import influx
from .redfish import Redfish
from .servers import registry
from .ssh_client import IloShell, IloSshError

RECENT_LEN = 720               # in-memory samples for sparklines and the trend
DOWN_AFTER = 3                 # failed polls in a row before the link counts as down
DRIFT_MARGIN = 15              # % above the ceiling that counts as "iLO dropped it"
DRIFT_TICKS = 3
FOLLOW_UPS = (60, 180)         # extra re-sends after the host powers on, seconds
CONNECTION_KEYS = ("host", "username", "password", "ssh_port", "https_port", "verify_tls")


class ServerWorker(threading.Thread):
    daemon = True

    def __init__(self, cfg: Dict[str, Any]) -> None:
        super().__init__(name=f"srv-{cfg['id']}")
        self.cfg = cfg
        self.sid = cfg["id"]
        self.rf = Redfish(cfg)
        self.shell = IloShell(cfg)
        self.fans = FanControl(self.sid, self.shell, cfg, self.fan_count)
        self.curve = CurveEngine(self.sid, self.fans, self.log)
        self._stop = threading.Event()
        self._wake = threading.Event()

        self.thermal: Optional[Dict[str, Any]] = None
        self.power: Optional[Dict[str, Any]] = None
        self.system: Optional[Dict[str, Any]] = None
        self.manager: Optional[Dict[str, Any]] = None
        self.recent: Deque[Dict[str, Any]] = deque(maxlen=RECENT_LEN)
        self.events: Deque[Dict[str, Any]] = deque(maxlen=40)
        try:        # pick up where we left off, so a restart doesn't blank the Activity card
            now = int(time.time())
            self.events.extend(history_db.events(self.sid, now - 7 * 86400, now, 40))
        except Exception:      # noqa: BLE001
            pass
        self.snapshot: Dict[str, Any] = {}
        self.seq = 0

        self.fails = 0
        self.link = "connecting"
        self.last_ok: Optional[float] = None
        self.last_error: Optional[str] = None
        self.last_poll: Optional[float] = None

        self._fan_count = 6
        self._tick = 0
        self._last_state: Optional[tuple] = None
        self._seen_connects = 0
        self._last_reassert = time.time()
        self._follow_ups: List[float] = []
        self._manual_pending = True     # re-apply stored manual limits on start
        self._drift: Dict[int, int] = {}
        self._last_drift_fix = 0.0
        self._build_snapshot()

    # ------------------------------------------------------------------ #
    def fan_count(self) -> int:
        return self._fan_count

    def log(self, kind: str, message: str) -> None:
        ev = {"ts": time.time(), "kind": kind, "message": message}
        self.events.appendleft(ev)
        try:
            history_db.event(self.sid, kind, message, ev["ts"])
        except Exception:      # noqa: BLE001
            pass

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def poke(self) -> None:
        """Run the next poll now instead of waiting for the interval."""
        self._wake.set()

    def apply_now(self) -> Optional[str]:
        """Send the current settings to the fans right away, from the last readings,
        instead of waiting for the next poll. Returns an error message or None."""
        if not self.thermal or self.curve.tripped:
            return None
        if (self.system or {}).get("power_state") not in (None, "On"):
            return None                       # sent when the host powers on
        try:
            if self.curve.config(self._fan_count)["enabled"]:
                self.curve.step(self.thermal["temperatures"], self._fan_count)
            else:
                stored = self.fans.state.read()
                if stored.get("ceilings") or stored.get("floors"):
                    self._apply_manual(stored)
            self._manual_pending = False
            return None
        except Exception as exc:      # noqa: BLE001
            self.curve.last_error = f"{type(exc).__name__}: {exc}"
            return self.curve.last_error
        finally:
            self._build_snapshot()

    def profile_inputs(self, pid: str) -> Optional[Dict[str, Any]]:
        """What the model for one curve learns from: its sensors, its fans, its limit."""
        from .curve import hot_sensor, sensor_limit
        cfg = self.curve.config(self._fan_count)
        prof = next((p for p in cfg["profiles"] if p["id"] == pid), None)
        if not prof:
            return None
        known = history_db.names(self.sid).get("temp", [])
        sel = prof["sensor"]
        if sel == "__cpu__":
            sensors = [n for n in known if "cpu" in n.lower() or "proc" in n.lower()]
        elif sel == "__hottest__":
            sensors = known
        else:
            sensors = [n for n in known if n.lower() == sel.lower()] or \
                      [n for n in known if sel.lower() in n.lower()]
        fans_live = (self.thermal or {}).get("fans", [])
        temps = (self.thermal or {}).get("temperatures", [])
        name, _ = hot_sensor(temps, sel)
        # With a fan map, the model weighs every fan by how much it cools this
        # sensor, instead of assuming only the curve's own fans matter.
        from .fanmap import weights
        airflow = None
        w = weights(self.curve.fanmap, name)
        if w and all(int(k) < len(fans_live) for k in w):
            airflow = {"names": {str(i): f["name"] for i, f in enumerate(fans_live)}, "w": w,
                       "own": [int(i) for i, p in cfg["assign"].items() if p == pid]}
        return {"prof": prof, "sensors": sensors, "airflow": airflow,
                "fans": [fans_live[int(i)]["name"] for i, p in cfg["assign"].items()
                         if p == pid and int(i) < len(fans_live)],
                "limit": sensor_limit(temps, name),
                "margin": float(self.cfg.get("watchdog_margin_c", 5)),
                "floor": int(self.cfg.get("min_pwm_percent", 0))}

    def update_settings(self, cfg: Dict[str, Any]) -> None:
        """Take new limits (minimum speed, poll interval, watchdog…) without a restart."""
        self.cfg.update(cfg)
        self.reassert()
        self.apply_now()
        self.poke()

    def reassert(self, reason: Optional[str] = None) -> None:
        """Make the next poll re-send whatever should be on the fans."""
        self.curve.reset_applied()
        self._manual_pending = True
        self._last_reassert = time.time()
        if reason:
            self.log("reassert", reason)

    # ------------------------------------------------------------------ #
    def run(self) -> None:
        try:
            p = self.shell.probe()
            self._seen_connects = self.shell.connects
            if p["ssh"] and not p["patched"]:
                self.log("info", "SSH works but the `fan` command doesn't — this iLO looks unpatched.")
        except Exception:      # noqa: BLE001
            pass
        while not self._stop.is_set():
            started = time.time()
            try:
                self._poll()
            except Exception as exc:      # noqa: BLE001 - never let the thread die
                self.last_error = f"{type(exc).__name__}: {exc}"
            self._build_snapshot()
            interval = int(self.cfg.get("poll_seconds", 10))
            if self.fails >= DOWN_AFTER:  # back off while the iLO is unreachable
                interval = min(60, interval * 2 ** min(3, self.fails - DOWN_AFTER + 1))
            self._wake.wait(max(1.0, interval - (time.time() - started)))
            self._wake.clear()
        self.rf.logout()
        self.shell.close()

    def _poll(self) -> None:
        now = time.time()
        self._tick += 1
        self.last_poll = now
        try:
            self.thermal = self.rf.thermal()
        except Exception as exc:          # noqa: BLE001
            self._failed(exc)
            return
        self._succeeded()

        try:
            self.power = self.rf.power()
        except Exception:                 # noqa: BLE001 - not worth failing the poll over
            pass
        sysd = self.system or {}
        settling = (sysd.get("power_state") != "On"
                    or sysd.get("post_state") not in (None, "Unknown", "FinishedPost"))
        if self.system is None or settling or self._tick % 3 == 0:
            try:
                self.system = self.rf.system()
            except Exception:             # noqa: BLE001
                pass
        if self.manager is None or self._tick % 360 == 0:
            self.manager = self.rf.manager()

        fans_live = self.thermal["fans"]
        if fans_live:
            self._fan_count = len(fans_live)
        self.curve.fan_actual = {f["index"]: f["reading"] for f in fans_live
                                 if isinstance(f.get("reading"), (int, float))}
        live: Dict[str, float] = {}
        from .tuner import pick_inlet
        inlet_name = pick_inlet([t["name"] for t in self.thermal["temperatures"]])
        inlet = next((t["reading"] for t in self.thermal["temperatures"] if t["name"] == inlet_name), None)
        if isinstance(inlet, (int, float)):
            live["inlet"] = float(inlet)
        if isinstance((self.power or {}).get("consumed_watts"), (int, float)):
            live["power"] = float(self.power["consumed_watts"])
        self.curve.live = live
        temps = self.thermal["temperatures"]

        self._watch_power_state(now)
        if self.shell.connects != self._seen_connects:
            # A fresh SSH login. If the iLO restarted it lost the fan settings.
            if self._seen_connects:
                self.reassert()
            self._seen_connects = self.shell.connects
        if self._follow_ups and now >= self._follow_ups[0]:
            self._follow_ups.pop(0)
            self.reassert()
        every = int(self.cfg.get("reassert_seconds", 300))
        if every and now - self._last_reassert >= every:
            self.reassert()

        curve_cfg = self.curve.config(self._fan_count)
        stored = self.fans.state.read()
        has_manual = bool(stored.get("ceilings") or stored.get("floors"))
        self.curve.watchdog(temps, self.cfg, managed=curve_cfg["enabled"] or has_manual)

        powered = (self.system or {}).get("power_state") in (None, "On")
        if not powered:
            # Nothing sticks while the host is off; send it all once it's back.
            self.curve.reset_applied()
            self._manual_pending = True
        elif not self.curve.tripped:
            try:
                if curve_cfg["enabled"]:
                    self._check_drift(fans_live, now)
                    self.curve.step(temps, self._fan_count)
                    self._manual_pending = False
                elif self._manual_pending and has_manual:
                    self._apply_manual(stored)
                    self._manual_pending = False
                else:
                    self._manual_pending = False
            except IloSshError as exc:
                self.curve.last_error = f"SSH: {exc}"
            except Exception as exc:      # noqa: BLE001
                self.curve.last_error = f"{type(exc).__name__}: {exc}"

        self._record(now, fans_live, temps)

    # ------------------------------------------------------------------ #
    def _failed(self, exc: Exception) -> None:
        self.fails += 1
        self.last_error = str(exc)
        if self.fails == DOWN_AFTER:
            self.link = "down"
            self.log("link", f"Lost contact with the iLO: {exc}")
        elif self.fails < DOWN_AFTER:
            self.link = "degraded" if self.last_ok else "connecting"

    def _succeeded(self) -> None:
        if self.link == "down":
            gone = int(time.time() - (self.last_ok or time.time()))
            self.log("link", f"iLO is reachable again (was out for ~{max(1, gone // 60)} min).")
        self.fails = 0
        self.link = "ok"
        self.last_ok = time.time()
        self.last_error = None

    def _watch_power_state(self, now: float) -> None:
        sysd = self.system or {}
        state = (sysd.get("power_state"), sysd.get("post_state"))
        if state == (None, None):
            return
        prev = self._last_state
        self._last_state = state
        if prev is None or prev == state:
            return
        if state[0] != prev[0]:
            self.log("power", f"Host power {prev[0] or '?'} → {state[0] or '?'}.")
        if state[0] == "On" and (prev[0] != "On" or state[1] != prev[1]):
            self._follow_ups = [now + d for d in FOLLOW_UPS]
            self.reassert(f"Host {'finished POST' if state[1] == 'FinishedPost' else 'is starting'}"
                          " — fan settings will be sent again.")
        else:
            self.reassert()

    def _check_drift(self, fans_live: List[Dict[str, Any]], now: float) -> None:
        for fan, target in list(self.curve.fan_applied.items()):
            reading = fans_live[fan]["reading"] if fan < len(fans_live) else None
            if isinstance(reading, (int, float)) and reading > target + DRIFT_MARGIN:
                self._drift[fan] = self._drift.get(fan, 0) + 1
            else:
                self._drift.pop(fan, None)
        if any(n >= DRIFT_TICKS for n in self._drift.values()) and now - self._last_drift_fix > 120:
            self._last_drift_fix = now
            self._drift.clear()
            self.reassert("Fans were running well above the curve — looks like iLO dropped "
                          "the settings, so they were sent again.")

    def _apply_manual(self, stored: Dict[str, Any]) -> None:
        mode = stored.get("mode", "max")
        with self.curve._lock:
            for i, pct in (stored.get("ceilings") or {}).items():
                self.fans.set_pwm(int(i), pct, mode if mode != "min" else "max", remember=False)
            for i, pct in (stored.get("floors") or {}).items():
                self.fans.set_pwm(int(i), pct, "min", remember=False)

    def _record(self, now: float, fans_live, temps) -> None:
        rows = [("temp", t["name"], t["reading"]) for t in temps]
        rows += [("fan", f["name"], f["reading"]) for f in fans_live
                 if isinstance(f.get("reading"), (int, float))]
        watts = (self.power or {}).get("consumed_watts")
        if isinstance(watts, (int, float)):
            rows.append(("power", "system", watts))
        for st in self.curve.profile_state.values():
            rows.append(("target", st["name"], st["applied"]))
            # The temperature each curve was steering by: paired with "target"
            # above, this is what the curve suggestion learns from.
            rows.append(("drive", st["name"], st["temp"]))
        try:
            history_db.record(self.sid, now, rows)
        except Exception:      # noqa: BLE001 - history is best-effort
            pass
        influx.enqueue(now, self.cfg["name"], rows)
        self.recent.append({"ts": now,
                            "temps": {t["name"]: t["reading"] for t in temps},
                            "fans": {f["name"]: f["reading"] for f in fans_live},
                            "watts": watts})

    # ------------------------------------------------------------------ #
    def _build_snapshot(self) -> None:
        self.seq += 1
        fs = self.fans.snapshot()
        self.snapshot = {
            "id": self.sid, "name": self.cfg["name"], "host": self.cfg["host"],
            "seq": self.seq, "enabled": True,
            "link": {"redfish": self.link, "fails": self.fails, "last_ok": self.last_ok,
                     "last_poll": self.last_poll, "error": self.last_error,
                     "ssh": self.shell.connected, "patched": self.shell.patched,
                     "ssh_error": self.shell.last_error, "fan_source": self.rf.fan_source},
            "thermal": self.thermal, "power": self.power,
            "system": self.system, "manager": self.manager,
            "fan_count": self._fan_count,
            "fan_state": {"ceilings": fs["ceilings"], "floors": fs["floors"], "mode": fs["mode"]},
            "curve": self.curve.status(self._fan_count),
            "events": list(self.events)[:12],
            "guards": {"min_pwm_percent": self.cfg.get("min_pwm_percent"),
                       "watchdog_enabled": self.cfg.get("watchdog_enabled"),
                       "watchdog_margin_c": self.cfg.get("watchdog_margin_c"),
                       "poll_seconds": self.cfg.get("poll_seconds")},
        }


def disabled_snapshot(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": cfg["id"], "name": cfg["name"], "host": cfg["host"], "seq": 0,
            "enabled": False, "link": {"redfish": "disabled"}, "thermal": None,
            "power": None, "system": None, "curve": None, "events": []}


class Fleet:
    def __init__(self) -> None:
        self.workers: Dict[str, ServerWorker] = {}
        self._lock = threading.Lock()

    def start_all(self) -> None:
        for cfg in registry.all():
            if cfg.get("enabled", True):
                self._start(cfg)

    def _start(self, cfg: Dict[str, Any]) -> None:
        w = ServerWorker(cfg)
        with self._lock:
            self.workers[cfg["id"]] = w
        w.start()

    def _stop(self, sid: str) -> Optional[ServerWorker]:
        with self._lock:
            w = self.workers.pop(sid, None)
        if w:
            w.stop()
        return w

    def get(self, sid: str) -> ServerWorker:
        w = self.workers.get(sid)
        if w is None:
            raise KeyError(sid)
        return w

    def snapshots(self) -> List[Dict[str, Any]]:
        out = []
        for cfg in registry.all():
            w = self.workers.get(cfg["id"])
            out.append(w.snapshot if w else disabled_snapshot(cfg))
        return out

    def added(self, cfg: Dict[str, Any]) -> None:
        if cfg.get("enabled", True):
            self._start(cfg)

    def changed(self, cfg: Dict[str, Any]) -> None:
        w = self.workers.get(cfg["id"])
        if w and cfg.get("enabled", True) and all(w.cfg.get(k) == cfg.get(k) for k in CONNECTION_KEYS):
            w.update_settings(cfg)             # same iLO, new limits: no restart needed
            return
        old = self._stop(cfg["id"])
        if old:
            old.join(timeout=15)
        if cfg.get("enabled", True):
            self._start(cfg)

    def removed(self, sid: str, restore: bool) -> Optional[str]:
        w = self.workers.get(sid)
        err = None
        if w and restore:
            try:
                w.fans.restore_auto()
            except Exception as exc:      # noqa: BLE001
                err = str(exc)
        self._stop(sid)
        return err

    def stop_all(self) -> None:
        for sid in list(self.workers):
            self._stop(sid)


fleet = Fleet()
