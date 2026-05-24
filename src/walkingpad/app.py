import datetime as dt
import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from walkingpad.health_export import build_yesterday

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")


class SpeedRequest(BaseModel):
    kmh: float


def _today_str():
    return dt.date.today().strftime("%Y-%m-%d")


def create_app(state):
    """Build the FastAPI app over a DaemonState (bind to 127.0.0.1 when served)."""
    app = FastAPI(title="WalkingPad")

    @app.get("/")
    def dashboard():
        return FileResponse(os.path.join(WEB_DIR, "dashboard.html"))

    @app.get("/status")
    def get_status():
        s = state.latest_status
        return {
            "connected": state.connected,
            "belt_state": (s.belt_state if s else None),
            "speed_kmh": (s.speed_kmh if s else 0.0),
            "distance_m": (s.distance_m if s else 0),
            "steps": (s.steps if s else 0),
            "elapsed_s": (s.elapsed_s if s else 0),
        }

    @app.get("/today")
    def get_today():
        d = _today_str()
        totals = state.store.daily_totals(d)
        totals["streak"] = state.store.current_streak()
        totals["date"] = d
        return totals

    @app.get("/sessions")
    def get_sessions(date: str | None = None):
        d = date or _today_str()
        return {"date": d, "sessions": state.store.list_sessions(d)}

    @app.get("/yesterday")
    def get_yesterday():
        # Flat one-record JSON for the iPhone Health Shortcut.
        return build_yesterday(state.store)

    @app.get("/history")
    def get_history(range: str = "week"):
        days = 30 if range == "month" else 7
        return {"range": range, "days": state.store.daily_series(days)}

    @app.post("/control/start")
    async def control_start():
        if not state.connected:
            raise HTTPException(status_code=503, detail="pad not connected")
        await state.start_belt()
        return {"ok": True}

    @app.post("/control/stop")
    async def control_stop():
        if not state.connected:
            raise HTTPException(status_code=503, detail="pad not connected")
        await state.stop_belt()
        return {"ok": True}

    @app.post("/control/speed")
    async def control_speed(req: SpeedRequest):
        if not state.connected:
            raise HTTPException(status_code=503, detail="pad not connected")
        await state.set_speed(req.kmh)
        return {"ok": True, "kmh": req.kmh}

    return app
