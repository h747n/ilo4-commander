"""Import an old single-server install (v2.x) into this panel.

Copy each old VM's install folder over first - the .env file and the data/
folder are what matter - then:

    sudo -u ilo /opt/ilo4-commander/venv/bin/python -m app.migrate /tmp/old-vm1 --name "R1 DL380"
    sudo systemctl restart ilo4-commander

The server is added with its address and credentials, and its curve,
presets, last fan limits and recorded history come along with it.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Dict


def read_env(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in path.read_text("utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", help="old install folder (holds .env and data/)")
    ap.add_argument("--name", help="display name for this server")
    ap.add_argument("--no-history", action="store_true", help="skip the history database")
    ap.add_argument("--time-offset", type=int, default=0, metavar="SECONDS",
                    help="add this to every old timestamp, e.g. -12345 if the old VM's clock ran ahead")
    ap.add_argument("--into", metavar="SERVER_ID",
                    help="only (re)import history into this existing server; replaces what it has")
    args = ap.parse_args()

    from .history_db import history_db
    from .servers import ServerError, registry, server_dir

    src = Path(args.folder).resolve()
    env_file = src / ".env"
    if not env_file.exists():
        print(f"No .env in {src}", file=sys.stderr)
        return 1
    env = read_env(env_file)
    # The copied folder wins: ILO_DATA_DIR in the old .env may be an absolute
    # path that also exists here - and then it points at *this* install.
    data = src / "data"
    if not data.exists():
        data = Path(env.get("ILO_DATA_DIR", "./data"))

    if args.into:
        if not registry.get(args.into):
            print(f"No server {args.into}", file=sys.stderr)
            return 1
        history_db.forget_server(args.into)
        _import_history(history_db, args.into, data / "history.db", args.time_offset)
        print("Done. Restart the service.")
        return 0

    try:
        cfg = registry.add({
            "name": args.name or env.get("ILO_HOST", ""),
            "host": env.get("ILO_HOST", ""),
            "username": env.get("ILO_USERNAME", "Administrator"),
            "password": env.get("ILO_PASSWORD", ""),
            "ssh_port": env.get("ILO_SSH_PORT", 22),
            "https_port": env.get("ILO_HTTPS_PORT", 443),
            "min_pwm_percent": env.get("MIN_PWM_PERCENT", 8),
            "watchdog_enabled": env.get("WATCHDOG_ENABLED", "true").lower() in ("1", "true", "yes", "on"),
            "watchdog_margin_c": env.get("WATCHDOG_MARGIN_C", 5),
        })
    except ServerError as exc:
        print(f"Couldn't add the server: {exc}", file=sys.stderr)
        return 1
    sid = cfg["id"]
    print(f"Added {cfg['name']} ({cfg['host']}) as {sid}")

    dest = server_dir(sid)
    for name, target in (("curve.json", "curve.json"), ("presets.json", "presets.json"),
                         ("fan-state.json", "fan-state.json")):
        if (data / name).exists():
            shutil.copy(data / name, dest / target)
            print(f"  copied {name}")
    curve = dest / "curve.json"
    if curve.exists():
        c = json.loads(curve.read_text("utf-8"))
        c.pop("poll_seconds", None)
        curve.write_text(json.dumps(c, indent=2), "utf-8")

    if not args.no_history:
        _import_history(history_db, sid, data / "history.db", args.time_offset)
    print("Done. Restart the service so it starts watching this server.")
    return 0


def _import_history(history_db, sid: str, db: Path, offset: int) -> None:
    if not db.exists():
        print("  no history.db found")
        return
    # Not read-only: a copied database may still have a -wal file to replay.
    old = sqlite3.connect(str(db))
    n = history_db.import_raw(sid, ((ts + offset, k, nm, v) for ts, k, nm, v in
                                    old.execute("SELECT ts, kind, name, value FROM raw")))
    print(f"  imported {n:,} raw samples" + (f" (shifted {offset:+d}s)" if offset else ""))
    try:
        # Hourly buckets stay on the hour: round the shift to whole hours here.
        hshift = round(offset / 3600) * 3600
        m = history_db.import_hourly(sid, ((ts + hshift, k, nm, a, mn, mx, c) for ts, k, nm, a, mn, mx, c in
                                           old.execute("SELECT ts, kind, name, avg, min, max, n FROM rollup")))
        print(f"  imported {m:,} hourly rollups")
    except sqlite3.OperationalError:
        pass
    old.close()


if __name__ == "__main__":
    sys.exit(main())
