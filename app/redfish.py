"""Redfish client for one iLO 4.

Only the reads this panel needs: thermal, power and system state. Fan writes
go over SSH (ssh_client.py) because Redfish on iLO 4 can't set fan speeds.

Why the old version kept dropping out
-------------------------------------
* Every request authenticated with HTTP Basic. iLO 4 runs a full login for
  each one, which is slow on its small CPU, and it piles up when several
  things poll at once.
* Every open browser tab ran its own polling loop against the iLO, on top
  of the curve thread. Two tabs plus a phone meant three times the load.
* iLO 4 silently drops idle keep-alive sockets, so the first request after a
  pause landed on a dead socket.

Now one worker per server is the only thing that talks to the iLO (browsers
read its cached snapshot), it logs in once and reuses a Redfish session
token, it drops pooled sockets that have been idle long enough for iLO to
have closed them, and a single failed poll no longer marks the link down.
"""
from __future__ import annotations

import ssl
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests
import urllib3
from requests.adapters import HTTPAdapter

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SYS = "/redfish/v1/Systems/1/"
MGR = "/redfish/v1/Managers/1/"
CHS = "/redfish/v1/Chassis/1/"
SESSIONS = "/redfish/v1/SessionService/Sessions/"

IDLE_SOCKET_S = 20          # iLO closes keep-alive sockets well before this
TIMEOUT = (6, 20)           # connect, read


class RedfishError(RuntimeError):
    pass


class LegacyTLSAdapter(HTTPAdapter):
    """iLO 4 ships a small self-signed cert and speaks old TLS.

    OpenSSL 3 refuses both by default (SECLEVEL=2, no TLS 1.0/1.1), which shows
    up as a connection error rather than anything that names TLS.
    """

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            ctx.set_ciphers("DEFAULT@SECLEVEL=1")
        except ssl.SSLError:
            pass
        try:
            ctx.minimum_version = ssl.TLSVersion.TLSv1
        except (ValueError, AttributeError):
            pass
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


def _oem(d: Dict[str, Any]) -> Dict[str, Any]:
    o = d.get("Oem") or {}
    return o.get("Hp") or o.get("Hpe") or {}


