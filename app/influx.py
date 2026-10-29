"""Optional export of every server's samples to an external InfluxDB.

Supports InfluxDB 1.x (`/write?db=...`) and 2.x (`/api/v2/write` with a
token). Writes are queued and sent from a background thread in batches, so a
slow or unreachable Influx never blocks fan control.

Schema: measurement=<configurable, default "ilo4">
        tags:  host=<server name>, kind=temp|fan|power|target, name=<sensor>
        field: value=<float>
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from .config import DATA_DIR, JsonStore

influx_store = JsonStore(DATA_DIR / "influx.json", {
    "enabled": False,
    "version": 1,
    "url": "http://localhost:8086",
    "verify_tls": True,
    "measurement": "ilo4",
    "database": "ilo4", "username": "", "password": "",      # v1
    "org": "", "bucket": "ilo4", "token": "",                 # v2
}, mode=0o600)

SECRET_FIELDS = ("token", "password")
MASK = "•" * 8


def redact(cfg: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(cfg)
    for f in SECRET_FIELDS:
        if out.get(f):
            out[f] = MASK
    return out


def merge_secret(new_cfg: Dict[str, Any], saved_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """A masked placeholder coming back from the UI must not overwrite the real secret."""
    out = dict(new_cfg)
    for f in SECRET_FIELDS:
        if out.get(f) == MASK:
            out[f] = saved_cfg.get(f, "")
    return out


def _esc_tag(v: Any) -> str:
    return str(v).replace("\\", "\\\\").replace(",", "\\,").replace(" ", "\\ ").replace("=", "\\=")


def line(measurement: str, tags: Dict[str, str], value: float, ts_ns: int) -> str:
    tag_str = "".join(f",{k}={_esc_tag(v)}" for k, v in tags.items() if v not in (None, ""))
    return f"{_esc_tag(measurement)}{tag_str} value={float(value)} {ts_ns}"


def _post(cfg: Dict[str, Any], body: str) -> requests.Response:
    data = body.encode("utf-8")
    if int(cfg.get("version", 1)) == 1:
        auth = (cfg["username"], cfg["password"]) if cfg.get("username") else None
        return requests.post(cfg["url"].rstrip("/") + "/write", data=data, auth=auth,
                             params={"db": cfg.get("database") or "ilo4", "precision": "ns"},
                             timeout=10, verify=cfg.get("verify_tls", True))
    return requests.post(cfg["url"].rstrip("/") + "/api/v2/write", data=data,
                         params={"org": cfg.get("org", ""), "bucket": cfg.get("bucket") or "ilo4",
                                 "precision": "ns"},
                         headers={"Authorization": f"Token {cfg.get('token', '')}"},
                         timeout=10, verify=cfg.get("verify_tls", True))


class InfluxExporter(threading.Thread):
    daemon = True
    BATCH_MAX = 1000
    FLUSH_SECONDS = 5

    def __init__(self) -> None:
        super().__init__(name="influx-export")
        self._q: "queue.Queue[str]" = queue.Queue(maxsize=50000)
        self._stop = threading.Event()
        self.last_error: Optional[str] = None
        self.last_ok: Optional[float] = None
        self.sent_total = 0
        self.dropped_total = 0

    def stop(self) -> None:
        self._stop.set()

    def enqueue(self, ts: float, host: str, rows: Iterable[Tuple[str, str, float]]) -> None:
        cfg = influx_store.read()
        if not cfg.get("enabled"):
            return
        ts_ns, meas = int(ts * 1e9), cfg.get("measurement") or "ilo4"
        for kind, name, v in rows:
            if not isinstance(v, (int, float)):
                continue
            try:
                self._q.put_nowait(line(meas, {"host": host, "kind": kind, "name": name}, v, ts_ns))
            except queue.Full:
                self.dropped_total += 1

    def run(self) -> None:
        batch: List[str] = []
        last_flush = time.time()
        while not self._stop.is_set():
            try:
                batch.append(self._q.get(timeout=1.0))
            except queue.Empty:
                pass
            if batch and (len(batch) >= self.BATCH_MAX
                          or time.time() - last_flush >= self.FLUSH_SECONDS):
                self._flush(batch)
                batch, last_flush = [], time.time()
        if batch:
            self._flush(batch)

    def _flush(self, batch: List[str]) -> None:
        cfg = influx_store.read()
        if not cfg.get("enabled"):
            return
        try:
            r = _post(cfg, "\n".join(batch))
            if r.status_code >= 300:
                self.last_error = f"InfluxDB {r.status_code}: {r.text[:200]}"
            else:
                self.last_error, self.last_ok = None, time.time()
                self.sent_total += len(batch)
        except requests.RequestException as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"

    def status(self) -> Dict[str, Any]:
        return {"config": redact(influx_store.read()), "queued": self._q.qsize(),
                "sent_total": self.sent_total, "dropped_total": self.dropped_total,
                "last_ok": self.last_ok, "last_error": self.last_error}

    def test(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        probe = line(cfg.get("measurement") or "ilo4",
                     {"host": "test", "kind": "probe", "name": "connectivity"},
                     1.0, int(time.time() * 1e9))
        try:
            r = _post(cfg, probe)
            if r.status_code < 300:
                return {"ok": True}
            return {"ok": False, "detail": f"HTTP {r.status_code}: {r.text[:300]}"}
        except requests.RequestException as exc:
            return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}


influx = InfluxExporter()
