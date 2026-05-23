RESET_TOL_M = 20  # distance drop (metres) that signals the pad reset its counter


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

    def handle(self, status):
        # Detect a pad counter reset -> the previous run is over.
        if (self._active_id is not None
                and status.distance_m + RESET_TOL_M < self._last_distance_m):
            self._close()
        self._last_distance_m = status.distance_m

        if self._active_id is None:
            if status.speed_kmh > 0 or status.distance_m > 0:
                self._open(status)
            else:
                return  # not walking, nothing to record

        self._max_speed = max(self._max_speed, status.speed_kmh)
        self.store.update_session(
            self._active_id,
            end_ts=status.ts,
            duration_s=status.elapsed_s,
            distance_m=status.distance_m,
            steps=status.steps,
            avg_speed_kmh=avg_speed_kmh(status.distance_m, status.elapsed_s),
            max_speed_kmh=self._max_speed,
        )
        self.store.add_sample(self._active_id, status.ts, status.speed_kmh,
                              status.distance_m, status.steps, status.belt_state)

    def _open(self, status):
        self._active_id = self.store.create_session(start_ts=status.ts)
        self._max_speed = 0.0

    def _close(self):
        self._active_id = None
        self._max_speed = 0.0

    def close(self):
        """Finalize any in-progress session (call on disconnect/shutdown)."""
        if self._active_id is not None:
            self._close()
