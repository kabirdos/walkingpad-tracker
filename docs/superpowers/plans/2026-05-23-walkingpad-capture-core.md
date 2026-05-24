# WalkingPad Capture Core — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reliable background-capable logger that connects to the WalkingPad P1 over BLE, parses its live stats, and records every walk into SQLite as sessions that survive counter resets — plus a CLI to view daily totals.

**Architecture:** A pure, unit-testable core (`store` → SQLite, `status` → unit conversion, `recorder` → session detection/accumulation) sits behind a thin BLE adapter (`pad_client`, wrapping `ph4-walkingpad`) and a `cli`. The recorder consumes a stream of `PadStatus` values decoupled from Bluetooth, so all logic is testable without hardware. A session is a continuous run between pad counter-resets/disconnects; the daily total is the sum of sessions for the local date.

**Tech Stack:** Python 3.11+ (run via `uv`), `ph4-walkingpad` + `bleak` for BLE, `sqlite3` (stdlib) for storage, `pytest` for tests.

**Scope note:** This is **Plan 1 of a series**. It delivers working capture + a CLI. Later plans add: (2) local HTTP API + control, (3) web dashboard, (4) menubar widget, (5) Apple Health export + Shortcut + launchd supervision. Each later plan builds on this core.

---

## File Structure

```
pyproject.toml                       # uv project + deps + pytest config
src/walkingpad/__init__.py
src/walkingpad/store.py              # SQLite schema + read/write helpers
src/walkingpad/status.py             # PadStatus dataclass + unit conversion
src/walkingpad/recorder.py           # session detection + accumulation (pure)
src/walkingpad/pad_client.py         # BLE adapter over ph4-walkingpad + speed helper
src/walkingpad/cli.py                # capture / today / sessions commands
tests/test_store.py
tests/test_status.py
tests/test_recorder.py
tests/test_pad_speed.py
```

- `store.py` owns all SQL. Nothing else touches the database directly.
- `status.py` owns the pad's wire-unit → SI conversion (the one place units are interpreted).
- `recorder.py` is pure logic over `PadStatus` + `Store`; no Bluetooth, fully unit-tested.
- `pad_client.py` is the only file that imports `bleak`/`ph4_walkingpad`; integration-tested manually.
- `cli.py` wires them together and owns the reconnect loop.

---

## Task 1: Project scaffolding

**Files:**

- Create: `pyproject.toml`
- Create: `src/walkingpad/__init__.py`
- Create: `tests/test_smoke.py`

- [ ] **Step 1: Write the failing smoke test**

Create `tests/test_smoke.py`:

```python
def test_package_imports():
    import walkingpad
    assert walkingpad.__name__ == "walkingpad"
```

- [ ] **Step 2: Create the package marker**

Create `src/walkingpad/__init__.py`:

```python
"""WalkingPad P1 capture, control, and Health-sync tooling."""
__version__ = "0.1.0"
```

- [ ] **Step 3: Create `pyproject.toml`**

```toml
[project]
name = "walkingpad-tracker"
version = "0.1.0"
description = "Self-hosted WalkingPad P1 capture, control, and Apple Health sync"
requires-python = ">=3.11"
dependencies = [
    "ph4-walkingpad>=1.0.0",
    "bleak>=0.21",
]

[dependency-groups]
dev = ["pytest>=8.0"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
pythonpath = ["src"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/walkingpad"]
```

- [ ] **Step 4: Run the smoke test**

Run: `uv run pytest tests/test_smoke.py -v`
Expected: PASS. (First run, `uv` creates the venv and installs deps. If it fails resolving `ph4-walkingpad`/`bleak` on Python 3.14, pin a known-good interpreter: `uv python pin 3.12` then re-run.)

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/walkingpad/__init__.py tests/test_smoke.py
git commit -m "chore: scaffold walkingpad-tracker uv project"
```

---

## Task 2: SQLite store

**Files:**

- Create: `src/walkingpad/store.py`
- Test: `tests/test_store.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_store.py`:

```python
import datetime as dt
from walkingpad.store import Store


