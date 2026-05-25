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


def test_resume_continues_session_no_double_count(tmp_path):
    db = Store(str(tmp_path / "t.db"))
    base = 1716451200.0
    # first recorder records a walk up to 200 m
    r1 = Recorder(db)
    r1.handle(mk(base + 1, 3.0, 100, 130, 60))
    r1.handle(mk(base + 2, 3.0, 200, 260, 120))
    # process "restarts" mid-walk: a fresh recorder resumes the open session
    sess = db.get_latest_session()
    r2 = Recorder(db)
    r2.resume(sess)
    # pad keeps counting the SAME run (cumulative grows past 200)
    r2.handle(mk(base + 3, 3.0, 260, 340, 150))
    r2.handle(mk(base + 4, 3.0, 320, 420, 180))
    r2.close()
    d = dt.datetime.fromtimestamp(base + 1).strftime("%Y-%m-%d")
    totals = db.daily_totals(d)
    assert totals["sessions"] == 1       # ONE session, not two
    assert totals["distance_m"] == 320   # final cumulative, not 200 + 320
    assert totals["steps"] == 420


def test_resume_after_midnight_rollover_does_not_inflate(tmp_path):
    # After a midnight rollover the new day's session carries a non-zero offset.
    # A daemon restart must restore that offset from the DB, or the resumed
    # session re-records the full cumulative counter (a whole day's inflation).
    db = Store(str(tmp_path / "t.db"))
    midnight = dt.datetime(2026, 5, 25, 0, 0, 0).timestamp()
    r1 = Recorder(db)
    r1.handle(mk(midnight - 60, 3.0, dist_m=1000, steps=1300, elapsed=600))  # day1, pad@1000
    r1.handle(mk(midnight + 60, 3.0, dist_m=1020, steps=1326, elapsed=612))  # rollover -> day2

    sess = db.get_latest_session()  # the day-2 session, carrying a 1000 m offset
    r2 = Recorder(db)
    r2.resume(sess)
    r2.handle(mk(midnight + 120, 3.0, dist_m=1080, steps=1404, elapsed=648))  # pad@1080
    r2.close()

    day2 = dt.datetime.fromtimestamp(midnight + 60).strftime("%Y-%m-%d")
    t2 = db.daily_totals(day2)
    assert t2["sessions"] == 1
    assert t2["distance_m"] == 80     # 1080 - 1000 offset, NOT 1080
    assert t2["steps"] == 104         # 1404 - 1300


def test_resume_of_previous_day_session_attributes_to_today(tmp_path):
    # A resume just after midnight reattaches to a session that started
    # yesterday; new walking must roll over to today, not pile onto yesterday.
    db = Store(str(tmp_path / "t.db"))
    midnight = dt.datetime(2026, 5, 25, 0, 0, 0).timestamp()
    r1 = Recorder(db)
    r1.handle(mk(midnight - 30, 3.0, dist_m=1000, steps=1300, elapsed=600))  # day1

    sess = db.get_latest_session()  # started yesterday, still open
    r2 = Recorder(db)
    r2.resume(sess)
    r2.handle(mk(midnight + 30, 3.0, dist_m=1060, steps=1378, elapsed=636))  # now day2
    r2.close()

    day1 = dt.datetime.fromtimestamp(midnight - 30).strftime("%Y-%m-%d")
    day2 = dt.datetime.fromtimestamp(midnight + 30).strftime("%Y-%m-%d")
    assert db.daily_totals(day1)["distance_m"] == 1000   # yesterday keeps its total
    assert db.daily_totals(day2)["sessions"] == 1        # a fresh today session
    assert db.daily_totals(day2)["distance_m"] == 60     # 1060 - 1000 carried over


def test_independent_elapsed_reset_does_not_log_negative_duration(tmp_path):
    # Defensive: if the pad's elapsed timer glitches below the carried offset
    # without a distance reset, duration must clamp to >= 0, not corrupt totals.
    db = Store(str(tmp_path / "t.db"))
    midnight = dt.datetime(2026, 5, 25, 0, 0, 0).timestamp()
    r = Recorder(db)
    r.handle(mk(midnight - 60, 3.0, dist_m=1000, steps=1300, elapsed=600))   # day1
    r.handle(mk(midnight + 60, 3.0, dist_m=1020, steps=1326, elapsed=612))   # rollover, base_elapsed=600
    r.handle(mk(midnight + 120, 3.0, dist_m=1040, steps=1352, elapsed=30))   # elapsed glitch below base
    r.close()

    day2 = dt.datetime.fromtimestamp(midnight + 60).strftime("%Y-%m-%d")
    assert db.daily_totals(day2)["duration_s"] >= 0


