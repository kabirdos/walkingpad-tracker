from walkingpad.pad_client import kmh_to_pad_speed


def test_kmh_to_pad_speed_basic():
    assert kmh_to_pad_speed(3.0) == 30
    assert kmh_to_pad_speed(0.5) == 5
    assert kmh_to_pad_speed(6.0) == 60


def test_kmh_to_pad_speed_clamps():
    assert kmh_to_pad_speed(10.0) == 60   # above max -> clamp to 6.0 km/h
    assert kmh_to_pad_speed(0.1) == 5     # below min -> clamp to 0.5 km/h
