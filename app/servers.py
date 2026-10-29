"""The server registry: which iLOs this panel manages.

One JSON file (mode 600, it holds iLO passwords) lists every server. Each
server also gets its own folder for the things that are specific to it:
curve, presets and the last values written to its fans.
"""
from __future__ import annotations

import re
import secrets
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import DATA_DIR, JsonStore, settings

SERVERS_DIR = DATA_DIR / "servers"
MASK = "•" * 8

# Every field a server can carry, with its default. Anything else is dropped.
FIELDS: Dict[str, Any] = {
    "name": "",
    "host": "",
    "username": "Administrator",
    "password": "",
    "ssh_port": 22,
    "https_port": 443,
    "verify_tls": False,
    "enabled": True,
    "poll_seconds": settings.default_poll_seconds,
    "min_pwm_percent": settings.default_min_pwm_percent,
    "watchdog_enabled": True,
    "watchdog_margin_c": settings.default_watchdog_margin_c,
    # Re-send the curve to the fans this often even if nothing changed, in
    # case iLO dropped it without a power event we could see. 0 = off.
    "reassert_seconds": 300,
}


class ServerError(ValueError):
    pass


def _clean(raw: Dict[str, Any], base: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out = dict(base or FIELDS)
    for key, default in FIELDS.items():
        if key not in raw:
            continue
        val = raw[key]
        if key == "password" and (val in (None, "", MASK)):
            continue                          # blank/masked = keep the old one
        if isinstance(default, bool):
            val = bool(val)
        elif isinstance(default, int):
            try:
                val = int(val)
            except (TypeError, ValueError):
                raise ServerError(f"{key} must be a number")
        else:
            val = str(val).strip()
        out[key] = val

    out["host"] = re.sub(r"^https?://", "", out["host"]).strip("/ ")
    if not out["host"]:
        raise ServerError("The iLO address can't be empty.")
    if not out["name"]:
        out["name"] = out["host"]
    if not (1 <= out["ssh_port"] <= 65535 and 1 <= out["https_port"] <= 65535):
        raise ServerError("Ports must be between 1 and 65535.")
    out["poll_seconds"] = max(5, min(120, out["poll_seconds"]))
    out["min_pwm_percent"] = max(0, min(60, out["min_pwm_percent"]))
    out["watchdog_margin_c"] = max(0, min(30, out["watchdog_margin_c"]))
    out["reassert_seconds"] = 0 if out["reassert_seconds"] <= 0 else max(60, out["reassert_seconds"])
    return out


def _new_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:20] or "server"
    return f"{slug}-{secrets.token_hex(3)}"


class Registry:
    def __init__(self) -> None:
        self._store = JsonStore(DATA_DIR / "servers.json", {"servers": []}, mode=0o600)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    def all(self) -> List[Dict[str, Any]]:
        return [dict(FIELDS, **s) for s in self._store.read().get("servers", [])]

    def get(self, sid: str) -> Optional[Dict[str, Any]]:
        return next((s for s in self.all() if s["id"] == sid), None)

    def add(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        cfg = _clean(raw)
        if not cfg["password"]:
            raise ServerError("The iLO password can't be empty.")
        with self._lock:
            data = self._store.read()
            if any(s["host"] == cfg["host"] for s in data["servers"]):
                raise ServerError(f"{cfg['host']} is already in the list.")
            cfg["id"] = raw.get("id") or _new_id(cfg["name"])
            data["servers"].append(cfg)
            self._store.write(data)
        server_dir(cfg["id"]).mkdir(parents=True, exist_ok=True)
        return cfg

    def update(self, sid: str, raw: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            data = self._store.read()
            for i, s in enumerate(data["servers"]):
                if s["id"] == sid:
                    cfg = _clean(raw, dict(FIELDS, **s))
                    cfg["id"] = sid
                    if any(o["host"] == cfg["host"] and o["id"] != sid for o in data["servers"]):
                        raise ServerError(f"{cfg['host']} is already in the list.")
                    data["servers"][i] = cfg
                    self._store.write(data)
                    return cfg
        raise ServerError("No such server.")

    def remove(self, sid: str) -> None:
        with self._lock:
            data = self._store.read()
            data["servers"] = [s for s in data["servers"] if s["id"] != sid]
            self._store.write(data)
        shutil.rmtree(server_dir(sid), ignore_errors=True)

    def reorder(self, ids: List[str]) -> None:
        with self._lock:
            data = self._store.read()
            pos = {sid: i for i, sid in enumerate(ids)}
            data["servers"].sort(key=lambda s: pos.get(s["id"], len(pos)))
            self._store.write(data)


def server_dir(sid: str) -> Path:
    return SERVERS_DIR / sid


def public(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """What the browser may see: everything except the real password."""
    out = dict(cfg)
    out["password"] = MASK if cfg.get("password") else ""
    return out


registry = Registry()
