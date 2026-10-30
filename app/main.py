"""iLO4 Commander - one panel for every unlocked iLO 4 on the network."""
from __future__ import annotations

import asyncio
import csv
import io
import json
import mimetypes
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from urllib.parse import urlparse

from fastapi import (Body, Cookie, Depends, FastAPI, HTTPException, Query,
                     Request, Response, WebSocket, WebSocketDisconnect)
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import auth
from .config import VERSION, history_store, settings
from .history_db import RANGES, history_db, maintenance
from .influx import influx, influx_store, merge_secret
from .redfish import Redfish, RedfishError
from .servers import (CONNECTION_KEYS, FIELDS, ServerError, connection_changed,
                      public, registry)
from .ssh_client import IloShell, IloSshError
from .worker import ServerWorker, fleet

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
mimetypes.add_type("application/manifest+json", ".webmanifest")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    fleet.start_all()
    maintenance.start()
    influx.start()
    yield
    fleet.stop_all()
    maintenance.stop()
    influx.stop()


app = FastAPI(title="iLO4 Commander", version=VERSION, lifespan=lifespan,
              docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")


def same_origin(origin: Optional[str], host: Optional[str]) -> bool:
    """Browsers always send Origin on cross-site writes and WebSocket opens.
    No Origin at all means curl or a script, which a malicious page can't be."""
    if origin is None:
        return True
    try:
        return bool(host) and urlparse(origin).netloc.lower() == host.lower()
    except ValueError:
        return False


@app.middleware("http")
async def reject_cross_site_writes(request: Request, call_next):
    # Blocks another website, open in the same browser, from driving the fans
    # through this panel - even when no panel password is set.
    if request.method not in ("GET", "HEAD", "OPTIONS") and \
            not same_origin(request.headers.get("origin"), request.headers.get("host")):
        return JSONResponse(status_code=403, content={"detail": "Cross-site request refused."})
    return await call_next(request)


# --------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------- #
def require_auth(ilo_session: Optional[str] = Cookie(default=None)) -> None:
    if not auth.valid(ilo_session):
        raise HTTPException(status_code=401, detail="Sign in to continue.")


guard = [Depends(require_auth)]


@app.get("/api/auth")
def auth_state(ilo_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    return {"required": auth.required(), "signed_in": auth.valid(ilo_session), "version": VERSION}


_fails: Dict[str, List[float]] = {}
LOCK_AFTER, LOCK_WINDOW_S = 5, 300


@app.post("/api/login")
def login(request: Request, response: Response,
          password: str = Body(..., embed=True)) -> Dict[str, Any]:
    if not auth.required():
        return {"ok": True}
    ip, now = (request.client.host if request.client else "?"), time.time()
    recent = [t for t in _fails.get(ip, []) if now - t < LOCK_WINDOW_S]
    if len(recent) >= LOCK_AFTER:
        raise HTTPException(status_code=429, detail="Too many wrong passwords. Try again in a few minutes.")
    if not auth.check_password(password):
        _fails[ip] = recent + [now]
        time.sleep(0.6)
        raise HTTPException(status_code=401, detail="That password doesn't match.")
    _fails.pop(ip, None)
    response.set_cookie(auth.COOKIE, auth.issue(), httponly=True, samesite="lax",
                        secure=request.url.scheme == "https", max_age=auth.max_age())
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response) -> Dict[str, Any]:
    response.delete_cookie(auth.COOKIE)
    return {"ok": True}


@app.exception_handler(RedfishError)
def _rf_error(_req, exc: RedfishError):
    return JSONResponse(status_code=502, content={"detail": str(exc), "source": "redfish"})


@app.exception_handler(IloSshError)
def _ssh_error(_req, exc: IloSshError):
    return JSONResponse(status_code=502, content={"detail": str(exc), "source": "ssh"})


@app.exception_handler(ServerError)
def _srv_error(_req, exc: ServerError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(ValueError)
def _value_error(_req, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


def worker(sid: str) -> ServerWorker:
    try:
        return fleet.get(sid)
    except KeyError:
        if registry.get(sid):
            raise HTTPException(status_code=409, detail="This server is paused. Enable it first.")
        raise HTTPException(status_code=404, detail="No such server.")


# --------------------------------------------------------------------- #
# Servers
# --------------------------------------------------------------------- #
@app.get("/api/meta", dependencies=guard)
def meta() -> Dict[str, Any]:
    return {"version": VERSION, "allow_raw_fan": settings.allow_raw_fan,
            "defaults": {"poll_seconds": settings.default_poll_seconds,
                         "min_pwm_percent": settings.default_min_pwm_percent,
                         "watchdog_margin_c": settings.default_watchdog_margin_c}}


@app.get("/api/servers", dependencies=guard)
def servers_list() -> Dict[str, Any]:
    return {"servers": [public(s) for s in registry.all()], "snapshots": fleet.snapshots()}


@app.post("/api/servers/test", dependencies=guard)
def servers_test(server: Dict[str, Any] = Body(..., embed=True)) -> Dict[str, Any]:
    """Try the address and credentials before saving them."""
    cfg = dict(server)
    cfg.setdefault("username", "Administrator")
    cfg["host"] = str(cfg.get("host", "")).replace("https://", "").replace("http://", "").strip("/ ")
    if cfg.get("id") and (not cfg.get("password") or str(cfg["password"]).startswith("•")):
        # Reuse the stored password only against the stored address: never send
        # it to a host, port or username that arrived with this request.
        saved = registry.get(str(cfg["id"]))
        if not saved:
            raise HTTPException(status_code=404, detail="No such server.")
        stored = dict(FIELDS, **saved)
        asked = {**stored, **{k: cfg[k] for k in CONNECTION_KEYS if k in cfg}}
        if connection_changed(stored, asked):
            raise HTTPException(status_code=400,
                                detail="Enter the iLO password to test a changed address, port or username.")
        cfg["password"] = saved["password"]
    out: Dict[str, Any] = {"redfish": {"ok": False}, "ssh": {"ok": False}}
    rf = Redfish(cfg)
    try:
        out["redfish"] = {"ok": True, **rf.ping()}
    except Exception as exc:      # noqa: BLE001
        out["redfish"] = {"ok": False, "error": str(exc)}
    finally:
        rf.logout()
    sh = IloShell(cfg)
    try:
        p = sh.probe()
        out["ssh"] = {"ok": p["ssh"], "patched": p["patched"], "error": p["error"]}
    finally:
        sh.close()
    return out


@app.post("/api/servers", dependencies=guard)
def servers_add(server: Dict[str, Any] = Body(..., embed=True)) -> Dict[str, Any]:
    cfg = registry.add(server)
    fleet.added(cfg)
    return {"server": public(cfg)}


@app.put("/api/servers/order", dependencies=guard)
def servers_order(ids: List[str] = Body(..., embed=True)) -> Dict[str, Any]:
    registry.reorder(ids)
    return {"ok": True}


@app.put("/api/servers/{sid}", dependencies=guard)
def servers_update(sid: str, server: Dict[str, Any] = Body(..., embed=True)) -> Dict[str, Any]:
    cfg = registry.update(sid, server)
    fleet.changed(cfg)
    return {"server": public(cfg)}


@app.delete("/api/servers/{sid}", dependencies=guard)
def servers_delete(sid: str, restore: bool = Query(True),
                   purge_history: bool = Query(False)) -> Dict[str, Any]:
    if not registry.get(sid):
        raise HTTPException(status_code=404, detail="No such server.")
    err = fleet.removed(sid, restore)
    registry.remove(sid)
    if purge_history:
        history_db.forget_server(sid)
    return {"ok": True, "restore_error": err}


@app.get("/api/servers/{sid}/snapshot", dependencies=guard)
def server_snapshot(sid: str) -> Dict[str, Any]:
    return worker(sid).snapshot


@app.get("/api/servers/{sid}/recent", dependencies=guard)
def server_recent(sid: str, limit: int = Query(360, ge=1, le=720)) -> Dict[str, Any]:
    return {"samples": list(worker(sid).recent)[-limit:]}


@app.post("/api/servers/{sid}/refresh", dependencies=guard)
def server_refresh(sid: str) -> Dict[str, Any]:
    worker(sid).poke()
    return {"ok": True}


# --------------------------------------------------------------------- #
# Fans
# --------------------------------------------------------------------- #
@app.get("/api/servers/{sid}/fan/state", dependencies=guard)
def fan_state(sid: str, firmware: bool = Query(False)) -> Dict[str, Any]:
    w = worker(sid)
    return {"count": w.fan_count(), **w.fans.snapshot(read_firmware=firmware)}


@app.post("/api/servers/{sid}/fan/pwm", dependencies=guard)
def fan_pwm(sid: str, fan: str = Body("all"), percent: float = Body(...),
            mode: str = Body("max")) -> Dict[str, Any]:
    w = worker(sid)
    res = w.fans.set_pwm(fan, percent, mode)
    w.curve.reset_applied()
    return res


@app.post("/api/servers/{sid}/fan/unlock", dependencies=guard)
def fan_unlock(sid: str, fan: str = Body("all", embed=True)) -> Dict[str, Any]:
    w = worker(sid)
    res = w.fans.unlock(fan)
    w.curve.reset_applied()
    return res


@app.post("/api/servers/{sid}/fan/restore", dependencies=guard)
def fan_restore(sid: str) -> Dict[str, Any]:
    w = worker(sid)
    w.curve.save({"enabled": False}, w.fan_count())
    res = w.fans.restore_auto()
    w.log("handback", "All fans handed back to iLO by hand (Hand back to iLO button); curve switched off.")
    return res


@app.post("/api/servers/{sid}/fan/reapply", dependencies=guard)
def fan_reapply(sid: str) -> Dict[str, Any]:
    w = worker(sid)
    w.curve.clear_trip()
    w.reassert("Fan settings re-sent by hand.")
    err = w.apply_now()
    if err:
        raise HTTPException(status_code=502, detail=err)
    return {"ok": True}


@app.get("/api/servers/{sid}/fan/info", dependencies=guard)
def fan_info(sid: str, section: str = Query("", pattern="^[thagp]?$")) -> Dict[str, Any]:
    return worker(sid).fans.info(section)


@app.post("/api/servers/{sid}/fan/pid", dependencies=guard)
def fan_pid(sid: str, index: int = Body(...), key: str = Body(...),
            value: str = Body(...)) -> Dict[str, Any]:
    return worker(sid).fans.pid(index, key, value)


@app.post("/api/servers/{sid}/fan/temp", dependencies=guard)
def fan_temp(sid: str, index: int = Body(...), key: str = Body(...),
             value: Optional[str] = Body(None)) -> Dict[str, Any]:
    return worker(sid).fans.temp_sensor(index, key, value)


@app.post("/api/servers/{sid}/fan/tach", dependencies=guard)
def fan_tach(sid: str, index: int = Body(...), key: str = Body(...),
             value: Optional[str] = Body(None)) -> Dict[str, Any]:
    return worker(sid).fans.tach(index, key, value)


@app.post("/api/servers/{sid}/fan/global", dependencies=guard)
def fan_global(sid: str, op: str = Body("", embed=True)) -> Dict[str, Any]:
    return worker(sid).fans.global_op(op)


@app.post("/api/servers/{sid}/fan/rate", dependencies=guard)
def fan_rate(sid: str, ms: int = Body(3000, embed=True)) -> Dict[str, Any]:
    return worker(sid).fans.poll_rate(ms)


@app.get("/api/servers/{sid}/fan/dump", dependencies=guard)
def fan_dump(sid: str) -> Dict[str, Any]:
    return worker(sid).fans.dump()


@app.post("/api/servers/{sid}/fan/raw", dependencies=guard)
def fan_raw(sid: str, command: str = Body(..., embed=True)) -> Dict[str, Any]:
    if not settings.allow_raw_fan:
        raise HTTPException(status_code=403, detail="Raw fan commands are off (ALLOW_RAW_FAN=false).")
    return worker(sid).fans.raw(command)


@app.get("/api/servers/{sid}/presets", dependencies=guard)
def presets_get(sid: str) -> Dict[str, Any]:
    return {"presets": worker(sid).fans.list_presets()}


@app.put("/api/servers/{sid}/presets", dependencies=guard)
def presets_put(sid: str, presets: List[Dict[str, Any]] = Body(..., embed=True)) -> Dict[str, Any]:
    return {"presets": worker(sid).fans.save_presets(presets)}


@app.post("/api/servers/{sid}/presets/apply", dependencies=guard)
def presets_apply(sid: str, name: str = Body(...), mode: str = Body("max")) -> Dict[str, Any]:
    w = worker(sid)
    try:
        res = w.fans.apply_preset(name, mode)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    w.curve.reset_applied()
    return res


# --------------------------------------------------------------------- #
# Curve
# --------------------------------------------------------------------- #
@app.get("/api/servers/{sid}/curve", dependencies=guard)
def curve_get(sid: str) -> Dict[str, Any]:
    w = worker(sid)
    return w.curve.status(w.fan_count())


@app.put("/api/servers/{sid}/curve", dependencies=guard)
def curve_put(sid: str, config: Dict[str, Any] = Body(..., embed=True)) -> Dict[str, Any]:
    w = worker(sid)
    was = w.curve.config(w.fan_count())["enabled"]
    cfg = w.curve.save(config, w.fan_count())
    if cfg["enabled"] != was:
        w.log("info", "Automatic curve switched " + ("on." if cfg["enabled"] else "off — fans handed back to iLO."))
        if not cfg["enabled"]:
            # Switching the curve off hands the fans back rather than leaving
            # them frozen at whatever the curve last wrote.
            try:
                w.fans.restore_auto()
            except Exception as exc:      # noqa: BLE001
                w.curve.last_error = f"Couldn't hand fans back: {exc}"
    if cfg["enabled"]:
        w.apply_now()                     # takes effect now, not on the next poll
    return w.curve.status(w.fan_count())


# --------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------- #
def _window(range_: Optional[str], since: Optional[int], until: Optional[int]) -> tuple:
    now = int(time.time())
    end = int(until) if until else now
    if since:
        start = int(since)
    else:
        start = end - RANGES.get(range_ or "24h", 86400)
    if start >= end:
        raise HTTPException(status_code=400, detail="The start has to be before the end.")
    return start, end


@app.get("/api/history/meta", dependencies=guard)
def history_meta(server: str) -> Dict[str, Any]:
    return {"names": history_db.names(server), "stats": history_db.stats(),
            "settings": history_store.read(), "ranges": list(RANGES)}


@app.put("/api/history/settings", dependencies=guard)
def history_settings(raw_retention_hours: int = Body(..., embed=True)) -> Dict[str, Any]:
    history_store.write({"raw_retention_hours": max(6, min(24 * 14, int(raw_retention_hours)))})
    return history_store.read()


@app.get("/api/history/query", dependencies=guard)
def history_query(server: str, kind: str, name: List[str] = Query(...),
                  range: Optional[str] = None, since: Optional[int] = None,
                  until: Optional[int] = None,
                  points: int = Query(600, ge=20, le=3000)) -> Dict[str, Any]:
    start, end = _window(range, since, until)
    return {"since": start, "until": end,
            "series": [{"name": n, "points": history_db.query(server, kind, n, start, end, points)}
                       for n in name[:24]],
            "events": history_db.events(server, start, end)}


@app.get("/api/history/events", dependencies=guard)
def history_events(server: str, range: Optional[str] = "7d", since: Optional[int] = None,
                   until: Optional[int] = None) -> Dict[str, Any]:
    start, end = _window(range, since, until)
    return {"events": history_db.events(server, start, end)}


@app.get("/api/history/export", dependencies=guard)
def history_export(server: str, kind: str, name: List[str] = Query(...),
                   range: Optional[str] = None, since: Optional[int] = None,
                   until: Optional[int] = None) -> StreamingResponse:
    start, end = _window(range, since, until)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time_utc", "unix", "server", "kind", "name", "avg", "min", "max"])
    srv = (registry.get(server) or {}).get("name", server)
    for n in name[:24]:
        for p in history_db.query(server, kind, n, start, end, 3000):
            w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(p["ts"])), p["ts"],
                        srv, kind, n, p["avg"], p["min"], p["max"]])
    buf.seek(0)
    fname = f"{srv}-{kind}-{time.strftime('%Y%m%d-%H%M', time.localtime(end))}.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# --------------------------------------------------------------------- #
