import datetime as dt
from walkingpad.store import Store


def _add_session(db, ts, dist, steps):
    sid = db.create_session(ts)
    db.update_session(sid, end_ts=ts + 300, duration_s=300, distance_m=dist,
                      steps=steps, avg_speed_kmh=5.0, max_speed_kmh=5.5)
    return sid


def test_list_sessions(tmp_path):
    db = Store(str(tmp_path / "t.db"))
    today = dt.datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
    _add_session(db, today.timestamp(), 500, 650)
    _add_session(db, (today + dt.timedelta(hours=1)).timestamp(), 700, 900)
    rows = db.list_sessions(today.strftime("%Y-%m-%d"))
    assert len(rows) == 2
    assert rows[0]["distance_m"] == 500
    assert rows[1]["steps"] == 900


def test_daily_series(tmp_path):
    db = Store(str(tmp_path / "t.db"))
    today = dt.date.today()
    t_today = dt.datetime.combine(today, dt.time(9, 0))
    _add_session(db, t_today.timestamp(), 1000, 1300)
    two_ago = t_today - dt.timedelta(days=2)
    _add_session(db, two_ago.timestamp(), 400, 520)
    series = db.daily_series(7)
    days = {row["day"]: row for row in series}
    assert days[today.strftime("%Y-%m-%d")]["distance_m"] == 1000
    assert days[(today - dt.timedelta(days=2)).strftime("%Y-%m-%d")]["distance_m"] == 400
    assert len(series) == 2


def test_current_streak_counts_consecutive_days(tmp_path):
    db = Store(str(tmp_path / "t.db"))
    today = dt.date.today()
    for delta in (0, 1, 2):
        day = dt.datetime.combine(today - dt.timedelta(days=delta), dt.time(9, 0))
        _add_session(db, day.timestamp(), 500, 650)
    assert db.current_streak() == 3


def test_current_streak_breaks_on_gap(tmp_path):
    db = Store(str(tmp_path / "t.db"))
    today = dt.date.today()
    for delta in (0, 3):
        day = dt.datetime.combine(today - dt.timedelta(days=delta), dt.time(9, 0))
        _add_session(db, day.timestamp(), 500, 650)
    assert db.current_streak() == 1


def test_current_streak_zero_when_empty(tmp_path):
    db = Store(str(tmp_path / "t.db"))
    assert db.current_streak() == 0
