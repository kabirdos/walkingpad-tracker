import datetime as dt
import json

from walkingpad.store import Store
from walkingpad.health_export import build_export, write_export


def _add(db, ts, dist, steps, dur):
    sid = db.create_session(ts)
    db.update_session(sid, end_ts=ts + dur, duration_s=dur, distance_m=dist,
                      steps=steps, avg_speed_kmh=5.0, max_speed_kmh=5.5)


def test_build_export(tmp_path):
    db = Store(str(tmp_path / "t.db"))
    today = dt.date.today()
    t = dt.datetime.combine(today, dt.time(9, 0))
    _add(db, t.timestamp(), 1000, 1300, 600)
    out = build_export(db, days=7)
    assert len(out) == 1
    row = out[0]
    assert row["date"] == today.strftime("%Y-%m-%d")
    assert row["distance_km"] == 1.0
    assert row["distance_mi"] == 0.621  # 1.0 km in miles
    assert row["steps"] == 1300
    assert row["duration_min"] == 10
    assert row["sessions"] == 1


def test_write_export_atomic_json(tmp_path):
    db = Store(str(tmp_path / "t.db"))
    t = dt.datetime.combine(dt.date.today(), dt.time(9, 0))
    _add(db, t.timestamp(), 500, 650, 300)
    path = tmp_path / "sub" / "daily.json"
    written = write_export(db, str(path), days=7)
    assert written == str(path)
    data = json.loads(path.read_text())
    assert "generated_at" in data
    assert data["days"][0]["distance_km"] == 0.5
    assert data["days"][0]["distance_mi"] == 0.311  # 0.5 km in miles
    assert data["days"][0]["duration_min"] == 5
