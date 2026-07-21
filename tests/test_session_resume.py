"""The daemon re-attaching to an in-progress session after a restart.

The failure this guards (issue #18): a wedged scanner mid-walk takes ~2 minutes
to detect, so a 30s resume window could never fire. The replacement process
opened a *second* session and, because the pad's counters are cumulative,
re-recorded everything the first one already had.

Widening the window alone is not enough, and is dangerous on its own: sessions
carry no open/closed flag, so a purely time-based resume can re-attach to a walk
that really ended and overwrite it. Continuity is therefore decided from the
pad's counters, with the window as an outer bound.
"""
import asyncio
import contextlib
import datetime as dt
import time

import walkingpad.cli as cli
import walkingpad.health_export as health_export
import walkingpad.pad_client as pad_client
from walkingpad.recorder import Recorder
from walkingpad.status import PadStatus
from walkingpad.store import Store


def mk(ts, speed, dist_m, steps, elapsed):
    return PadStatus(ts=ts, speed_kmh=speed, distance_m=dist_m,
                     steps=steps, elapsed_s=elapsed, belt_state=1)


def _day_of(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _seed_session(store, ts, dist_m, steps, elapsed):
    """Record one status so the store holds a session left open mid-walk."""
    Recorder(store).handle(mk(ts, 3.2, dist_m, steps, elapsed))
    return store.get_latest_session()


def _detection_cycle_s(from_live_connection):
    """One pass of empty scans, ending in a re-exec.

    Derived from the live constants rather than hardcoded, so tightening the
    backoff or a timeout can't silently strand the resume window. A wedge that
    starts from a live connection costs an extra stale-link timeout before the
    scan/backoff cycle begins; the processes that follow a re-exec start cold.
    """
    total = pad_client.STALE_TIMEOUT_S if from_live_connection else 0.0
    backoff = 1
    for _ in range(cli.NOT_FOUND_RESPAWN_THRESHOLD):
        # A sweep that comes back empty, the disconnect that follows it (capped,
        # but it can hang to the cap under the same wedge), then the backoff.
        total += (pad_client.SCAN_TIMEOUT_S + pad_client.DISCONNECT_TIMEOUT_S
                  + backoff)
        backoff = min(30, backoff * 2)
    return total


def _worst_case_reexec_chain_s():
    # FAST_RESPAWN_LIMIT re-execs are allowed back to back, and the session's
    # end_ts stays frozen through all of them — no data is arriving.
    return (_detection_cycle_s(True)
            + (cli.FAST_RESPAWN_LIMIT - 1) * _detection_cycle_s(False))


def test_resume_window_outlasts_the_whole_reexec_chain():
    # Sizing the window against a single detection cycle is not enough: the
    # throttle allows a chain of fast re-execs, and the walk is still going.
    chain = _worst_case_reexec_chain_s()
    assert chain > 30, "sanity: this is why the old 30s window never fired"
    assert cli.SESSION_RESUME_WINDOW_S >= chain + 120, (
        f"resume window {cli.SESSION_RESUME_WINDOW_S}s leaves no startup "
        f"allowance over a {chain:.0f}s re-exec chain")


# --- the recency bound -------------------------------------------------------

def test_resume_candidate_offered_across_a_reexec(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    now = 1_700_000_000.0
    _seed_session(store, now - _worst_case_reexec_chain_s(), 500, 860, 560)
    assert cli._resume_candidate(store, now) is not None


def test_no_resume_candidate_for_a_long_finished_session(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    now = 1_700_000_000.0
    _seed_session(store, now - cli.SESSION_RESUME_WINDOW_S - 1, 500, 860, 560)
    assert cli._resume_candidate(store, now) is None


def test_no_resume_candidate_without_history_or_with_a_stepped_clock(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    now = 1_700_000_000.0
    assert cli._resume_candidate(store, now) is None, "empty store"

    _seed_session(store, now + 5, 500, 860, 560)
    assert cli._resume_candidate(store, now) is None, \
        "a future end_ts means the clock stepped, not that the walk is fresh"


# --- the continuity decision -------------------------------------------------

T = 1_700_000_000.0  # the moment the interrupted session was last recorded


def _session(dist_m, steps, duration_s, origin=(0, 0, 0), end_ts=T):
    return {"distance_m": dist_m, "steps": steps, "duration_s": duration_s,
            "origin_distance": origin[0], "origin_steps": origin[1],
            "origin_elapsed": origin[2], "end_ts": end_ts}


def test_continues_when_the_counters_carried_on():
    session = _session(500, 860, 560)  # a run that began at T-560
    # Two minutes on, still walking: the pad's timer still dates the run to T-560.
    assert cli._continues_session(session, mk(T + 124, 3.2, 700, 1200, 684))
    # Paused: counters unmoved, and the run still predates T. Same walk.
    assert cli._continues_session(session, mk(T + 100, 0.0, 500, 860, 560))


def test_does_not_continue_a_restarted_run_that_caught_up():
    # The case a time window cannot see: the walk ended, the pad zeroed, and the
    # user got going again before we reconnected. Resuming here would overwrite
    # the finished 500m walk and swallow it into the new one.
    session = _session(500, 860, 560)
    assert not cli._continues_session(session, mk(T + 295, 3.2, 485, 830, 291))
    # ...even once the new run has out-walked the old one on every counter: its
    # own timer says it started at T, well after we last saw the old walk.
    assert not cli._continues_session(session, mk(T + 300, 6.0, 520, 890, 300))


def test_does_not_continue_a_short_session_out_walked_by_a_new_run():
    # Counters alone can't defend a short session — a fresh 40m/30s run passes
    # every one of them against a finished 20m/25s walk. The run's start time is
    # what gives it away.
    session = _session(20, 30, 25)
    assert not cli._continues_session(session, mk(T + 200, 3.2, 40, 60, 30))


def test_does_not_continue_a_short_walk_followed_by_a_reset():
    # A 20m walk then a zeroed counter slips past the recorder's RESET_TOL_M
    # (0 + 20 is not < 20), so resuming would blank the recorded walk.
    session = _session(20, 30, 25)
    assert not cli._continues_session(session, mk(T + 200, 3.2, 0, 0, 0))


def test_continuity_accounts_for_a_rolled_over_session_origin():
    # A session that rolled over at midnight measures from a carried origin, so
    # continuity has to compare against the pad's raw counter, not the session's.
    session = _session(200, 340, 220, origin=(5000, 8600, 5600))
    assert cli._continues_session(session, mk(T + 60, 3.2, 5300, 9000, 5900))
    assert not cli._continues_session(session, mk(T + 60, 3.2, 300, 500, 320)), \
        "a raw counter below the origin is a fresh run, not a continuation"


# --- the gate ----------------------------------------------------------------

def test_gate_resumes_once_and_then_stands_aside(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    now = 1_700_000_000.0
    _seed_session(store, now - 200, 500, 860, 560)
    recorder = Recorder(store)
    gate = cli._make_resume_gate(store, recorder, now=now)

    assert gate(mk(now, 3.2, 700, 1200, 760)) is True
    # The question is settled after the first packet; later ones must not
    # re-trigger it (a mid-walk reset would have moved the recorder on).
    assert gate(mk(now + 1, 3.2, 720, 1240, 780)) is False


def test_gate_declines_when_the_counters_say_new_run(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    now = 1_700_000_000.0
    _seed_session(store, now - 200, 500, 860, 560)
    recorder = Recorder(store)
    gate = cli._make_resume_gate(store, recorder, now=now)

    assert gate(mk(now, 3.2, 40, 70, 45)) is False
    assert recorder._active_id is None, "must leave a fresh session to open"


def test_gate_declines_a_candidate_that_went_stale_while_waiting(tmp_path):
    # The candidate is chosen at startup, but the pad may not come back for
    # hours. A long enough new run would satisfy every continuity check by then,
    # so the bound has to be re-tested against the packet that actually arrives.
    store = Store(str(tmp_path / "t.db"))
    now = 1_700_000_000.0
    _seed_session(store, now - 200, 500, 860, 560)
    recorder = Recorder(store)
    gate = cli._make_resume_gate(store, recorder, now=now)

    late = now + cli.SESSION_RESUME_WINDOW_S
    assert gate(mk(late, 3.2, 5000, 8000, 1200)) is False
    assert recorder._active_id is None


# --- end to end --------------------------------------------------------------

def test_resuming_mid_walk_does_not_double_count(tmp_path):
    # The scenario from issue #18: walk, wedge, re-exec, reconnect, still walking.
    store = Store(str(tmp_path / "t.db"))
    base = 1716451200.0
    _seed_session(store, base + 60, 500, 860, 560)

    gap = _worst_case_reexec_chain_s()
    recorder = Recorder(store)
    gate = cli._make_resume_gate(store, recorder, now=base + 60 + gap)
    status = mk(base + 60 + gap + 20, 3.2, 700, 1200, 760)
    gate(status)
    recorder.handle(status)
    recorder.close()

    day = store.daily_totals(_day_of(base))
    assert day["sessions"] == 1, "the wedge must not split the walk in two"
    assert day["distance_m"] == 700, "cumulative counters must not be re-added"
    assert day["steps"] == 1200


def test_a_genuinely_new_run_gets_its_own_session(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    base = 1716451200.0
    _seed_session(store, base + 60, 500, 860, 560)

    recorder = Recorder(store)
    gate = cli._make_resume_gate(store, recorder, now=base + 200)
    for status in (mk(base + 200, 3.2, 40, 70, 45),
                   mk(base + 260, 3.2, 300, 520, 105)):
        gate(status)
        recorder.handle(status)
    recorder.close()

    day = store.daily_totals(_day_of(base))
    assert day["sessions"] == 2, "a new run must not extend the finished one"
    assert day["distance_m"] == 800, "500 + 300, neither walk lost"


class _FakePad:
    """Stands in for PadClient: connects instantly, delivers scripted statuses,
    then holds the connection open until the test cancels it."""

    def __init__(self, statuses):
        self.statuses = list(statuses)

    async def connect(self, address=None):
        return "fake:pad"

    async def capture(self, callback, **kwargs):
        for status in self.statuses:
            callback(status)
        await asyncio.sleep(60)

    async def disconnect(self):
        pass


def test_run_serve_wires_the_resume_gate(tmp_path, monkeypatch):
    # Guards the wiring, not just the helpers: reverting run_serve to the old
    # unconditional-resume (or dropping it) must fail here.
    import uvicorn

    db_path = str(tmp_path / "t.db")
    store = Store(db_path)
    now = time.time()
    _seed_session(store, now - 200, 500, 860, 560)

    async def never_serve(self, sockets=None):
        await asyncio.sleep(60)  # no real socket bound during tests

    monkeypatch.setattr(uvicorn.Server, "serve", never_serve)
    monkeypatch.setattr(cli, "PadClient",
                        lambda: _FakePad([mk(now, 3.2, 700, 1200, 760)]))
    # export_loop writes to iCloud Drive on its first tick — not in a test.
    monkeypatch.setattr(health_export, "write_export", lambda *a, **k: None)

    async def drive():
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(cli.run_serve(db_path), timeout=1.0)

    asyncio.run(drive())

    # This test runs against the wall clock, so it can straddle local midnight —
    # where the recorder legitimately splits the walk across two days. Assert on
    # the total, which the double-count bug inflates to 1200 either way.
    days = {_day_of(now - 200), _day_of(now)}
    totals = [store.daily_totals(d) for d in days]
    assert sum(t["distance_m"] for t in totals) == 700, \
        "the resumed walk must not re-add the pad's cumulative counters"
    if len(days) == 1:
        assert totals[0]["sessions"] == 1, \
            "the interrupted walk must be resumed, not re-opened"
