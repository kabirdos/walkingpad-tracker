import datetime as dt
import json

from walkingpad.obsidian_export import (
    build_payload,
    vault_export_path,
    write_export,
)
from walkingpad.state import DaemonState
from walkingpad.status import PadStatus
from walkingpad.store import Store


def _add_session(db, ts, dist, steps, dur):
    sid = db.create_session(ts)
    db.update_session(
        sid, end_ts=ts + dur, duration_s=dur, distance_m=dist,
        steps=steps, avg_speed_kmh=5.0, max_speed_kmh=5.5,
    )


def _state_with(db, status=None, connected=False):
    s = DaemonState(db)
    s.connected = connected
    if status is not None:
        s.record_status(status)
    return s


def test_vault_export_path_appends_dashboard_subdir(tmp_path):
    assert vault_export_path(str(tmp_path)).endswith("/.dashboard/walkingpad.json")


def test_payload_disconnected_idle(tmp_path):
    db = Store(str(tmp_path / "t.db"))
    p = build_payload(_state_with(db), db)
    assert p["connected"] is False
    assert p["live"]["speed_mph"] == 0
    assert p["live"]["is_running"] is False
    assert p["today"]["steps"] == 0
    assert p["streak_days"] == 0


def test_payload_running_today(tmp_path):
    db = Store(str(tmp_path / "t.db"))
    today = dt.date.today()
    t = dt.datetime.combine(today, dt.time(9, 0))
    _add_session(db, t.timestamp(), dist=2000, steps=2600, dur=1200)
    status = PadStatus(
        ts=t.timestamp(), speed_kmh=5.0, distance_m=2000, steps=2600,
        elapsed_s=1200, belt_state=1,
    )
    p = build_payload(_state_with(db, status=status, connected=True), db)
    assert p["connected"] is True
    assert p["live"]["is_running"] is True
    assert p["live"]["speed_mph"] == round(5.0 * 0.621371, 2)
    assert p["live"]["belt_state"] == 1
    assert p["today"]["steps"] == 2600
    assert p["today"]["distance_mi"] == round(2.0 * 0.621371, 2)
    assert p["today"]["duration_min"] == 20
    assert p["today"]["sessions"] == 1


def test_spark_7d_is_always_seven_oldest_first_with_gaps_zero_filled(tmp_path):
    db = Store(str(tmp_path / "t.db"))
    today = dt.date.today()
    # Walks today and 2 days ago only — days in between/before should fill 0.
    for offset in (0, 2):
        d = dt.datetime.combine(today - dt.timedelta(days=offset), dt.time(9, 0))
        _add_session(db, d.timestamp(), dist=1000, steps=1300, dur=600)
    p = build_payload(_state_with(db), db)
    assert len(p["spark_7d"]) == 7
    # Oldest first; last entry is today.
    assert p["spark_7d"][-1]["date"] == today.strftime("%Y-%m-%d")
    assert p["spark_7d"][-1]["steps"] == 1300
    assert p["spark_7d"][-3]["steps"] == 1300  # two days ago
    assert p["spark_7d"][-2]["steps"] == 0     # yesterday — gap filled
    assert p["spark_7d"][0]["steps"] == 0      # 6 days ago — no walk


def test_write_export_creates_dashboard_subdir_and_writes_atomic(tmp_path):
    db = Store(str(tmp_path / "t.db"))
    today = dt.date.today()
    t = dt.datetime.combine(today, dt.time(9, 0))
    _add_session(db, t.timestamp(), dist=500, steps=650, dur=300)
    state = _state_with(db, connected=True)

    out = write_export(state, db, str(tmp_path / "vault"))
    assert out.endswith("/.dashboard/walkingpad.json")

    data = json.loads(open(out).read())
    assert "generated_at" in data
    assert data["today"]["steps"] == 650
    # No partial .tmp file left behind after a successful write.
    assert not (tmp_path / "vault" / ".dashboard" / "walkingpad.json.tmp").exists()
