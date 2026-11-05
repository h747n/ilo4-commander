"""Keeps every curve's model up to date, all the time.

Every 20 minutes - and soon after a Learn run finishes - it refits the model
for every curve on every server from up to 30 days of history (which keeps
growing with live data), grades it on recent data it hasn't seen, and hands
it to the curve engine. Adaptive mode then steers with it, the panel shows
how good it is, and a ready-made suggestion is kept alongside: the curve the
designer would draw now, and how it compares with the curve in use.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict

from . import tuner

REFIT_EVERY_S = 1200
FIRST_RUN_S = 90
HISTORY_DAYS = 30


class Learner(threading.Thread):
    daemon = True

    def __init__(self, fleet) -> None:
        super().__init__(name="learner")
        self.fleet = fleet
        self._stop = threading.Event()
        self.last_run: Dict[str, float] = {}

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        self._stop.wait(FIRST_RUN_S)
        while not self._stop.is_set():
            for sid, w in list(self.fleet.workers.items()):
                due = time.time() - self.last_run.get(sid, 0) >= REFIT_EVERY_S
                if due or w.curve.relearn_wanted:
                    w.curve.relearn_wanted = False
                    try:
                        self.refit(w)
                    except Exception as exc:      # noqa: BLE001 - never stop learning over one server
                        w.curve.last_error = f"Model update failed: {exc}"
                    self.last_run[sid] = time.time()
            self._stop.wait(60)

    def refit(self, w) -> None:
        cfg = w.curve.config(w.fan_count())
        for prof in cfg["profiles"]:
            inp = w.profile_inputs(prof["id"])
            if not inp or not inp["sensors"]:
                continue
            data = tuner.collect(w.sid, inp["sensors"], inp["fans"], prof["name"], HISTORY_DAYS)
            m = tuner.learn(data)
            if not m.get("error"):
                # Quietest curve within the maximum the user set (or a sensible default).
                target = prof["target_c"] if (prof.get("mode") == "target" or prof.get("target_set")) else None
                d = tuner.design(m, target, inp["limit"], inp["margin"], inp["floor"])
                if d.get("points"):
                    now_t, now_f = tuner.settle(m, prof["points"], m["power"][1], m["inlet"][1])
                    m["suggestion"] = {"points": d["points"], "target": d["target"],
                                       "typical": d["predict"]["typical"],
                                       "current_typical": {"temp": now_t, "fan": now_f},
                                       "adaptive_typical": d["adaptive"]["typical"],
                                       "prefer": d.get("prefer", "curve")}
            m["updated_at"] = int(time.time())
            w.curve.set_model(prof["id"], m)