def test_session_rolls_over_at_local_midnight(tmp_path):
    # A continuous run whose pad counter never resets but whose samples cross
    # local midnight must be split into one session per calendar day, with the
    # cumulative counter offset so the new day starts from zero (no double-count,
    # no loss). Regression: session 5 logged a full day's steps under the prior
    # day because the recorder bucketed everything by the session's start date.
    db = Store(str(tmp_path / "t.db"))
    r = Recorder(db)
    midnight = dt.datetime(2026, 5, 25, 0, 0, 0).timestamp()  # local midnight
    r.handle(mk(midnight - 120, 3.0, dist_m=100, steps=130, elapsed=60))
    r.handle(mk(midnight - 60,  3.0, dist_m=200, steps=260, elapsed=120))  # last of day1
    r.handle(mk(midnight + 60,  3.0, dist_m=320, steps=420, elapsed=180))  # first of day2
    r.handle(mk(midnight + 120, 3.0, dist_m=420, steps=550, elapsed=240))  # last of day2
    r.close()

    day1 = dt.datetime.fromtimestamp(midnight - 60).strftime("%Y-%m-%d")
    day2 = dt.datetime.fromtimestamp(midnight + 60).strftime("%Y-%m-%d")
    t1 = db.daily_totals(day1)
    t2 = db.daily_totals(day2)

    assert t1["sessions"] == 1
    assert t2["sessions"] == 1
    assert t1["distance_m"] == 200          # up to the midnight crossing
    assert t2["distance_m"] == 220          # 420 cumulative - 200 carried over
    assert t1["steps"] == 260
    assert t2["steps"] == 290               # 550 - 260
    # the run total is preserved exactly across the split
    assert t1["distance_m"] + t2["distance_m"] == 420
    assert t1["steps"] + t2["steps"] == 550


def test_idle_belt_across_midnight_makes_no_phantom_session(tmp_path):
    # The bug's real shape: the belt is left on (counter not reset) and idles
    # across midnight. Rolling over must finalize day 1 but NOT spawn an empty
    # day-2 session just because the cumulative counter is still non-zero.
    db = Store(str(tmp_path / "t.db"))
    r = Recorder(db)
    midnight = dt.datetime(2026, 5, 25, 0, 0, 0).timestamp()
    r.handle(mk(midnight - 120, 3.0, dist_m=100, steps=130, elapsed=60))   # walking
    r.handle(mk(midnight - 60,  0.0, dist_m=200, steps=260, elapsed=120))  # stopped, not reset
    r.handle(mk(midnight + 60,  0.0, dist_m=200, steps=260, elapsed=120))  # idle, past midnight
    r.handle(mk(midnight + 120, 0.0, dist_m=200, steps=260, elapsed=120))  # still idle
    r.close()

    day1 = dt.datetime.fromtimestamp(midnight - 60).strftime("%Y-%m-%d")
    day2 = dt.datetime.fromtimestamp(midnight + 60).strftime("%Y-%m-%d")
    assert db.daily_totals(day1)["sessions"] == 1     # day 1 finalized normally
    assert db.daily_totals(day1)["distance_m"] == 200
    assert db.daily_totals(day2)["sessions"] == 0     # no phantom day-2 session


def test_reset_after_midnight_rollover_clears_carryover(tmp_path):
    # After an idle belt rolls over at midnight (carrying a non-zero offset but
    # opening no session), a real pad reset must clear that carry-over so the
    # next walk records its true distance instead of a negative one.
    db = Store(str(tmp_path / "t.db"))
    r = Recorder(db)
    midnight = dt.datetime(2026, 5, 25, 0, 0, 0).timestamp()
    r.handle(mk(midnight - 60,  3.0, dist_m=200, steps=260, elapsed=120))  # day1, counter=200
    r.handle(mk(midnight + 60,  0.0, dist_m=200, steps=260, elapsed=120))  # idle -> rollover, base=200
    r.handle(mk(midnight + 120, 0.0, dist_m=0,   steps=0,   elapsed=0))    # belt reset -> counter 0
    r.handle(mk(midnight + 180, 3.0, dist_m=80,  steps=104, elapsed=48))   # day2 walk, fresh counter
    r.handle(mk(midnight + 240, 3.0, dist_m=150, steps=195, elapsed=90))
    r.close()

    day2 = dt.datetime.fromtimestamp(midnight + 180).strftime("%Y-%m-%d")
    t2 = db.daily_totals(day2)
    assert t2["sessions"] == 1
    assert t2["distance_m"] == 150     # true distance, not 150 - 200 = -50
    assert t2["steps"] == 195


def test_avg_speed_computed(tmp_path):
    db = Store(str(tmp_path / "t.db"))
    r = Recorder(db)
    base = 1716451200.0
    # 1000 m in 600 s -> 6.0 km/h average
    r.handle(mk(base + 1, 6.0, 1000, 1300, 600))
    r.close()
    s = db.get_latest_session()
    assert abs(s["avg_speed_kmh"] - 6.0) < 0.001
