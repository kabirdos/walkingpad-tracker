"""Pure helpers for the menubar widget (no GUI deps, so they're testable)."""

MI = 0.621371  # km -> miles, and km/h -> mph


def format_title(status, today):
    """The short string shown in the macOS menu bar.

    Moving -> live speed (mph); connected-idle -> today's distance; pad asleep
    (daemon reachable but reports no BLE link) -> a sleep symbol with today's
    distance, so it's obvious the remote is needed to wake it.
    """
    mi = ((today or {}).get("distance_m", 0) or 0) / 1000 * MI
    if status and status.get("connected"):
        if (status.get("speed_kmh") or 0) > 0:
            return f"▸ {status['speed_kmh'] * MI:.1f} mph"
        return f"\U0001f6b6 {mi:.2f} mi"
    if status is not None:  # daemon reachable but pad disconnected -> asleep
        return f"⏾ {mi:.2f} mi"
    return f"\U0001f6b6 {mi:.2f} mi"


def summary_lines(today):
    """The detail lines shown in the dropdown."""
    t = today or {}
    mi = (t.get("distance_m", 0) or 0) / 1000 * MI
    mins = round((t.get("duration_s", 0) or 0) / 60)
    return [
        f"Distance: {mi:.2f} mi",
        f"Steps: {int(t.get('steps', 0) or 0):,}",
        f"Active: {mins} min",
        f"Streak: {int(t.get('streak', 0) or 0)} d",
    ]
