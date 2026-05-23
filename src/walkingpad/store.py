import datetime as dt
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts      REAL NOT NULL,
    end_ts        REAL,
    duration_s    INTEGER DEFAULT 0,
    distance_m    INTEGER DEFAULT 0,
    steps         INTEGER DEFAULT 0,
    avg_speed_kmh REAL DEFAULT 0,
    max_speed_kmh REAL DEFAULT 0,
    created_at    REAL NOT NULL
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
    def __init__(self, db_path):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def create_session(self, start_ts):
        cur = self.conn.execute(
            "INSERT INTO sessions (start_ts, end_ts, created_at) VALUES (?, ?, ?)",
            (start_ts, start_ts, time.time()),
        )
        self.conn.commit()
        return cur.lastrowid

    def update_session(self, session_id, end_ts, duration_s, distance_m,
                       steps, avg_speed_kmh, max_speed_kmh):
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
        self.conn.execute(
            """INSERT INTO samples
               (session_id, ts, speed_kmh, distance_m, steps, belt_state)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (session_id, ts, speed_kmh, distance_m, steps, belt_state),
        )
        self.conn.commit()

    def get_session(self, session_id):
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_latest_session(self):
        row = self.conn.execute(
            "SELECT * FROM sessions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def sample_count(self, session_id):
        return self.conn.execute(
            "SELECT COUNT(*) FROM samples WHERE session_id=?", (session_id,)
        ).fetchone()[0]

    def daily_totals(self, date_str):
        row = self.conn.execute(
            """SELECT COALESCE(SUM(distance_m), 0) AS distance_m,
                      COALESCE(SUM(steps), 0)      AS steps,
                      COALESCE(SUM(duration_s), 0) AS duration_s,
                      COUNT(*)                     AS sessions
               FROM sessions
               WHERE date(start_ts, 'unixepoch', 'localtime') = ?""",
            (date_str,),
        ).fetchone()
        return dict(row)

    def list_sessions(self, date_str):
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