def test_create_and_update_session(tmp_path):
    db = Store(str(tmp_path / "t.db"))
    sid = db.create_session(start_ts=1000.0)
    db.update_session(sid, end_ts=1600.0, duration_s=600, distance_m=1000,
                      steps=1300, avg_speed_kmh=6.0, max_speed_kmh=6.0)
    s = db.get_session(sid)
    assert s["distance_m"] == 1000
    assert s["steps"] == 1300
    assert s["duration_s"] == 600
    assert s["max_speed_kmh"] == 6.0


def test_get_latest_session_none_when_empty(tmp_path):
    db = Store(str(tmp_path / "t.db"))
    assert db.get_latest_session() is None


def test_daily_totals_sums_sessions(tmp_path):
    db = Store(str(tmp_path / "t.db"))
    day = dt.datetime(2026, 5, 23, 8, 0, 0)
    ts1 = day.timestamp()
    ts2 = (day + dt.timedelta(hours=2)).timestamp()
    for ts, dist, steps in [(ts1, 500, 650), (ts2, 700, 900)]:
        sid = db.create_session(ts)
        db.update_session(sid, end_ts=ts + 300, duration_s=300, distance_m=dist,
                          steps=steps, avg_speed_kmh=5.0, max_speed_kmh=5.5)
    totals = db.daily_totals("2026-05-23")
    assert totals["distance_m"] == 1200
    assert totals["steps"] == 1550
    assert totals["duration_s"] == 600
    assert totals["sessions"] == 2


def test_add_sample(tmp_path):
    db = Store(str(tmp_path / "t.db"))
    sid = db.create_session(start_ts=1000.0)
    db.add_sample(sid, ts=1001.0, speed_kmh=3.0, distance_m=10, steps=12, belt_state=1)
    assert db.sample_count(sid) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'walkingpad.store'`.

- [ ] **Step 3: Implement the store**

Create `src/walkingpad/store.py`:

```python
import sqlite3

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
        import time
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_store.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/walkingpad/store.py tests/test_store.py
git commit -m "feat: add SQLite store for sessions and samples"
```

---

## Task 3: Status conversion

**Files:**

- Create: `src/walkingpad/status.py`
- Test: `tests/test_status.py`

Confirmed `WalkingPadCurStatus` encoding: `.speed` is 0.1 km/h, `.dist` is 0.01 km (×10 = metres), `.time` is seconds, `.steps` raw, `.belt_state` int.

- [ ] **Step 1: Write the failing test**

Create `tests/test_status.py`:

```python
from types import SimpleNamespace
from walkingpad.status import PadStatus, cur_status_to_padstatus


def test_conversion_units():
    record = SimpleNamespace(speed=30, dist=150, time=600, steps=1200, belt_state=1)
    s = cur_status_to_padstatus(record, ts=1000.0)
    assert isinstance(s, PadStatus)
    assert s.speed_kmh == 3.0       # 30 / 10
    assert s.distance_m == 1500     # 150 * 10  (150 * 0.01 km = 1.5 km)
    assert s.elapsed_s == 600
    assert s.steps == 1200
    assert s.belt_state == 1
    assert s.ts == 1000.0


def test_conversion_zero():
    record = SimpleNamespace(speed=0, dist=0, time=0, steps=0, belt_state=0)
    s = cur_status_to_padstatus(record, ts=5.0)
    assert s.speed_kmh == 0.0
    assert s.distance_m == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_status.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'walkingpad.status'`.

- [ ] **Step 3: Implement the conversion**

Create `src/walkingpad/status.py`:

```python
from dataclasses import dataclass


@dataclass
class PadStatus:
    ts: float            # epoch seconds when observed
    speed_kmh: float
    distance_m: int
    steps: int
    elapsed_s: int
    belt_state: int