class Redfish:
    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg
        port = "" if int(cfg.get("https_port", 443)) == 443 else f":{cfg['https_port']}"
        self.base = f"https://{cfg['host']}{port}"
        self._s = self._new_session()
        self._net = threading.Lock()          # iLO 4 serves one request at a time anyway
        self._token: Optional[str] = None
        self._session_uri: Optional[str] = None
        self._basic_only = False              # set if this iLO refuses session login
        self._last_io = 0.0
        self.last_error: Optional[str] = None

    def _new_session(self) -> requests.Session:
        s = requests.Session()
        s.verify = bool(self.cfg.get("verify_tls"))
        s.headers.update({"OData-Version": "4.0", "Accept": "application/json"})
        if not s.verify:
            s.mount("https://", LegacyTLSAdapter(pool_connections=1, pool_maxsize=1))
        return s

    # ------------------------------------------------------------------ #
    # Session handling
    # ------------------------------------------------------------------ #
    def _login(self) -> None:
        """Get an X-Auth-Token. Falls back to Basic auth if the iLO says no."""
        self._token = self._session_uri = None
        if self._basic_only:
            return
        r = self._s.post(self.base + SESSIONS, timeout=TIMEOUT,
                         json={"UserName": self.cfg["username"],
                               "Password": self.cfg["password"]})
        if r.status_code in (200, 201) and r.headers.get("X-Auth-Token"):
            self._token = r.headers["X-Auth-Token"]
            self._session_uri = urlparse(r.headers.get("Location") or "").path or None
        elif r.status_code in (401, 403):
            raise RedfishError("iLO rejected the username or password.")
        else:
            # Old firmware without SessionService: plain Basic auth still works.
            self._basic_only = True

    def logout(self) -> None:
        if self._token and self._session_uri:
            try:
                with self._net:
                    self._s.delete(self.base + self._session_uri, timeout=(3, 5),
                                   headers={"X-Auth-Token": self._token})
            except Exception:      # noqa: BLE001 - best effort, it times out anyway
                pass
        self._token = self._session_uri = None
        try:
            self._s.close()
        except Exception:          # noqa: BLE001
            pass

    def _send(self, method: str, path: str, **kw) -> requests.Response:
        if time.time() - self._last_io > IDLE_SOCKET_S:
            # The pooled socket has very likely been closed by iLO already.
            self._s.close()
        if self._token is None and not self._basic_only:
            self._login()
        headers = kw.pop("headers", {})
        auth = None
        if self._token:
            headers["X-Auth-Token"] = self._token
        else:
            auth = (self.cfg["username"], self.cfg["password"])
        r = self._s.request(method, self.base + path, headers=headers, auth=auth,
                            timeout=TIMEOUT, **kw)
        self._last_io = time.time()
        return r

    def _request(self, method: str, path: str, **kw) -> requests.Response:
        attempts = 3 if method == "GET" else 2
        last: Optional[Exception] = None
        for attempt in range(attempts):
            try:
                with self._net:
                    r = self._send(method, path, **dict(kw))
                    if r.status_code == 401 and self._token:
                        # Session expired or iLO was reset: log in again once.
                        self._token = None
                        r = self._send(method, path, **dict(kw))
                self.last_error = None
                return r
            except RedfishError:
                raise
            except requests.RequestException as exc:
                last = exc
                self._s.close()               # next try opens a fresh socket
                if attempt + 1 < attempts:
                    time.sleep(0.5 * (attempt + 1))
        self.last_error = f"{type(last).__name__}: {last}"
        raise RedfishError(f"{method} {path}: no answer from iLO at {self.cfg['host']} "
                           f"({type(last).__name__})") from last

    def get(self, path: str) -> Dict[str, Any]:
        r = self._request("GET", path)
        if r.status_code >= 400:
            raise RedfishError(f"GET {path} -> {r.status_code} {r.text[:200]}")
        return r.json()

    def try_get(self, path: str) -> Optional[Dict[str, Any]]:
        try:
            return self.get(path)
        except Exception:      # noqa: BLE001
            return None

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    def thermal(self) -> Dict[str, Any]:
        d = self.get(f"{CHS}Thermal/")
        fans: List[Dict[str, Any]] = []
        # Keep "Absent" fans in the list: array position has to line up with
        # the PWM channel number used by `fan p <n>`.
        for i, f in enumerate(d.get("Fans", []) or []):
            st = f.get("Status") or {}
            fans.append({
                "index": i,
                "name": f.get("FanName") or f.get("Name") or f"Fan {i + 1}",
                "reading": f.get("CurrentReading", f.get("Reading")),
                "units": f.get("Units") or f.get("ReadingUnits") or "Percent",
                "state": st.get("State", "Unknown"),
                "health": st.get("Health", "Unknown"),
            })
        temps: List[Dict[str, Any]] = []
        for t in d.get("Temperatures", []):
            st = t.get("Status") or {}
            reading = t.get("ReadingCelsius")
            if st.get("State") == "Absent" or not isinstance(reading, (int, float)) or reading <= 0:
                continue
            temps.append({
                "name": (t.get("Name") or "?").strip(),
                "context": t.get("PhysicalContext"),
                "reading": reading,
                "caution": t.get("UpperThresholdNonCritical") or t.get("UpperThresholdUser"),
                "critical": t.get("UpperThresholdCritical") or t.get("UpperThresholdFatal"),
                "health": st.get("Health", "OK"),
            })
        return {"fans": fans, "temperatures": temps}

    def power(self) -> Dict[str, Any]:
        d = self.get(f"{CHS}Power/")
        pc = (d.get("PowerControl") or [{}])[0]
        metrics = pc.get("PowerMetrics") or {}
        return {
            "consumed_watts": pc.get("PowerConsumedWatts"),
            "capacity_watts": pc.get("PowerCapacityWatts"),
            "average_watts": metrics.get("AverageConsumedWatts"),
            "max_watts": metrics.get("MaxConsumedWatts"),
            "supplies": [{
                "name": s.get("Name") or f"PSU {i + 1}",
                "output": s.get("LastPowerOutputWatts"),
                "capacity": s.get("PowerCapacityWatts"),
                "health": (s.get("Status") or {}).get("Health"),
            } for i, s in enumerate(d.get("PowerSupplies", []))],
        }

    def system(self) -> Dict[str, Any]:
        """The fields that change: power state, POST state, health."""
        d = self.get(SYS)
        st = d.get("Status") or {}
        return {
            "model": d.get("Model"),
            "serial": d.get("SerialNumber"),
            "hostname": d.get("HostName"),
            "power_state": d.get("PowerState"),
            "post_state": _oem(d).get("PostState"),
            "health": st.get("HealthRollUp") or st.get("HealthRollup") or st.get("Health"),
            "indicator_led": d.get("IndicatorLED"),
            "bios_version": d.get("BiosVersion"),
            "cpu_model": ((d.get("ProcessorSummary") or {}).get("Model") or "").strip(),
            "cpu_count": (d.get("ProcessorSummary") or {}).get("Count"),
            "memory_gib": (d.get("MemorySummary") or {}).get("TotalSystemMemoryGiB"),
        }

    def manager(self) -> Dict[str, Any]:
        d = self.try_get(MGR) or {}
        fw = d.get("FirmwareVersion")
        legacy = _oem(d).get("Firmware")
        if not fw and isinstance(legacy, dict):
            fw = (legacy.get("Current") or {}).get("VersionString")
        return {"firmware": fw, "model": d.get("Model")}

    def ping(self) -> Dict[str, Any]:
        """Used by the 'Test connection' button before a server is saved."""
        s = self.system()
        return {"model": s.get("model"), "serial": s.get("serial"),
                "power_state": s.get("power_state")}
