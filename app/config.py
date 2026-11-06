"""Process-wide settings and the small JSON persistence layer.

Everything that belongs to one iLO (address, credentials, curve, presets)
lives in servers.py and under data/servers/<id>/. What's left here is the
panel itself: where it listens, the login, and the defaults new servers get.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict

VERSION = "3.3.2"

DATA_DIR = Path(os.environ.get("ILO_DATA_DIR", "./data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _b(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


class Settings:
    bind_host: str = os.environ.get("BIND_HOST", "0.0.0.0")
    bind_port: int = int(os.environ.get("BIND_PORT", "8080"))
    ui_password: str = os.environ.get("UI_PASSWORD", "")  # empty = no login
    # Login lifetime. Long by default so the phone app doesn't keep asking.
    session_days: int = int(os.environ.get("SESSION_DAYS", "30"))

    # Defaults for servers added later; each server can override them.
    default_poll_seconds: int = int(os.environ.get("POLL_SECONDS", "10"))
    default_min_pwm_percent: int = int(os.environ.get("MIN_PWM_PERCENT", "8"))
    default_watchdog_margin_c: int = int(os.environ.get("WATCHDOG_MARGIN_C", "5"))

    # Free-form `fan ...` commands on the Tuning tab.
    allow_raw_fan: bool = _b("ALLOW_RAW_FAN", True)


settings = Settings()

# PWM registers on the patched iLO4 are 8-bit: 0 = stopped, 255 = full tilt.
PWM_MAX = 255


def pct_to_pwm(percent: float) -> int:
    return max(0, min(PWM_MAX, round(float(percent) * PWM_MAX / 100.0)))


def pwm_to_pct(pwm: float) -> int:
    return max(0, min(100, round(float(pwm) * 100.0 / PWM_MAX)))


class JsonStore:
    """Tiny thread-safe JSON document, one file per topic."""

    def __init__(self, path: Path, default: Dict[str, Any], mode: int = 0o644):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._default = default
        self._mode = mode
        if not self.path.exists():
            self.write(default)

    def read(self) -> Dict[str, Any]:
        with self._lock:
            try:
                data = json.loads(self.path.read_text("utf-8"))
            except Exception:
                return json.loads(json.dumps(self._default))
            merged = json.loads(json.dumps(self._default))
            merged.update(data if isinstance(data, dict) else {})
            return merged

    def write(self, data: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")
            try:
                os.chmod(tmp, self._mode)
            except OSError:
                pass
            tmp.replace(self.path)
            return data


history_store = JsonStore(DATA_DIR / "history-settings.json", {"raw_retention_hours": 48})
