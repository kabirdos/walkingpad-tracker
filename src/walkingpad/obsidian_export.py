"""Write a near-live JSON summary into an Obsidian vault for the personal
dashboard plugin's WalkingPad widget.

Mirrors health_export.py: atomic write so the plugin's vault-modify watcher
never sees a half-written file. Run from a 30s loop in cli.py's `serve` command
when a vault path is configured; opt-in (no path → nothing is written).

JSON shape is what the widget reads:
  generated_at    ISO timestamp
  connected       bool — BLE link to the pad
  live.speed_mph  current speed (0 if idle or disconnected)
  live.is_running speed > 0 — the simple "show live mph" signal
  live.belt_state raw integer for debugging; widget should prefer is_running
  today           dict (steps, distance_mi, duration_min, sessions)
  streak_days     int — consecutive days with distance > 0
  spark_7d        list of last 7 days (date, steps) for the mini sparkline
"""
import datetime as dt
import json
import os

MI = 0.621371  # km -> miles, km/h -> mph


def vault_export_path(vault_dir):
    return os.path.join(
        os.path.expanduser(vault_dir), ".dashboard", "walkingpad.json"
    )


def _filled_series(store, days):
    """Always return exactly `days` entries, oldest-first, with 0-filled gaps.
    The store's daily_series drops days with no sessions; the widget would
    rather render gaps than skip the bar, so fill here."""
    by_day = {row["day"]: int(row["steps"] or 0) for row in store.daily_series(days)}
    today = dt.date.today()
    out = []
    for i in range(days - 1, -1, -1):
        d = (today - dt.timedelta(days=i)).strftime("%Y-%m-%d")
        out.append({"date": d, "steps": by_day.get(d, 0)})
    return out


def build_payload(state, store, days=7):
    today_str = dt.date.today().strftime("%Y-%m-%d")
    today = store.daily_totals(today_str)
    streak = store.current_streak()
    status = state.latest_status

    speed_kmh = (status.speed_kmh if status else 0.0) or 0.0
    return {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "connected": bool(state.connected),
        "live": {
            "speed_mph": round(speed_kmh * MI, 2),
            "is_running": speed_kmh > 0,
            "belt_state": (status.belt_state if status else None),
        },
        "today": {
            "steps": int(today.get("steps", 0) or 0),
            "distance_mi": round(
                (today.get("distance_m", 0) or 0) / 1000 * MI, 2
            ),
            "duration_min": round((today.get("duration_s", 0) or 0) / 60),
            "sessions": int(today.get("sessions", 0) or 0),
        },
        "streak_days": int(streak),
        "spark_7d": _filled_series(store, days),
    }


def _atomic_write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def write_export(state, store, vault_dir):
    path = vault_export_path(vault_dir)
    _atomic_write_json(path, build_payload(state, store))
    return path
