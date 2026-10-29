"""Everything that writes to one unlocked fan controller.

The patched firmware exposes the raw PWM registers. Three ways to steer them:

  max   - ceiling. iLO keeps its own curve underneath, it just can't exceed
          this. Safest, and what the automatic curve uses.
  min   - floor. Handy to stop a fan stalling.
  lock  - hard-fix the duty cycle. iLO can no longer ramp up on its own,
          so a locked fan plus a hot CPU is how you cook a board.

Values are percent in the UI and converted to the 0-255 register here.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Union

from .config import PWM_MAX, JsonStore, pct_to_pwm, pwm_to_pct
from .servers import server_dir
from .ssh_client import IloShell, parse_pwm_table

MODES = ("max", "min", "lock")
INFO_SECTIONS = {"": "all", "t": "temperature", "h": "tachometer",
                 "a": "algorithms", "g": "groups", "p": "pwm"}

DEFAULT_PRESETS = [
    {"name": "Silent", "speeds": {"all": 12}},
    {"name": "Quiet", "speeds": {"all": 20}},
    {"name": "Balanced", "speeds": {"all": 35}},
    {"name": "Full", "speeds": {"all": 100}},
]


class FanControl:
    def __init__(self, sid: str, shell: IloShell, cfg: Dict[str, Any],
                 fan_count: Callable[[], int]) -> None:
        self.shell = shell
        self.cfg = cfg
        self.fan_count = fan_count
        d = server_dir(sid)
        self.presets = JsonStore(d / "presets.json", {"presets": DEFAULT_PRESETS})
        # What this tool last wrote to each fan. `fan info p` output doesn't
        # parse reliably on every build, so it can't be the only record.
        self.state = JsonStore(d / "fan-state.json",
                               {"ceilings": {}, "floors": {}, "mode": "max"})

    def _clamp(self, percent: float) -> int:
        p = int(round(float(percent)))
        return max(int(self.cfg.get("min_pwm_percent", 8)), min(100, p))

    def _targets(self, fan: Union[int, str]) -> List[int]:
        return list(range(self.fan_count())) if str(fan).lower() == "all" else [int(fan)]

    # ------------------------------------------------------------------ #
    def set_pwm(self, fan: Union[int, str], percent: float, mode: str = "max",
                remember: bool = True) -> Dict[str, Any]:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        pct = self._clamp(percent)
        value = pct_to_pwm(pct)
        results = []
        for idx in self._targets(fan):
            cmd = f"fan p {idx} {mode} {value}"
            results.append({"fan": idx, "command": cmd, "output": self.shell.run(cmd)})
        if remember:
            self._remember(self._targets(fan), pct, mode)
        return {"mode": mode, "percent": pct, "pwm": value,
                "clamped": pct != int(round(float(percent))), "results": results}

    def _remember(self, indexes: List[int], pct: int, mode: str) -> None:
        data = self.state.read()
        bucket = data.setdefault("floors" if mode == "min" else "ceilings", {})
        for i in indexes:
            bucket[str(i)] = pct
        data["mode"] = mode
        self.state.write(data)

    def unlock(self, fan: Union[int, str] = "all") -> Dict[str, Any]:
        targets = self._targets(fan)
        res = []
        for i in targets:
            res.append({"fan": i, "output": self.shell.run(f"fan p {i} unlock")})
            self.shell.run(f"fan p {i} max {PWM_MAX}")
            self.shell.run(f"fan p {i} min 0")
        data = self.state.read()
        for key in ("ceilings", "floors"):
            for i in targets:
                data.get(key, {}).pop(str(i), None)
        self.state.write(data)
        return {"results": res}

    def restore_auto(self) -> Dict[str, Any]:
        """Panic button: drop every manual limit and hand control back to iLO."""
        out = []
        for i in range(self.fan_count()):
            out.append(self.shell.run(f"fan p {i} unlock"))
            out.append(self.shell.run(f"fan p {i} max {PWM_MAX}"))
            out.append(self.shell.run(f"fan p {i} min 0"))
        out.append(self.shell.run("fan g start"))
        self.state.write({"ceilings": {}, "floors": {}, "mode": "max"})
        return {"restored": True, "output": "\n".join(o for o in out if o)}

    def snapshot(self, read_firmware: bool = False) -> Dict[str, Any]:
        """Ceiling per fan, as percent. Optionally overlaid with `fan info p`."""
        stored = self.state.read()
        ceilings = {int(k): int(v) for k, v in (stored.get("ceilings") or {}).items()}
        floors = {int(k): int(v) for k, v in (stored.get("floors") or {}).items()}
        from_firmware = False
        if read_firmware:
            try:
                for row in parse_pwm_table(self.shell.run("fan info p")):
                    val = row.get("max", row.get("cur"))
                    if isinstance(val, int):
                        ceilings[int(row["index"])] = pwm_to_pct(val)
                        from_firmware = True
            except Exception:      # noqa: BLE001 - the stored values still stand
                pass
        return {"ceilings": ceilings, "floors": floors,
                "from_firmware": from_firmware, "mode": stored.get("mode", "max")}

    # ------------------------------------------------------------------ #
    # Tuning-tab commands
    # ------------------------------------------------------------------ #
    def info(self, section: str = "") -> Dict[str, Any]:
        section = (section or "").strip().lower()
        if section not in INFO_SECTIONS:
            section = ""
        return {"section": INFO_SECTIONS[section],
                "raw": self.shell.run(f"fan info {section}".strip())}

    def pid(self, index: int, key: str, value: str) -> Dict[str, Any]:
        allowed = ("p", "i", "d", "sp", "imin", "imax", "lo", "hi")
        if key not in allowed:
            raise ValueError(f"pid key must be one of {allowed}")
        return self._cmd(f"fan pid {int(index)} {key} {_arg(value)}")

    def temp_sensor(self, index: int, key: str, value: Optional[str] = None) -> Dict[str, Any]:
        allowed = ("on", "off", "adj", "hyst", "caut", "crit", "set", "unset")
        if key not in allowed:
            raise ValueError(f"temperature key must be one of {allowed}")
        return self._cmd(f"fan t {int(index)} {key}" + (f" {_arg(value)}" if value not in (None, "") else ""))

    def tach(self, index: int, key: str, value: Optional[str] = None) -> Dict[str, Any]:
        allowed = ("on", "off", "min", "hyst")
        if key not in allowed:
            raise ValueError(f"tachometer key must be one of {allowed}")
        return self._cmd(f"fan h {int(index)} {key}" + (f" {_arg(value)}" if value not in (None, "") else ""))

    def global_op(self, op: str) -> Dict[str, Any]:
        allowed = ("start", "stop", "smsc", "ro", "rw", "nc", "")
        if op not in allowed:
            raise ValueError(f"global op must be one of {allowed}")
        return self._cmd(f"fan g {op}".strip())

    def poll_rate(self, ms: int) -> Dict[str, Any]:
        return self._cmd(f"fan rate {max(250, min(60000, int(ms)))}")

    def dump(self) -> Dict[str, Any]:
        return {"raw": self.shell.run("fan dump", timeout=30)}

    def raw(self, command: str) -> Dict[str, Any]:
        command = command.strip()
        if not re.match(r"^fan\b", command, re.I):
            command = "fan " + command
        if re.search(r"[\r\n;]", command):
            raise ValueError("One command at a time, please.")
        return {"command": command, "output": self.shell.run(command, timeout=30)}

    def _cmd(self, cmd: str) -> Dict[str, Any]:
        return {"command": cmd, "output": self.shell.run(cmd)}

    # ------------------------------------------------------------------ #
    # Presets
    # ------------------------------------------------------------------ #
    def list_presets(self) -> List[Dict[str, Any]]:
        return self.presets.read().get("presets", [])

    def save_presets(self, presets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        self.presets.write({"presets": presets})
        return presets

    def apply_preset(self, name: str, mode: str = "max") -> Dict[str, Any]:
        for p in self.list_presets():
            if p.get("name") == name:
                speeds = p.get("speeds", {})
                applied = []
                if "all" in speeds:
                    applied.append(self.set_pwm("all", speeds["all"], mode))
                for key, val in speeds.items():
                    if key != "all":
                        applied.append(self.set_pwm(int(key), val, mode))
                return {"preset": name, "applied": applied}
        raise KeyError(f"no preset named {name!r}")


def _arg(value: Any) -> str:
    """A single CLI token: numbers, dots, minus signs. Nothing that splits lines."""
    s = str(value).strip()
    if not re.fullmatch(r"[-+]?[0-9A-Za-z_.]{1,24}", s):
        raise ValueError(f"{s!r} isn't a value the fan controller takes.")
    return s
