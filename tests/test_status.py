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
