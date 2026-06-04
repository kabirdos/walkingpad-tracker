import datetime as dt
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts        REAL NOT NULL,
    end_ts          REAL,
    duration_s      INTEGER DEFAULT 0,
    distance_m      INTEGER DEFAULT 0,
    steps           INTEGER DEFAULT 0,
    avg_speed_kmh   REAL DEFAULT 0,
    max_speed_kmh   REAL DEFAULT 0,
    -- Pad counter values at this session's zero point. Non-zero when a run
    -- spilled past midnight into a new day; lets a restarted daemon restore
    -- the offset and keep the session's totals relative to its own start.
    origin_distance INTEGER DEFAULT 0,
    origin_steps    INTEGER DEFAULT 0,
    origin_elapsed  INTEGER DEFAULT 0,
    created_at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS samples (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    ts         REAL NOT NULL,
    speed_kmh  REAL,
    distance_m INTEGER,
    steps      INTEGER,
    belt_state INTEGER,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
"""


class Store:
    # The capture loop (asyncio thread) and the FastAPI route handlers
    # (anyio threadpool workers) share this Store. sqlite3 Connections are
    # thread-safe at the C level only with serialized access — concurrent
    # cursor use on a shared connection can silently corrupt fetch results
    # (observed: `daily_totals().fetchone()` returning None on an aggregate
    # query that always has a row). The lock below serializes every public
    # method so reads and writes never race on the same cursor.
    def __init__(self, db_path):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self):
        # Add columns introduced after the original schema to pre-existing DBs.
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(sessions)")}
        for col in ("origin_distance", "origin_steps", "origin_elapsed"):
            if col not in have:
                self.conn.execute(
                    f"ALTER TABLE sessions ADD COLUMN {col} INTEGER DEFAULT 0"
                )

    def create_session(self, start_ts, origin_distance=0, origin_steps=0,
                       origin_elapsed=0):
        with self.lock:
            cur = self.conn.execute(
                """INSERT INTO sessions
                   (start_ts, end_ts, origin_distance, origin_steps, origin_elapsed,
                    created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (start_ts, start_ts, origin_distance, origin_steps, origin_elapsed,
                 time.time()),
            )
            self.conn.commit()
            return cur.lastrowid

    def update_session(self, session_id, end_ts, duration_s, distance_m,
                       steps, avg_speed_kmh, max_speed_kmh):
        with self.lock:
            self.conn.execute(
                """UPDATE sessions
                   SET end_ts=?, duration_s=?, distance_m=?, steps=?,
                       avg_speed_kmh=?, max_speed_kmh=?
                   WHERE id=?""",
                (end_ts, duration_s, distance_m, steps,
                 avg_speed_kmh, max_speed_kmh, session_id),
            )
            self.conn.commit()

    def add_sample(self, session_id, ts, speed_kmh, distance_m, steps, belt_state):
        with self.lock:
            self.conn.execute(
                """INSERT INTO samples
                   (session_id, ts, speed_kmh, distance_m, steps, belt_state)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, ts, speed_kmh, distance_m, steps, belt_state),
            )
            self.conn.commit()

    def get_session(self, session_id):
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_latest_session(self):
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM sessions ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def sample_count(self, session_id):
        with self.lock:
            return self.conn.execute(
                "SELECT COUNT(*) FROM samples WHERE session_id=?", (session_id,)
            ).fetchone()[0]

    def daily_totals(self, date_str):
        with self.lock:
            row = self.conn.execute(
                """SELECT COALESCE(SUM(distance_m), 0) AS distance_m,
                          COALESCE(SUM(steps), 0)      AS steps,
                          COALESCE(SUM(duration_s), 0) AS duration_s,
                          COUNT(*)                     AS sessions
                   FROM sessions
                   WHERE date(start_ts, 'unixepoch', 'localtime') = ?""",
                (date_str,),
            ).fetchone()
            return dict(row) if row else {
                "distance_m": 0, "steps": 0, "duration_s": 0, "sessions": 0,
            }

    def list_sessions(self, date_str):
        with self.lock:
            rows = self.conn.execute(
                """SELECT id, start_ts, end_ts, distance_m, steps, duration_s,
                          avg_speed_kmh, max_speed_kmh
                   FROM sessions
                   WHERE date(start_ts, 'unixepoch', 'localtime') = ?
                   ORDER BY id""",
                (date_str,),
            ).fetchall()
            return [dict(r) for r in rows]

    def daily_series(self, days):
        cutoff = (dt.date.today() - dt.timedelta(days=days - 1)).strftime("%Y-%m-%d")
        with self.lock:
            rows = self.conn.execute(
                """SELECT date(start_ts, 'unixepoch', 'localtime') AS day,
                          COALESCE(SUM(distance_m), 0) AS distance_m,
                          COALESCE(SUM(steps), 0)      AS steps,
                          COALESCE(SUM(duration_s), 0) AS duration_s,
                          COUNT(*)                     AS sessions
                   FROM sessions
                   WHERE date(start_ts, 'unixepoch', 'localtime') >= ?
                   GROUP BY day ORDER BY day""",
                (cutoff,),
            ).fetchall()
            return [dict(r) for r in rows]

    def current_streak(self):
        with self.lock:
            rows = self.conn.execute(
                """SELECT DISTINCT date(start_ts, 'unixepoch', 'localtime') AS day
                   FROM sessions WHERE distance_m > 0"""
            ).fetchall()
        dayset = {r["day"] for r in rows}
        cur = dt.date.today()
        if cur.strftime("%Y-%m-%d") not in dayset:
            cur = cur - dt.timedelta(days=1)
        streak = 0
        while cur.strftime("%Y-%m-%d") in dayset:
            streak += 1
            cur -= dt.timedelta(days=1)
        return streak
