import datetime as dt

# Observed live on WALKINGPAD P1 (2026-05-23): on stop, the run counter RESETS
# to 0 in a single tick (0.08 km / 155 steps -> 0 / 0), then idles at zero while
# still connected. RESET_TOL_M catches the drop and finalizes the session; the
# trailing zero-ticks never open a new one (validated: today == 1 session).
RESET_TOL_M = 20  # distance drop (metres) that signals the pad reset its counter


def _local_day(ts):
    """Calendar day of a timestamp in local time — matches the SQL bucketing
    `date(start_ts, 'unixepoch', 'localtime')` used for daily totals."""
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def avg_speed_kmh(distance_m, duration_s):
    if duration_s <= 0:
        return 0.0
    return (distance_m / 1000.0) / (duration_s / 3600.0)


class Recorder:
    """Turns a stream of PadStatus into persisted sessions.

    A session spans one continuous pad run (between counter resets / disconnects).
    The session row is updated on every status, so a crash loses at most the
    in-flight session's last second, never saved history.
    """

    def __init__(self, store):
        self.store = store
        self._active_id = None
        self._max_speed = 0.0
        self._last_distance_m = 0
        self._last_steps = 0
        self._last_elapsed = 0
        self._session_day = None
        # Cumulative counter values carried over from a run that spilled past
        # midnight, so the new day's session is measured from zero (the pad's
        # counter keeps climbing across midnight; only a real stop resets it).
        self._base_distance = 0
        self._base_steps = 0
        self._base_elapsed = 0

    def handle(self, status):
        # Detect a pad counter reset -> the previous run is over and the pad's
        # counter has zeroed, so any carried-over offset is now stale. Clear it
        # even when no session is active (e.g. the belt was reset while idle).
        if status.distance_m + RESET_TOL_M < self._last_distance_m:
            if self._active_id is not None:
                self._close()
            self._base_distance = self._base_steps = self._base_elapsed = 0

        # Roll a still-running session over at local midnight: finalize the
        # current day's session and carry the cumulative counter as the new
        # session's zero point so the new day is measured from there.
        if (self._active_id is not None
                and self._session_day is not None
                and _local_day(status.ts) != self._session_day):
            self._close()
            self._base_distance = self._last_distance_m
            self._base_steps = self._last_steps
            self._base_elapsed = self._last_elapsed

        self._last_distance_m = status.distance_m
        self._last_steps = status.steps
        self._last_elapsed = status.elapsed_s

        # Everything below is measured relative to the active session's zero
        # point (0 for a fresh run, the carry-over for a rolled-over one).
        # Clamp to >= 0 so a stray sub-baseline pad reading can't log a negative.
        distance_m = max(0, status.distance_m - self._base_distance)
        steps = max(0, status.steps - self._base_steps)
        duration_s = max(0, status.elapsed_s - self._base_elapsed)

        if self._active_id is None:
            if status.speed_kmh > 0 or distance_m > 0:
                self._open(status)
            else:
                return  # not walking / no new distance, nothing to record
        elif self._session_day is None:
            # Resumed without a known start day; adopt the current day so a
            # later midnight crossing still rolls the session over.
            self._session_day = _local_day(status.ts)

        self._max_speed = max(self._max_speed, status.speed_kmh)
        self.store.update_session(
            self._active_id,
            end_ts=status.ts,
            duration_s=duration_s,
            distance_m=distance_m,
            steps=steps,
            avg_speed_kmh=avg_speed_kmh(distance_m, duration_s),
            max_speed_kmh=self._max_speed,
        )
        self.store.add_sample(self._active_id, status.ts, status.speed_kmh,
                              distance_m, steps, status.belt_state)

    def _open(self, status):
        self._active_id = self.store.create_session(
            start_ts=status.ts,
            origin_distance=self._base_distance,
            origin_steps=self._base_steps,
            origin_elapsed=self._base_elapsed,
        )
        self._max_speed = 0.0
        self._session_day = _local_day(status.ts)

    def resume(self, session):
        """Re-attach to an existing session after a reconnect/restart mid-run,
        so the ongoing walk isn't double-counted as a new session. Restores the
        session's day and counter offset from the row, so it still rolls over at
        midnight and a rolled-over session isn't re-inflated to the raw counter."""
        self._active_id = session["id"]
        self._base_distance = session["origin_distance"]
        self._base_steps = session["origin_steps"]
        self._base_elapsed = session["origin_elapsed"]
        # Track the pad's raw counter (recorded total + offset) so reset
        # detection and a further midnight rollover both work after resuming.
        self._last_distance_m = session["distance_m"] + self._base_distance
        self._last_steps = session["steps"] + self._base_steps
        self._last_elapsed = session["duration_s"] + self._base_elapsed
        self._max_speed = session["max_speed_kmh"] or 0.0
        self._session_day = _local_day(session["start_ts"])

    def _close(self):
        self._active_id = None
        self._max_speed = 0.0

    def close(self):
        """Finalize any in-progress session (call on disconnect/shutdown)."""
        if self._active_id is not None:
            self._close()
