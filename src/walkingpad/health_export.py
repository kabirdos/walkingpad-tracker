"""Write a daily-totals summary into iCloud Drive for the iPhone Shortcut.

The Shortcut reads `daily.json`, picks yesterday's entry, and logs a Walking
workout into Apple Health. We can't write HealthKit from macOS directly, so
this file is the bridge.
"""
import datetime as dt
import json
import os

MI = 0.621371  # km -> miles


def default_export_dir():
    return os.path.expanduser(
        "~/Library/Mobile Documents/com~apple~CloudDocs/WalkingPad"
    )


def build_export(store, days=60):
    rows = store.daily_series(days)
    return [
        {
            "date": row["day"],
            "distance_km": round((row["distance_m"] or 0) / 1000, 3),
            "distance_mi": round((row["distance_m"] or 0) / 1000 * MI, 3),
            "steps": int(row["steps"] or 0),
            "duration_min": round((row["duration_s"] or 0) / 60),
            "sessions": int(row["sessions"] or 0),
        }
        for row in rows
    ]


def write_export(store, path=None, days=60):
    path = path or os.path.join(default_export_dir(), "daily.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "days": build_export(store, days),
    }
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)  # atomic
    return path
