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
