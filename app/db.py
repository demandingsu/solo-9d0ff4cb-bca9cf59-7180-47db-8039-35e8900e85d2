"""SQLite 存储层。

版本一致性设计（对应"上传、发布和查询交错时不得混用两版"）：
  * 全库只有一条 meta 记录保存 active_version_id；
  * 发布 = 单个事务内翻转 active_version_id 并更新版本状态，失败整体回滚，旧版继续供数；
  * 查询 = 单个只读事务内先读 active_version_id、再读该版本的数据，
    同一事务的快照保证一次查询只看到一个版本；
  * 进程内用一把 RLock 串行化所有事务（单写者模型），
    因此上传暂存、发布、查询无论怎样交错都不会出现跨版本混读。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

from .csv_import import ParsedData

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    active_version_id INTEGER
);
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL CHECK (status IN ('staged', 'active', 'superseded', 'invalid')),
    created_at TEXT NOT NULL,
    published_at TEXT,
    stops_count INTEGER NOT NULL DEFAULT 0,
    trips_count INTEGER NOT NULL DEFAULT 0,
    stop_times_count INTEGER NOT NULL DEFAULT 0,
    errors TEXT
);
CREATE TABLE IF NOT EXISTS stops (
    version_id INTEGER NOT NULL REFERENCES versions (id),
    stop_id TEXT NOT NULL,
    stop_name TEXT NOT NULL,
    PRIMARY KEY (version_id, stop_id)
);
CREATE TABLE IF NOT EXISTS trips (
    version_id INTEGER NOT NULL REFERENCES versions (id),
    trip_id TEXT NOT NULL,
    route_id TEXT NOT NULL,
    service_date TEXT NOT NULL,
    PRIMARY KEY (version_id, trip_id)
);
CREATE TABLE IF NOT EXISTS stop_times (
    version_id INTEGER NOT NULL REFERENCES versions (id),
    trip_id TEXT NOT NULL,
    stop_id TEXT NOT NULL,
    stop_sequence INTEGER NOT NULL,
    departure_minutes INTEGER NOT NULL CHECK (departure_minutes BETWEEN 0 AND 1799),
    PRIMARY KEY (version_id, trip_id, stop_sequence),
    FOREIGN KEY (version_id, trip_id) REFERENCES trips (version_id, trip_id),
    FOREIGN KEY (version_id, stop_id) REFERENCES stops (version_id, stop_id)
);
CREATE INDEX IF NOT EXISTS idx_stop_times_stop ON stop_times (version_id, stop_id);
CREATE INDEX IF NOT EXISTS idx_trips_date ON trips (version_id, service_date);
"""


class NoActiveVersion(Exception):
    """当前没有已发布的版本。"""


class StopNotFound(Exception):
    """当前版本中不存在该站点。"""


class VersionNotFound(Exception):
    """版本不存在。"""


