"""Local time-series storage for every server's sensors, fans and power.

SQLite only, no external database. Three resolutions:

    raw   every sample, kept for `raw_retention_hours` (default 48h)
    m5    5-minute avg/min/max, kept 35 days   -> the 7-day and 30-day views
    h1    hourly avg/min/max, kept 2 years

Rollups are computed from the finer tier before it's trimmed, so they are
exact. A series is (server, kind, name); kind is temp | fan | power | target.
Events (power on/off, watchdog, re-applied curve) are stored too and drawn
on the history chart as markers.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import DATA_DIR, history_store

DB_PATH = DATA_DIR / "history.db"
M5, H1 = 300, 3600
M5_KEEP_S = 35 * 86400
H1_KEEP_S = 2 * 365 * 86400
EVENT_KEEP_S = 180 * 86400
MAINTENANCE_INTERVAL_S = 600

RANGES = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400,
          "30d": 30 * 86400, "90d": 90 * 86400, "1y": 365 * 86400}


def raw_retention_s() -> int:
    return int(history_store.read().get("raw_retention_hours", 48)) * 3600


class HistoryDB:
    def __init__(self, path=DB_PATH) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._series: Dict[Tuple[str, str, str], int] = {}
        self._init_schema()

    def _init_schema(self) -> None:
        c = self._conn
        with self._lock, c:
            c.execute("""CREATE TABLE IF NOT EXISTS series (
                id INTEGER PRIMARY KEY, server TEXT NOT NULL, kind TEXT NOT NULL,
                name TEXT NOT NULL, UNIQUE(server, kind, name))""")
            c.execute("""CREATE TABLE IF NOT EXISTS raw (
                sid INTEGER NOT NULL, ts INTEGER NOT NULL, v REAL NOT NULL,
                PRIMARY KEY (sid, ts)) WITHOUT ROWID""")
            c.execute("CREATE INDEX IF NOT EXISTS raw_ts ON raw(ts)")
            for tier in ("m5", "h1"):
                c.execute(f"""CREATE TABLE IF NOT EXISTS {tier} (
                    sid INTEGER NOT NULL, ts INTEGER NOT NULL,
                    avg REAL NOT NULL, min REAL NOT NULL, max REAL NOT NULL, n INTEGER NOT NULL,
                    PRIMARY KEY (sid, ts)) WITHOUT ROWID""")
                c.execute(f"CREATE INDEX IF NOT EXISTS {tier}_ts ON {tier}(ts)")
            c.execute("""CREATE TABLE IF NOT EXISTS events (
                server TEXT NOT NULL, ts INTEGER NOT NULL, kind TEXT NOT NULL,
                message TEXT NOT NULL)""")
            c.execute("CREATE INDEX IF NOT EXISTS events_ts ON events(server, ts)")
            c.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v INTEGER)")
            for sid, server, kind, name in c.execute("SELECT id, server, kind, name FROM series"):
                self._series[(server, kind, name)] = sid

    def _sid(self, server: str, kind: str, name: str) -> int:
        key = (server, kind, name)
        sid = self._series.get(key)
        if sid is None:
            self._conn.execute("INSERT OR IGNORE INTO series (server, kind, name) VALUES (?,?,?)", key)
            sid = self._conn.execute("SELECT id FROM series WHERE server=? AND kind=? AND name=?",
                                     key).fetchone()[0]
            self._series[key] = sid
        return sid

    def _meta(self, key: str, default: int = 0) -> int:
        row = self._conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return row[0] if row else default

    def _set_meta(self, key: str, val: int) -> None:
        self._conn.execute("INSERT INTO meta (k, v) VALUES (?, ?) "
                           "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, val))

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #
    def record(self, server: str, ts: float, rows: Iterable[Tuple[str, str, float]]) -> None:
        """rows: (kind, name, value)."""
        t = int(ts)
        with self._lock, self._conn:
            data = [(self._sid(server, k, n), t, float(v)) for k, n, v in rows
                    if isinstance(v, (int, float))]
            self._conn.executemany("INSERT OR REPLACE INTO raw (sid, ts, v) VALUES (?,?,?)", data)

    def event(self, server: str, kind: str, message: str, ts: Optional[float] = None) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO events (server, ts, kind, message) VALUES (?,?,?,?)",
                               (server, int(ts or time.time()), kind, message))

    def import_raw(self, server: str, rows: Iterable[Tuple[int, str, str, float]]) -> int:
        """Bulk load (ts, kind, name, value), used when migrating an old install."""
        n = 0
        with self._lock, self._conn:
            batch = []
            for ts, kind, name, v in rows:
                batch.append((self._sid(server, kind, name), int(ts), float(v)))
                if len(batch) >= 5000:
                    self._conn.executemany("INSERT OR IGNORE INTO raw VALUES (?,?,?)", batch)
                    n += len(batch); batch = []
            self._conn.executemany("INSERT OR IGNORE INTO raw VALUES (?,?,?)", batch)
            n += len(batch)
            # Roll the imported samples up straight away: they are probably older
            # than the rollup watermark, so maintain() would otherwise skip them.
            sids = [v for k, v in self._series.items() if k[0] == server]
            marks = ",".join("?" * len(sids))
            if sids:
                self._conn.execute(f"""INSERT OR REPLACE INTO m5
                    SELECT sid, (ts/300)*300, AVG(v), MIN(v), MAX(v), COUNT(*)
                    FROM raw WHERE sid IN ({marks}) GROUP BY sid, ts/300""", sids)
                self._conn.execute(f"""INSERT OR REPLACE INTO h1
                    SELECT sid, (ts/3600)*3600, SUM(avg*n)/SUM(n), MIN(min), MAX(max), SUM(n)
                    FROM m5 WHERE sid IN ({marks}) GROUP BY sid, ts/3600""", sids)
        return n

    def import_hourly(self, server: str, rows: Iterable[Tuple[int, str, str, float, float, float, int]]) -> int:
        n = 0
        with self._lock, self._conn:
            for ts, kind, name, avg, mn, mx, cnt in rows:
                self._conn.execute("INSERT OR IGNORE INTO h1 VALUES (?,?,?,?,?,?)",
                                   (self._sid(server, kind, name), int(ts), avg, mn, mx, cnt))
                n += 1
        return n

    def forget_server(self, server: str) -> None:
        with self._lock, self._conn:
            ids = [r[0] for r in self._conn.execute("SELECT id FROM series WHERE server=?", (server,))]
            for tier in ("raw", "m5", "h1"):
                self._conn.executemany(f"DELETE FROM {tier} WHERE sid=?", [(i,) for i in ids])
            self._conn.execute("DELETE FROM series WHERE server=?", (server,))
            self._conn.execute("DELETE FROM events WHERE server=?", (server,))
            self._series = {k: v for k, v in self._series.items() if k[0] != server}

    # ------------------------------------------------------------------ #
    # Rollups
    # ------------------------------------------------------------------ #
    def maintain(self) -> None:
        now = int(time.time())
        with self._lock, self._conn:
            c = self._conn
            # raw -> m5, for every 5-minute bucket that has finished.
            done5 = (now // M5) * M5
            start5 = self._meta("m5_until", 0)
            if start5 < done5:
                c.execute("""INSERT OR REPLACE INTO m5 (sid, ts, avg, min, max, n)
                    SELECT sid, (ts/300)*300, AVG(v), MIN(v), MAX(v), COUNT(*)
                    FROM raw WHERE ts >= ? AND ts < ? GROUP BY sid, ts/300""", (start5, done5))
                self._set_meta("m5_until", done5)
            # m5 -> h1, for every hour that has finished.
            doneh = (now // H1) * H1
            starth = self._meta("h1_until", 0)
            if starth < doneh:
                c.execute("""INSERT OR REPLACE INTO h1 (sid, ts, avg, min, max, n)
                    SELECT sid, (ts/3600)*3600, SUM(avg*n)/SUM(n), MIN(min), MAX(max), SUM(n)
                    FROM m5 WHERE ts >= ? AND ts < ? GROUP BY sid, ts/3600""", (starth, doneh))
                self._set_meta("h1_until", doneh)
            c.execute("DELETE FROM raw WHERE ts < ?", (min(now - raw_retention_s(), done5),))
            c.execute("DELETE FROM m5 WHERE ts < ?", (min(now - M5_KEEP_S, doneh),))
            c.execute("DELETE FROM h1 WHERE ts < ?", (now - H1_KEEP_S,))
            c.execute("DELETE FROM events WHERE ts < ?", (now - EVENT_KEEP_S,))
        try:
            with self._lock:
                self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except sqlite3.OperationalError:
            pass

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    def names(self, server: str) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        with self._lock:
            keys = list(self._series)
        for (srv, kind, name) in keys:
            if srv == server:
                out.setdefault(kind, []).append(name)
        for k in out:
            out[k].sort()
        return out

    def query(self, server: str, kind: str, name: str, since: int, until: int,
              max_points: int = 600) -> List[Dict[str, float]]:
        """Bucketed avg/min/max between since and until, picking the finest tier
        that still has data that far back."""
        sid = self._series.get((server, kind, name))
        if sid is None:
            return []
        now = int(time.time())
        span = max(1, until - since)
        if since >= now - raw_retention_s():
            tier, res = "raw", 1
        elif since >= now - M5_KEEP_S:
            tier, res = "m5", M5
        else:
            tier, res = "h1", H1
        width = max(res, -(-span // max(10, max_points)))
        with self._lock:
            if tier == "raw":
                rows = self._conn.execute(
                    """SELECT (ts/?)*? AS b, AVG(v), MIN(v), MAX(v) FROM raw
                       WHERE sid=? AND ts BETWEEN ? AND ? GROUP BY b ORDER BY b""",
                    (width, width, sid, since, until)).fetchall()
            else:
                rolled_until = self._meta("m5_until" if tier == "m5" else "h1_until", 0)
                rows = self._conn.execute(
                    f"""SELECT (ts/?)*? AS b, SUM(avg*n)/SUM(n), MIN(min), MAX(max) FROM {tier}
                        WHERE sid=? AND ts BETWEEN ? AND ? GROUP BY b ORDER BY b""",
                    (width, width, sid, since, until)).fetchall()
                # The newest few minutes haven't been rolled up yet - take them from raw.
                if rolled_until < until:
                    rows += self._conn.execute(
                        """SELECT (ts/?)*? AS b, AVG(v), MIN(v), MAX(v) FROM raw
                           WHERE sid=? AND ts >= ? AND ts <= ? GROUP BY b ORDER BY b""",
                        (width, width, sid, max(since, rolled_until), until)).fetchall()
        merged: Dict[int, List[float]] = {}
        for b, avg, mn, mx in rows:
            if b in merged:          # a bucket split across tier boundary
                m = merged[b]
                merged[b] = [(m[0] + avg) / 2, min(m[1], mn), max(m[2], mx)]
            else:
                merged[b] = [avg, mn, mx]
        return [{"ts": b, "avg": round(v[0], 2), "min": round(v[1], 2), "max": round(v[2], 2)}
                for b, v in sorted(merged.items())]

    def events(self, server: str, since: int, until: int, limit: int = 500) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, kind, message FROM events WHERE server=? AND ts BETWEEN ? AND ? "
                "ORDER BY ts DESC LIMIT ?", (server, since, until, limit)).fetchall()
        return [{"ts": r[0], "kind": r[1], "message": r[2]} for r in rows]

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            counts = {t: self._conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      for t in ("raw", "m5", "h1")}
            oldest = self._conn.execute(
                "SELECT MIN(ts) FROM (SELECT MIN(ts) ts FROM raw UNION ALL "
                "SELECT MIN(ts) FROM m5 UNION ALL SELECT MIN(ts) FROM h1)").fetchone()[0]
        size = 0
        for suffix in ("", "-wal"):
            p = self.path.with_name(self.path.name + suffix)
            if p.exists():
                size += p.stat().st_size
        return {"rows": counts, "oldest_ts": oldest, "db_bytes": size,
                "raw_retention_hours": raw_retention_s() // 3600}


history_db = HistoryDB()


class Maintenance(threading.Thread):
    daemon = True

    def __init__(self, db: HistoryDB) -> None:
        super().__init__(name="history-maint")
        self.db = db
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.db.maintain()
            except Exception:      # noqa: BLE001 - a missed rollup isn't fatal
                pass
            self._stop.wait(MAINTENANCE_INTERVAL_S)


maintenance = Maintenance(history_db)
