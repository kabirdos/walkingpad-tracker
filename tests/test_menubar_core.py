from walkingpad.menubar_core import format_title, summary_lines


def test_title_moving_shows_mph():
    s = {"connected": True, "speed_kmh": 3.2}  # 3.2 km/h ≈ 2.0 mph
    assert format_title(s, {"distance_m": 80}) == "▸ 2.0 mph"


def test_title_idle_shows_today_miles():
    s = {"connected": True, "speed_kmh": 0.0}
    assert format_title(s, {"distance_m": 80}) == "\U0001f6b6 0.05 mi"


def test_title_offline_shows_today_miles():
    assert format_title({"connected": False}, {"distance_m": 250}) == "\U0001f6b6 0.16 mi"


def test_title_handles_missing():
    assert format_title(None, None) == "\U0001f6b6 0.00 mi"


def test_summary_lines():
    lines = summary_lines({"distance_m": 1234, "steps": 1600, "duration_s": 700, "streak": 3})
    assert lines[0] == "Distance: 0.77 mi"  # 1.234 km ≈ 0.767 mi
    assert lines[1] == "Steps: 1,600"
    assert lines[2] == "Active: 12 min"
    assert lines[3] == "Streak: 3 d"


def test_summary_lines_empty():
    assert summary_lines(None) == [
        "Distance: 0.00 mi", "Steps: 0", "Active: 0 min", "Streak: 0 d",
    ]
