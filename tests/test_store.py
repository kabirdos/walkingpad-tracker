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