def cur_status_to_padstatus(record, ts):
    """Convert a ph4_walkingpad WalkingPadCurStatus into our PadStatus.

    Wire units: speed=0.1 km/h, dist=0.01 km (so *10 = metres), time=seconds.
    """
    return PadStatus(
        ts=ts,
        speed_kmh=record.speed / 10.0,
        distance_m=int(record.dist) * 10,
        steps=int(record.steps),
        elapsed_s=int(record.time),
        belt_state=int(record.belt_state),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_status.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/walkingpad/status.py tests/test_status.py
git commit -m "feat: add PadStatus and wire-unit conversion"
```

---

## Task 4: Recorder (session detection + accumulation)

**Files:**

- Create: `src/walkingpad/recorder.py`
- Test: `tests/test_recorder.py`

Logic: the pad reports cumulative `distance_m`/`steps`/`elapsed_s` **for the current run**, resetting to ~0 on a new run. A session opens when the belt is moving (or counters are non-zero), updates the session row on every status (crash-safe), and closes when the counter resets (distance drops by more than `RESET_TOL_M`) or `close()` is called (disconnect/shutdown). Daily total = sum of sessions for the local date.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_recorder.py`:

```python
import datetime as dt
from walkingpad.store import Store
from walkingpad.status import PadStatus
from walkingpad.recorder import Recorder


def mk(ts, speed, dist_m, steps, elapsed):
    return PadStatus(ts=ts, speed_kmh=speed, distance_m=dist_m,
                     steps=steps, elapsed_s=elapsed, belt_state=1)


def test_single_session_accumulates(tmp_path):
    db = Store(str(tmp_path / "t.db"))
    r = Recorder(db)
    base = 1716451200.0
    for i in range(1, 6):
        r.handle(mk(base + i, 3.0, dist_m=i * 50, steps=i * 65, elapsed=i * 30))
    r.close()
    s = db.get_latest_session()
    assert s["distance_m"] == 250
    assert s["steps"] == 325
    assert s["max_speed_kmh"] == 3.0
    assert db.sample_count(s["id"]) == 5


def test_reset_creates_second_session(tmp_path):
    db = Store(str(tmp_path / "t.db"))
    r = Recorder(db)
    base = 1716451200.0
    r.handle(mk(base + 1, 3.0, 100, 130, 60))
    r.handle(mk(base + 2, 3.0, 200, 260, 120))
    # pad reset -> new run begins from a low counter
    r.handle(mk(base + 3, 3.0, 30, 40, 20))
    r.handle(mk(base + 4, 3.0, 90, 120, 60))
    r.close()
    d = dt.datetime.fromtimestamp(base + 1).strftime("%Y-%m-%d")
    totals = db.daily_totals(d)
    assert totals["sessions"] == 2
    assert totals["distance_m"] == 290     # 200 + 90
    assert totals["steps"] == 380          # 260 + 120


def test_no_session_when_idle(tmp_path):
    db = Store(str(tmp_path / "t.db"))
    r = Recorder(db)
    base = 1716451200.0
    r.handle(mk(base + 1, 0.0, 0, 0, 0))
    r.handle(mk(base + 2, 0.0, 0, 0, 0))
    assert db.get_latest_session() is None


def test_avg_speed_computed(tmp_path):
    db = Store(str(tmp_path / "t.db"))
    r = Recorder(db)
    base = 1716451200.0
    # 1000 m in 600 s -> 6.0 km/h average
    r.handle(mk(base + 1, 6.0, 1000, 1300, 600))
    r.close()
    s = db.get_latest_session()
    assert abs(s["avg_speed_kmh"] - 6.0) < 0.001
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_recorder.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'walkingpad.recorder'`.

- [ ] **Step 3: Implement the recorder**

Create `src/walkingpad/recorder.py`:

```python
RESET_TOL_M = 20  # distance drop (metres) that signals the pad reset its counter


def avg_speed_kmh(distance_m, duration_s):
    if duration_s <= 0:
        return 0.0
    return (distance_m / 1000.0) / (duration_s / 3600.0)


class Recorder:
    """Turns a stream of PadStatus into persisted sessions.

    A session spans one continuous pad run (between counter resets / disconnects).
    The session row is updated on every status, so a crash loses at most the
    in-flight session's last second, never saved history.
    """

    def __init__(self, store):
        self.store = store
        self._active_id = None
        self._max_speed = 0.0
        self._last_distance_m = 0

    def handle(self, status):
        # Detect a pad counter reset -> the previous run is over.
        if (self._active_id is not None
                and status.distance_m + RESET_TOL_M < self._last_distance_m):
            self._close()
        self._last_distance_m = status.distance_m

        if self._active_id is None:
            if status.speed_kmh > 0 or status.distance_m > 0:
                self._open(status)
            else:
                return  # not walking, nothing to record

        self._max_speed = max(self._max_speed, status.speed_kmh)
        self.store.update_session(
            self._active_id,
            end_ts=status.ts,
            duration_s=status.elapsed_s,
            distance_m=status.distance_m,
            steps=status.steps,
            avg_speed_kmh=avg_speed_kmh(status.distance_m, status.elapsed_s),
            max_speed_kmh=self._max_speed,
        )
        self.store.add_sample(self._active_id, status.ts, status.speed_kmh,
                              status.distance_m, status.steps, status.belt_state)

    def _open(self, status):
        self._active_id = self.store.create_session(start_ts=status.ts)
        self._max_speed = 0.0

    def _close(self):
        self._active_id = None
        self._max_speed = 0.0

    def close(self):
        """Finalize any in-progress session (call on disconnect/shutdown)."""
        if self._active_id is not None:
            self._close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_recorder.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/walkingpad/recorder.py tests/test_recorder.py
git commit -m "feat: add recorder with session detection and accumulation"
```

---

## Task 5: BLE adapter (`pad_client`)

**Files:**

- Create: `src/walkingpad/pad_client.py`
- Test: `tests/test_pad_speed.py`

`change_speed` takes an int in 0.1 km/h units (e.g. `30` = 3.0 km/h); valid belt range 0.5–6.0 km/h. The pure conversion is unit-tested; the BLE methods are exercised in the Task 7 manual run.

- [ ] **Step 1: Write the failing test**

Create `tests/test_pad_speed.py`:

```python
from walkingpad.pad_client import kmh_to_pad_speed


def test_kmh_to_pad_speed_basic():
    assert kmh_to_pad_speed(3.0) == 30
    assert kmh_to_pad_speed(0.5) == 5
    assert kmh_to_pad_speed(6.0) == 60


def test_kmh_to_pad_speed_clamps():
    assert kmh_to_pad_speed(10.0) == 60   # above max -> clamp to 6.0 km/h
    assert kmh_to_pad_speed(0.1) == 5     # below min -> clamp to 0.5 km/h
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pad_speed.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'walkingpad.pad_client'`.

- [ ] **Step 3: Implement the adapter**

Create `src/walkingpad/pad_client.py`:

```python
import asyncio
import time

from bleak import BleakScanner
from ph4_walkingpad.pad import Controller

from walkingpad.status import cur_status_to_padstatus


def kmh_to_pad_speed(kmh):
    """Convert km/h to the pad's 0.1 km/h integer units, clamped to 0.5-6.0."""
    raw = int(round(kmh * 10))
    return max(5, min(60, raw))


class PadClient:
    """Thin async adapter over ph4_walkingpad.Controller."""

    def __init__(self, name_hint="WalkingPad"):
        self.name_hint = name_hint
        self.ctrl = Controller()
        self._callback = None
        self._address = None
        # ph4 may call the handler as (sender, record) or (record); take last arg.
        self.ctrl.handler_cur_status = self._on_cur_status

    def _on_cur_status(self, *args):
        record = args[-1]
        if self._callback and record is not None:
            self._callback(cur_status_to_padstatus(record, time.time()))

    async def discover_address(self):
        devices = await BleakScanner.discover(timeout=8.0)
        for d in devices:
            if d.name and self.name_hint.lower() in d.name.lower():
                return d.address
        return None

    async def connect(self, address=None):
        if address is None:
            address = await self.discover_address()
        if address is None:
            raise RuntimeError(
                "WalkingPad not found. Is it powered on and the official app closed?"
            )
        await self.ctrl.run(address)
        self._address = address
        return address

    async def disconnect(self):
        try:
            await self.ctrl.disconnect()
        except Exception:
            pass

    async def poll(self):
        await self.ctrl.ask_stats()

    async def start_belt(self):
        await self.ctrl.start_belt()

    async def stop_belt(self):
        await self.ctrl.stop_belt()

    async def set_speed(self, kmh):
        await self.ctrl.change_speed(kmh_to_pad_speed(kmh))

    async def capture(self, callback, interval=0.8):
        """Poll the pad forever, pushing PadStatus to callback.

        Raises on connection loss so the caller's reconnect loop can take over.
        """
        self._callback = callback
        while True:
            await self.poll()
            await asyncio.sleep(interval)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_pad_speed.py -v`
Expected: PASS (2 passed). Then run the full suite: `uv run pytest -v` → all green.

- [ ] **Step 5: Commit**

```bash
git add src/walkingpad/pad_client.py tests/test_pad_speed.py
git commit -m "feat: add BLE adapter over ph4-walkingpad with speed conversion"
```

---

## Task 6: CLI (`capture` / `today` / `sessions`)

**Files:**

- Create: `src/walkingpad/cli.py`

This task has no unit test (it is thin glue + a reconnect loop over the tested core); it is validated by the manual run in Task 7.

- [ ] **Step 1: Implement the CLI**

Create `src/walkingpad/cli.py`:

```python
import argparse
import asyncio
import datetime as dt
import os

from walkingpad.store import Store
from walkingpad.recorder import Recorder
from walkingpad.pad_client import PadClient


def default_db_path():
    base = os.path.expanduser("~/.local/share/walkingpad")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "walkingpad.db")


def _print_status(status):
    print(f"  speed={status.speed_kmh:>4.1f} km/h  "
          f"dist={status.distance_m / 1000:>5.2f} km  "
          f"steps={status.steps:>5}  time={status.elapsed_s // 60}m",
          flush=True)


async def run_capture(db_path, address=None):
    store = Store(db_path)
    recorder = Recorder(store)

    def on_status(status):
        _print_status(status)
        recorder.handle(status)

    client = PadClient()
    backoff = 1
    while True:
        try:
            addr = await client.connect(address)
            print(f"connected to {addr}", flush=True)
            backoff = 1
            await client.capture(on_status)
        except Exception as e:
            recorder.close()
            await client.disconnect()
            print(f"connection lost ({e}); retrying in {backoff}s", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(30, backoff * 2)


def cmd_today(db_path):
    store = Store(db_path)
    today = dt.date.today().strftime("%Y-%m-%d")
    t = store.daily_totals(today)
    print(f"{today}: {t['distance_m'] / 1000:.2f} km, {t['steps']} steps, "
          f"{t['duration_s'] // 60} min, {t['sessions']} session(s)")


def cmd_sessions(db_path, date_str):
    store = Store(db_path)
    rows = store.conn.execute(
        """SELECT id, start_ts, distance_m, steps, duration_s, max_speed_kmh
           FROM sessions
           WHERE date(start_ts, 'unixepoch', 'localtime') = ?
           ORDER BY id""",
        (date_str,),
    ).fetchall()
    if not rows:
        print(f"No sessions on {date_str}.")
        return
    for r in rows:
        start = dt.datetime.fromtimestamp(r["start_ts"]).strftime("%H:%M")
        print(f"  #{r['id']} {start}  {r['distance_m'] / 1000:.2f} km  "
              f"{r['steps']} steps  {r['duration_s'] // 60} min  "
              f"max {r['max_speed_kmh']:.1f} km/h")


def main():
    parser = argparse.ArgumentParser(prog="walkingpad")
    parser.add_argument("--db", default=default_db_path(), help="SQLite path")
    sub = parser.add_subparsers(dest="command", required=True)

    p_cap = sub.add_parser("capture", help="connect and log walks")
    p_cap.add_argument("--address", default=os.environ.get("WALKINGPAD_ADDRESS"),
                       help="BLE address/UUID (default: auto-discover by name)")

    sub.add_parser("today", help="print today's totals")

    p_ses = sub.add_parser("sessions", help="list sessions for a date")
    p_ses.add_argument("--date", default=dt.date.today().strftime("%Y-%m-%d"))

    args = parser.parse_args()
    if args.command == "capture":
        asyncio.run(run_capture(args.db, args.address))
    elif args.command == "today":
        cmd_today(args.db)
    elif args.command == "sessions":
        cmd_sessions(args.db, args.date)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify the CLI loads (no hardware needed)**

Run: `uv run python -m walkingpad.cli today`
Expected: prints a line like `2026-05-23: 0.00 km, 0 steps, 0 min, 0 session(s)` (empty DB is fine).

- [ ] **Step 3: Commit**

```bash
git add src/walkingpad/cli.py
git commit -m "feat: add capture/today/sessions CLI"
```

---

## Task 7: Manual end-to-end validation + reset calibration

**Files:** none (manual verification; the spec's one open question gets answered here).

This task confirms the BLE path works against the real P1 and calibrates session-reset detection. **Requires:** pad powered on, official WalkingPad/KS Fit app force-closed (single-connection rule), Bluetooth permission granted to the terminal.

- [ ] **Step 1: Start capture and take a short walk**

Run: `uv run python -m walkingpad.cli capture`
Expected: `connected to <UUID>`, then a status line ~once per second. Walk ~2 minutes, then step off / stop the belt. Watch whether, on stop, the printed `dist` **freezes** (auto-pause) or **drops to 0.00** (reset). Note which. Press Ctrl-C to stop capture.

- [ ] **Step 2: Verify totals were recorded**

Run: `uv run python -m walkingpad.cli today`
Then: `uv run python -m walkingpad.cli sessions`
Expected: today's distance/steps roughly match what the belt display showed; at least one session listed.

- [ ] **Step 3: Confirm or adjust `RESET_TOL_M`**

If Step 1 showed the counter **drops to 0** on stop (true reset), the default `RESET_TOL_M = 20` in `src/walkingpad/recorder.py` is correct — a second walk will create a second session and `today` will sum both. Do a second short walk and re-check `sessions` shows two rows.

If the counter **freezes / keeps climbing** on resume (auto-pause, no reset), that is also handled — it stays one session and the daily total is still correct. No code change needed. Record the observed behaviour as a comment at the top of `recorder.py`:

```python
# Observed on WALKINGPAD P1 (2026-05-23): on stop the run counter <RESETS to 0 | FREEZES>.
```

- [ ] **Step 4: Commit the observation**

```bash
git add src/walkingpad/recorder.py
git commit -m "docs: record observed P1 counter-reset behaviour"
```

---

## Definition of Done (Plan 1)

- `uv run pytest` is all green (store, status, recorder, pad_speed, smoke).
- `walkingpad capture` connects to the real P1, logs live status, and survives a stop/resume without losing prior distance.
- `walkingpad today` / `sessions` report totals that match the belt display.
- The P1's reset-vs-pause behaviour is observed and recorded.

**Next:** Plan 2 — wrap the running capture process in a FastAPI app exposing `/status`, `/today`, `/history`, and `/control/{start,stop,speed}`, reusing `PadClient` + `Recorder` + `Store` unchanged.
