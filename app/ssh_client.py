"""Interactive SSH session against one iLO 4 CLI.

iLO 4 does not accept `exec` channels, so every command goes through a
long-lived interactive shell whose prompt (`</>hpiLO->`) we watch for.
The unlocked firmware adds `fan` to that shell - the command this module
exists to reach.
"""
from __future__ import annotations

import re
import socket
import threading
import time
from typing import Any, Dict, List, Optional

import paramiko

PROMPT = re.compile(r"</>hpiLO->\s*$")
ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


class IloSshError(RuntimeError):
    pass


class IloShell:
    """One shell per iLO, guarded by a lock, reconnecting on demand."""

    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg
        self._lock = threading.RLock()
        self._client: Optional[paramiko.SSHClient] = None
        self._chan: Optional[paramiko.Channel] = None
        self.last_error: Optional[str] = None
        # Bumped on every fresh login. A new login after the shell was lost
        # usually means the iLO itself restarted, and a restarted iLO has
        # forgotten every fan setting - the worker watches this number.
        self.connects = 0
        self.patched: Optional[bool] = None

    # ------------------------------------------------------------------ #
    def _connect(self) -> None:
        self.close()
        kwargs = dict(
            hostname=self.cfg["host"],
            port=int(self.cfg.get("ssh_port", 22)),
            username=self.cfg["username"],
            password=self.cfg["password"],
            look_for_keys=False,
            allow_agent=False,
            timeout=12,
            banner_timeout=25,
            auth_timeout=25,
        )
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(**kwargs)
        except paramiko.AuthenticationException:
            raise
        except paramiko.SSHException:
            # Gen8 iLOs speak old crypto; retry without the modern-only set.
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                disabled_algorithms={
                    "pubkeys": ["rsa-sha2-256", "rsa-sha2-512"],
                    "kex": ["curve25519-sha256@libssh.org"],
                },
                **kwargs,
            )
        transport = client.get_transport()
        if transport:
            transport.set_keepalive(30)
        chan = client.invoke_shell(width=200, height=200)
        chan.settimeout(20)
        self._client, self._chan = client, chan
        self._read_until_prompt(timeout=20)
        self.connects += 1
        self.last_error = None

    def _read_until_prompt(self, timeout: int = 20) -> str:
        assert self._chan is not None
        buf, deadline = "", time.time() + timeout
        while time.time() < deadline:
            try:
                if self._chan.recv_ready():
                    buf += self._chan.recv(65535).decode("utf-8", "replace")
                else:
                    time.sleep(0.05)
            except socket.timeout:
                break
            if PROMPT.search(buf.rstrip()):
                break
        return ANSI.sub("", buf).replace("\r\n", "\n").replace("\r", "\n")

    def close(self) -> None:
        for obj in (self._chan, self._client):
            try:
                if obj:
                    obj.close()
            except Exception:      # noqa: BLE001
                pass
        self._chan = self._client = None

    @property
    def connected(self) -> bool:
        return bool(self._chan and self._chan.active and not self._chan.closed)

    # ------------------------------------------------------------------ #
    def run(self, command: str, timeout: int = 8) -> str:
        """Send one command, return its output with echo and prompt stripped."""
        command = command.strip()
        if not command:
            return ""
        with self._lock:
            for attempt in (1, 2):
                try:
                    if not self.connected:
                        self._connect()
                    assert self._chan is not None
                    while self._chan.recv_ready():          # drain stale bytes
                        self._chan.recv(65535)
                    self._chan.send(command + "\n")
                    raw = self._read_until_prompt(timeout)
                    if not PROMPT.search(raw.rstrip()):
                        # No prompt back: the session is stuck or half-dead. Don't
                        # pretend it worked - drop it and retry on a fresh one.
                        raise TimeoutError(f"no prompt after {timeout}s for {command!r}")
                    return self._clean(raw, command)
                except paramiko.AuthenticationException as exc:
                    self.last_error = "iLO rejected the SSH username or password."
                    self.close()
                    raise IloSshError(self.last_error) from exc
                except Exception as exc:                    # noqa: BLE001
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    self.close()
                    if attempt == 2:
                        raise IloSshError(self.last_error) from exc
                    time.sleep(0.7)
        return ""

    @staticmethod
    def _clean(raw: str, command: str) -> str:
        lines = [ln.rstrip() for ln in raw.split("\n")]
        while lines and not lines[0].strip():
            lines.pop(0)
        if lines and command in lines[0]:
            lines = lines[1:]
        while lines and PROMPT.search(lines[-1].strip()):
            lines.pop()
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        return "\n".join(lines)

    def probe(self) -> Dict[str, Any]:
        """Is this iLO actually running unlocked firmware?"""
        try:
            out = self.run("fan info")
        except IloSshError as exc:
            return {"ssh": False, "patched": False, "error": str(exc)}
        # Stock firmware answers with an error. Patched builds often print the
        # `fan info` tables to their debug console instead of the SSH session,
        # so silence counts as patched.
        low = out.lower()
        bad = ("invalid" in low or "unknown command" in low or "not recognized" in low
               or "not found" in low or "status=2" in low)
        self.patched = not bad
        return {"ssh": True, "patched": not bad, "error": None}


# ---------------------------------------------------------------------- #
# Parsers.  The unlocked firmware's text output is undocumented and varies
# between builds, so every parser is tolerant: it pulls out what it can and
# the raw text always travels alongside it to the UI.
# ---------------------------------------------------------------------- #
_NUM = r"(-?\d+)"


def parse_pwm_table(raw: str) -> List[Dict[str, object]]:
    """Pull per-PWM rows out of `fan info p`."""
    out: List[Dict[str, object]] = []
    for line in raw.split("\n"):
        m = re.search(r"PWM\s*#?\s*(\d+)", line, re.I)
        if not m:
            continue
        entry: Dict[str, object] = {"index": int(m.group(1)), "raw": line.strip()}
        for key, pat in (
            ("cur", r"(?:cur|current|speed|val)\D{0,6}" + _NUM),
            ("min", r"\bmin\D{0,6}" + _NUM),
            ("max", r"\bmax\D{0,6}" + _NUM),
            ("lock", r"\block\D{0,6}" + _NUM),
        ):
            mm = re.search(pat, line, re.I)
            if mm:
                entry[key] = int(mm.group(1))
        out.append(entry)
    return out