# InfluxDB export
# --------------------------------------------------------------------- #
@app.get("/api/integrations/influx", dependencies=guard)
def influx_status() -> Dict[str, Any]:
    return influx.status()


@app.put("/api/integrations/influx", dependencies=guard)
def influx_update(config: Dict[str, Any] = Body(..., embed=True)) -> Dict[str, Any]:
    saved = influx_store.read()
    influx_store.write({**saved, **merge_secret(config, saved)})
    return influx.status()


@app.post("/api/integrations/influx/test", dependencies=guard)
def influx_test(config: Dict[str, Any] = Body(..., embed=True)) -> Dict[str, Any]:
    saved = influx_store.read()
    return influx.test({**saved, **merge_secret(config, saved)})


# --------------------------------------------------------------------- #
# Live updates: every snapshot that changed since the last push.
# --------------------------------------------------------------------- #
@app.websocket("/ws")
async def ws_live(ws: WebSocket) -> None:
    if not same_origin(ws.headers.get("origin"), ws.headers.get("host")):
        await ws.close(code=4403)          # another website trying to read the feed
        return
    if not auth.valid(ws.cookies.get(auth.COOKIE)):
        await ws.close(code=4401)
        return
    await ws.accept()
    seen: Dict[str, int] = {}
    order: List[str] = []
    try:
        while True:
            snaps = fleet.snapshots()
            ids = [s["id"] for s in snaps]
            changed = [s for s in snaps if seen.get(s["id"]) != s["seq"]]
            if changed or ids != order:
                for s in changed:
                    seen[s["id"]] = s["seq"]
                order = ids
                await ws.send_text(json.dumps({"ids": ids, "snapshots": changed}, default=str))
            await asyncio.sleep(1.0)
    except (WebSocketDisconnect, RuntimeError):
        return


# --------------------------------------------------------------------- #
# Web app
# --------------------------------------------------------------------- #
def _page() -> HTMLResponse:
    """Stamp asset URLs with the version so an upgrade isn't hidden by cache."""
    html = (WEB_DIR / "index.html").read_text("utf-8").replace("{{V}}", VERSION)
    return HTMLResponse(html, headers={"Cache-Control": "no-store, must-revalidate"})


@app.get("/")
def index() -> HTMLResponse:
    return _page()


@app.get("/sw.js")
def service_worker() -> Response:
    body = (WEB_DIR / "sw.js").read_text("utf-8").replace("{{V}}", VERSION)
    return Response(body, media_type="application/javascript",
                    headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


app.mount("/", StaticFiles(directory=str(WEB_DIR)), name="web")
