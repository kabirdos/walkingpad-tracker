"""Pure helpers for the menubar widget (no GUI deps, so they're testable)."""


def format_title(status, today):
    """The short string shown in the macOS menu bar.

    Moving -> live speed; otherwise today's distance.
    """
    km = ((today or {}).get("distance_m", 0) or 0) / 1000
    if status and status.get("connected") and (status.get("speed_kmh") or 0) > 0:
        return f"▸ {status['speed_kmh']:.1f} km/h"
    return f"\U0001f6b6 {km:.2f} km"


def summary_lines(today):
    """The detail lines shown in the dropdown."""
    t = today or {}
    km = (t.get("distance_m", 0) or 0) / 1000
    mins = round((t.get("duration_s", 0) or 0) / 60)
    return [
        f"Distance: {km:.2f} km",
        f"Steps: {int(t.get('steps', 0) or 0):,}",
        f"Active: {mins} min",
        f"Streak: {int(t.get('streak', 0) or 0)} d",
    ]