class VersionNotPublishable(Exception):
    """版本状态不允许发布。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Database:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        # isolation_level=None => 自动提交关闭，所有事务由本类显式控制
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.execute("INSERT OR IGNORE INTO meta (id, active_version_id) VALUES (1, NULL)")

    @contextmanager
    def _tx(self, immediate: bool = False):
        """显式事务：任何异常都会整体回滚，保证旧版本数据不受污染。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self._conn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # ---------------- 写入：暂存 / 发布 ----------------

    def record_invalid_version(self, errors: list[str]) -> int:
        """校验失败的上传也留痕（status=invalid），便于交接员追溯，但不存任何业务数据。"""
        with self._tx(immediate=True) as conn:
            cur = conn.execute(
                "INSERT INTO versions (status, created_at, errors) VALUES ('invalid', ?, ?)",
                (_now_iso(), json.dumps(errors, ensure_ascii=False)),
            )
            return int(cur.lastrowid)

    def insert_staged_version(self, data: ParsedData) -> int:
        """校验通过的数据整版写入，status=staged；此时尚不影响线上供数。"""
        with self._tx(immediate=True) as conn:
            cur = conn.execute(
                """INSERT INTO versions (status, created_at, stops_count, trips_count, stop_times_count)
                   VALUES ('staged', ?, ?, ?, ?)""",
                (_now_iso(), len(data.stops), len(data.trips), len(data.stop_times)),
            )
            vid = int(cur.lastrowid)
            conn.executemany(
                "INSERT INTO stops (version_id, stop_id, stop_name) VALUES (?, ?, ?)",
                [(vid, sid, name) for sid, name in data.stops],
            )
            conn.executemany(
                "INSERT INTO trips (version_id, trip_id, route_id, service_date) VALUES (?, ?, ?, ?)",
                [(vid, tid, route, sdate) for tid, route, sdate in data.trips],
            )
            conn.executemany(
                "INSERT INTO stop_times (version_id, trip_id, stop_id, stop_sequence, departure_minutes)"
                " VALUES (?, ?, ?, ?, ?)",
                [(vid, tid, sid, seq, minutes) for tid, sid, seq, minutes in data.stop_times],
            )
            return vid

    def publish(self, version_id: int) -> dict:
        """整版发布：单事务内完成 旧版下线 + 新版上线 + 切换 active 指针。

        中途任何失败都会回滚，meta 仍指向旧版本，旧版继续供数。
        """
        with self._tx(immediate=True) as conn:
            row = conn.execute("SELECT status FROM versions WHERE id = ?", (version_id,)).fetchone()
            if row is None:
                raise VersionNotFound(f"版本 {version_id} 不存在")
            status = row["status"]
            if status == "invalid":
                raise VersionNotPublishable(f"版本 {version_id} 校验未通过，不能发布")
            if status == "active":
                raise VersionNotPublishable(f"版本 {version_id} 已是当前发布版本")
            if status == "superseded":
                raise VersionNotPublishable(f"版本 {version_id} 已被更新版本取代，不允许回滚发布")
            now = _now_iso()
            conn.execute("UPDATE versions SET status = 'superseded' WHERE status = 'active'")
            conn.execute(
                "UPDATE versions SET status = 'active', published_at = ? WHERE id = ?",
                (now, version_id),
            )
            conn.execute("UPDATE meta SET active_version_id = ? WHERE id = 1", (version_id,))
            return {"version_id": version_id, "status": "active", "published_at": now}

    # ---------------- 读取 ----------------

    def list_versions(self) -> list[dict]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT * FROM versions ORDER BY id DESC"
            ).fetchall()
        return [self._version_dict(r) for r in rows]

    def get_current_version(self) -> dict | None:
        with self._tx() as conn:
            row = conn.execute(
                """SELECT v.* FROM versions v
                   JOIN meta m ON m.active_version_id = v.id WHERE m.id = 1"""
            ).fetchone()
        return self._version_dict(row) if row else None

    def get_stop_departures(self, stop_id: str, from_dt: datetime) -> dict:
        """查询某站点从 from_dt（UTC）起一小时内的发车。

        合并两个运营日：from 当天 + 前一个运营日（其 24:00~29:59 的班次
        实际发生在 from 当天的凌晨）。整个读取在一个事务快照内完成，
        因此 version_id 与数据必然来自同一版本。
        """
        from_dt = from_dt.astimezone(timezone.utc)
        from_ts = from_dt.timestamp()
        to_ts = from_ts + 3600
        day = from_dt.date()
        dates = [(day - timedelta(days=1)).isoformat(), day.isoformat()]

        with self._tx() as conn:
            active = conn.execute(
                "SELECT active_version_id FROM meta WHERE id = 1"
            ).fetchone()["active_version_id"]
            if active is None:
                raise NoActiveVersion
            stop = conn.execute(
                "SELECT stop_name FROM stops WHERE version_id = ? AND stop_id = ?",
                (active, stop_id),
            ).fetchone()
            if stop is None:
                raise StopNotFound(stop_id)
            rows = conn.execute(
                """SELECT tr.route_id AS route_id, st.trip_id AS trip_id,
                          tr.service_date AS service_date, st.departure_minutes AS departure_minutes
                   FROM stop_times st
                   JOIN trips tr ON tr.version_id = st.version_id AND tr.trip_id = st.trip_id
                   WHERE st.version_id = ? AND st.stop_id = ?
                     AND tr.service_date IN (?, ?)""",
                (active, stop_id, dates[0], dates[1]),
            ).fetchall()

        departures = []
        for r in rows:
            d = date.fromisoformat(r["service_date"])
            base = datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()
            actual_ts = base + r["departure_minutes"] * 60
            if from_ts <= actual_ts < to_ts:
                departures.append(
                    {
                        "route_id": r["route_id"],
                        "trip_id": r["trip_id"],
                        "service_date": r["service_date"],
                        "scheduled_departure": f"{r['departure_minutes'] // 60:02d}:{r['departure_minutes'] % 60:02d}",
                        "departure_time": _iso_z(datetime.fromtimestamp(actual_ts, timezone.utc)),
                    }
                )
        departures.sort(key=lambda x: (x["departure_time"], x["trip_id"]))
        return {
            "stop_id": stop_id,
            "stop_name": stop["stop_name"],
            "from": _iso_z(from_dt),
            "to": _iso_z(datetime.fromtimestamp(to_ts, timezone.utc)),
            "version_id": active,
            "departures": departures,
        }

    # ---------------- 工具 ----------------

    @staticmethod
    def _version_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        if d.get("errors"):
            d["errors"] = json.loads(d["errors"])
        else:
            d.pop("errors", None)
        return d

    def close(self) -> None:
        with self._lock:
            self._conn.close()
