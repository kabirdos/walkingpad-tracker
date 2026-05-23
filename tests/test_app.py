import datetime as dt

from fastapi.testclient import TestClient

from walkingpad.store import Store
from walkingpad.status import PadStatus
from walkingpad.state import DaemonState
from walkingpad.app import create_app


class FakePad:
    def __init__(self):
        self.calls = []

    async def start_belt(self):
        self.calls.append(("start",))

    async def stop_belt(self):
        self.calls.append(("stop",))

    async def set_speed(self, kmh):
        self.calls.append(("speed", kmh))


def make_client(tmp_path, connected=True):
    store = Store(str(tmp_path / "t.db"))
    pad = FakePad()
    state = DaemonState(store, pad_client=pad)
    state.connected = connected
    return TestClient(create_app(state)), state, pad


def test_dashboard_served(tmp_path):
    client, state, pad = make_client(tmp_path)
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Walking" in r.text
    assert "/control/speed" in r.text  # the dashboard wires up controls


def test_status_empty(tmp_path):
    client, state, pad = make_client(tmp_path)
    body = client.get("/status").json()
    assert body["connected"] is True
    assert body["speed_kmh"] == 0.0
    assert body["steps"] == 0


def test_status_reflects_latest(tmp_path):
    client, state, pad = make_client(tmp_path)
    state.record_status(PadStatus(ts=1.0, speed_kmh=3.0, distance_m=500,
                                  steps=650, elapsed_s=300, belt_state=1))
    body = client.get("/status").json()
    assert body["speed_kmh"] == 3.0
    assert body["distance_m"] == 500
    assert body["belt_state"] == 1


def test_today_and_sessions(tmp_path):
    client, state, pad = make_client(tmp_path)
    now = dt.datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
    sid = state.store.create_session(now.timestamp())
    state.store.update_session(sid, end_ts=now.timestamp() + 300, duration_s=300,
                               distance_m=800, steps=1000, avg_speed_kmh=5.0,
                               max_speed_kmh=5.5)
    today = client.get("/today").json()
    assert today["distance_m"] == 800
    assert today["sessions"] == 1
    assert "streak" in today
    sess = client.get("/sessions").json()
    assert len(sess["sessions"]) == 1
    assert sess["sessions"][0]["steps"] == 1000


def test_history(tmp_path):
    client, state, pad = make_client(tmp_path)
    body = client.get("/history?range=month").json()
    assert body["range"] == "month"
    assert isinstance(body["days"], list)


def test_control_calls_pad(tmp_path):
    client, state, pad = make_client(tmp_path)
    assert client.post("/control/start").json() == {"ok": True}
    assert client.post("/control/stop").json() == {"ok": True}
    r = client.post("/control/speed", json={"kmh": 3.5})
    assert r.json() == {"ok": True, "kmh": 3.5}
    assert ("start",) in pad.calls
    assert ("stop",) in pad.calls
    assert ("speed", 3.5) in pad.calls


def test_control_503_when_disconnected(tmp_path):
    client, state, pad = make_client(tmp_path, connected=False)
    assert client.post("/control/start").status_code == 503
