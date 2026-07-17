import argparse
import asyncio
import datetime as dt
import json
import os
import sys
import time

from walkingpad.store import Store
from walkingpad.recorder import Recorder
from walkingpad.pad_client import PadClient, PadNotFoundError

MI = 0.621371  # km -> miles, km/h -> mph

# If the BLE scanner returns no matching device this many times in a row,
# consider exiting to let launchd respawn us with fresh CoreBluetooth state. The
# macOS CoreBluetooth daemon caches discovery state per-process; once a
# Python-side BleakScanner falls into the "always empty" pathology it does not
# recover on its own. With the 1→2→4→8→16→30 backoff this triggers after ~60s of
# empty scans. A genuinely powered-off pad produces the same empty scans, so the
# actual exit is gated by the respawn throttle below — hitting this threshold is
# necessary but not sufficient to respawn.
NOT_FOUND_RESPAWN_THRESHOLD = 6

# Exit-for-respawn is a workaround for a *stuck* CoreBluetooth cache, which a
# fresh process clears in a respawn or two. But a pad that's simply powered off
# produces the exact same endless empty scans, and exiting unconditionally every
# ~60s crash-loops the daemon: launchd's KeepAlive respawns it hundreds of times
# until it classifies the job as "inefficient" (`pended nondemand spawn`) and
# stops respawning it for good — leaving the menubar and dashboard dead
# (observed 900+ runs). So we throttle: a persisted ledger of recent respawns
# lets us allow a few quick respawns to clear a genuine cache wedge, then fall
# back to at most one respawn per SLOW interval while the pad stays absent. A
# 30-minute run reads to launchd as a well-behaved long-running job, never a
# cycling one, so the job is never pended. Receiving real status data from the
# pad clears the ledger, restoring fast recovery for the next real wedge.
FAST_RESPAWN_LIMIT = 3           # quick respawns allowed to clear a stuck cache
SLOW_RESPAWN_INTERVAL_S = 1800   # afterwards, respawn at most this often (30m)
RESPAWN_STALE_S = 24 * 3600      # drop ledger entries older than a day (hygiene)


def _force_respawn(streak):
    # os._exit, not sys.exit. SystemExit propagates through asyncio.gather in
    # theory, but in practice uvicorn's lifespan handler caught it and the
    # process stayed alive for days holding port 8787 but answering nothing —
    # the worst possible state because the watchdog appeared to fire (the
    # message even printed) and launchd had no signal to respawn. os._exit
    # bypasses every Python-level handler and immediately terminates with the
    # given code; we're explicitly trying to be unkillable-by-finally.
    print(f"scanner returned empty {streak}x; exiting for launchd respawn "
          f"(fresh CoreBluetooth state)", flush=True)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(75)  # EX_TEMPFAIL — KeepAlive=true will respawn us


def _respawn_ledger_path(db_path):
    return os.path.join(os.path.dirname(db_path), "respawn_history.json")


def _load_respawns(path, now):
    """Respawn timestamps from the ledger, dropping anything older than
    RESPAWN_STALE_S. A missing or corrupt ledger reads as empty — the watchdog
    must never be blocked by its own bookkeeping."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [t for t in data
            if isinstance(t, (int, float)) and 0 <= now - t < RESPAWN_STALE_S]


def _save_respawns(path, respawns):
    """Persist the ledger atomically. Returns True on success. The caller must
    NOT respawn on a False return: an unrecorded exit defeats the throttle
    entirely (unlimited ~60s exits → launchd pends the job), which is the exact
    failure this file exists to prevent."""
    try:
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump(respawns, f)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def _clear_respawns(path):
    """Drop the ledger once real status data arrives: the pad is genuinely
    reachable, so the next empty-scan wedge should get fast recovery again.
    Returns True once the ledger is absent (removed, or already gone) so the
    caller can retry on a transient unlink failure instead of giving up."""
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return True  # already absent — nothing to clear
    except OSError:
        return False  # couldn't remove; caller should retry next status


def _respawn_allowed(respawns, now):
    """Decide whether to exit for a respawn given prior respawns since the pad
    last delivered data. Fast for the first few (clear a stuck cache), then at
    most one per SLOW_RESPAWN_INTERVAL_S so a powered-off pad can't crash-loop."""
    if len(respawns) < FAST_RESPAWN_LIMIT:
        return True
    return now - max(respawns) >= SLOW_RESPAWN_INTERVAL_S


def _maybe_force_respawn(db_path, streak, now=None):
    """Exit for a launchd respawn if the throttle allows it (never returns in
    that case); otherwise log and return so the caller keeps scanning."""
    now = time.time() if now is None else now
    path = _respawn_ledger_path(db_path)
    respawns = _load_respawns(path, now)
    if not _respawn_allowed(respawns, now):
        print(f"scanner returned empty {streak}x; already respawned "
              f"{len(respawns)}x with no connection — pad appears powered off, "
              f"scanning on instead of respawning", flush=True)
        return
    respawns.append(now)
    if not _save_respawns(path, respawns):
        # Can't record this respawn, so we can't throttle the next one. Refuse
        # to exit rather than risk an untracked crash-loop; keep scanning.
        print(f"scanner returned empty {streak}x; could not persist respawn "
              f"ledger at {path} — scanning on instead of respawning", flush=True)
        return
    _force_respawn(streak)


def default_db_path():
    base = os.path.expanduser("~/.local/share/walkingpad")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "walkingpad.db")


def _print_status(status):
    print(f"  speed={status.speed_kmh * MI:>4.1f} mph  "
          f"dist={status.distance_m / 1000 * MI:>5.2f} mi  "
          f"steps={status.steps:>5}  time={status.elapsed_s // 60}m",
          flush=True)


async def run_capture(db_path, address=None):
    store = Store(db_path)
    recorder = Recorder(store)
    ledger_path = _respawn_ledger_path(db_path)

    # Clear the respawn throttle only once we've received real status data, not
    # on a bare connect(): the pad can accept a BLE connection and then deliver
    # nothing ("no pad data ... connection stale"), and clearing on connect
    # alone would reset the throttle every process and re-enable the crash-loop.
    got_data = {"seen": False}

    def on_status(status):
        # Retry the clear each packet until the ledger is confirmed gone, so a
        # transient unlink failure doesn't leave stale throttle history behind.
        if not got_data["seen"] and _clear_respawns(ledger_path):
            got_data["seen"] = True
        _print_status(status)
        recorder.handle(status)

    client = PadClient()
    backoff = 1
    not_found_streak = 0
    while True:
        try:
            addr = await client.connect(address)
            got_data["seen"] = False  # this connection must re-earn the clear
            print(f"connected to {addr}", flush=True)
            backoff = 1
            not_found_streak = 0
            await client.capture(on_status)
        except PadNotFoundError as e:
            not_found_streak += 1
            await client.disconnect()
            if not_found_streak >= NOT_FOUND_RESPAWN_THRESHOLD:
                # Exits for a respawn only if the throttle allows it; otherwise
                # we keep scanning (pad is powered off, not a stuck cache).
                _maybe_force_respawn(db_path, not_found_streak)
                not_found_streak = 0
            print(f"connection lost ({e}); retrying in {backoff}s", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(30, backoff * 2)
        except Exception as e:
            # Don't finalize the session on a transient drop — the recorder
            # closes it only on a real counter reset, so a reconnect mid-walk
            # continues the same session instead of double-counting.
            not_found_streak = 0
            await client.disconnect()
            print(f"connection lost ({e}); retrying in {backoff}s", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(30, backoff * 2)


async def run_serve(db_path, host="0.0.0.0", port=8787, address=None,
                    vault_dir=None):
    """Run the capture loop and the HTTP API together in one asyncio process."""
    import uvicorn

    from walkingpad.state import DaemonState
    from walkingpad.app import create_app
    from walkingpad.health_export import write_export
    from walkingpad.obsidian_export import write_export as write_vault_export

    store = Store(db_path)
    recorder = Recorder(store)
    client = PadClient()
    state = DaemonState(store, pad_client=client)

    # If the daemon restarted mid-walk, resume the still-open session so the
    # ongoing run isn't recorded twice.
    latest = store.get_latest_session()
    if latest and latest["end_ts"] and (time.time() - latest["end_ts"] < 30):
        recorder.resume(latest)

    ledger_path = _respawn_ledger_path(db_path)

    # Clear the respawn throttle only once we've received real status data, not
    # on a bare connect(): the pad can accept a BLE connection and then deliver
    # nothing ("no pad data ... connection stale"), and clearing on connect
    # alone would reset the throttle every process and re-enable the crash-loop.
    got_data = {"seen": False}

    def on_status(status):
        # Retry the clear each packet until the ledger is confirmed gone, so a
        # transient unlink failure doesn't leave stale throttle history behind.
        if not got_data["seen"] and _clear_respawns(ledger_path):
            got_data["seen"] = True
        state.record_status(status)
        recorder.handle(status)

    app = create_app(state)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)

    async def capture_loop():
        backoff = 1
        not_found_streak = 0
        while True:
            try:
                addr = await client.connect(address)
                got_data["seen"] = False  # this connection must re-earn the clear
                state.connected = True
                print(f"connected to {addr}", flush=True)
                backoff = 1
                not_found_streak = 0
                await client.capture(on_status)
            except PadNotFoundError as e:
                state.connected = False
                not_found_streak += 1
                await client.disconnect()
                if not_found_streak >= NOT_FOUND_RESPAWN_THRESHOLD:
                    # Exits for a respawn only if the throttle allows it;
                    # otherwise keep scanning (pad off, not a stuck cache).
                    _maybe_force_respawn(db_path, not_found_streak)
                    not_found_streak = 0
                print(f"connection lost ({e}); retrying in {backoff}s", flush=True)
                await asyncio.sleep(backoff)
                backoff = min(30, backoff * 2)
            except Exception as e:
                state.connected = False
                not_found_streak = 0
                # Keep the session open across transient reconnects (see note in
                # run_capture); the recorder splits sessions only on a real reset.
                await client.disconnect()
                print(f"connection lost ({e}); retrying in {backoff}s", flush=True)
                await asyncio.sleep(backoff)
                backoff = min(30, backoff * 2)

    async def export_loop():
        # Keep the iCloud Drive summary fresh so the morning Shortcut reads
        # current data. Atomic write; failures are non-fatal.
        while True:
            try:
                write_export(store)
            except Exception:
                pass
            await asyncio.sleep(120)

    async def vault_export_loop():
        # Write a near-live JSON file into the Obsidian vault every 30s for
        # the personal-dashboard plugin's WalkingPad widget. Atomic write;
        # failures are non-fatal (the widget will just show stale data). 30s
        # is fast enough that a walk feels live without thrashing vault sync.
        while True:
            try:
                write_vault_export(state, store, vault_dir)
            except Exception:
                pass
            await asyncio.sleep(30)

    print(f"serving API on http://{host}:{port}", flush=True)
    loops = [server.serve(), capture_loop(), export_loop()]
    if vault_dir:
        print(f"writing Obsidian vault summary to {vault_dir}", flush=True)
        loops.append(vault_export_loop())
    await asyncio.gather(*loops)


def cmd_today(db_path):
    store = Store(db_path)
    today = dt.date.today().strftime("%Y-%m-%d")
    t = store.daily_totals(today)
    print(f"{today}: {t['distance_m'] / 1000 * MI:.2f} mi, {t['steps']} steps, "
          f"{t['duration_s'] // 60} min, {t['sessions']} session(s)")


def cmd_sessions(db_path, date_str):
    store = Store(db_path)
    rows = store.conn.execute(
        """SELECT id, start_ts, distance_m, steps, duration_s, max_speed_kmh
           FROM sessions
           WHERE date(start_ts, 'unixepoch', 'localtime') = ?
           ORDER BY id""",
        (date_str,),
    ).fetchall()
    if not rows:
        print(f"No sessions on {date_str}.")
        return
    for r in rows:
        start = dt.datetime.fromtimestamp(r["start_ts"]).strftime("%H:%M")
        print(f"  #{r['id']} {start}  {r['distance_m'] / 1000 * MI:.2f} mi  "
              f"{r['steps']} steps  {r['duration_s'] // 60} min  "
              f"max {r['max_speed_kmh'] * MI:.1f} mph")


def main():
    parser = argparse.ArgumentParser(prog="walkingpad")
    parser.add_argument("--db", default=default_db_path(), help="SQLite path")
    sub = parser.add_subparsers(dest="command", required=True)

    p_cap = sub.add_parser("capture", help="connect and log walks")
    p_cap.add_argument("--address", default=os.environ.get("WALKINGPAD_ADDRESS"),
                       help="BLE address/UUID (default: auto-discover by name)")

    p_srv = sub.add_parser("serve", help="run capture + local HTTP API together")
    p_srv.add_argument("--host", default="0.0.0.0",
                       help="bind address (default 0.0.0.0 so the iPhone can reach it on your LAN)")
    p_srv.add_argument("--port", type=int, default=8787)
    p_srv.add_argument("--address", default=os.environ.get("WALKINGPAD_ADDRESS"),
                       help="BLE address/UUID (default: auto-discover by name)")
    p_srv.add_argument("--vault", default=os.environ.get("WALKINGPAD_VAULT"),
                       help="Obsidian vault path; if set, write a near-live "
                            "summary to <vault>/.dashboard/walkingpad.json "
                            "for the personal-dashboard plugin")

    sub.add_parser("today", help="print today's totals")

    p_ses = sub.add_parser("sessions", help="list sessions for a date")
    p_ses.add_argument("--date", default=dt.date.today().strftime("%Y-%m-%d"))

    p_exp = sub.add_parser("export", help="write the daily summary for the iPhone Shortcut")
    p_exp.add_argument("--path", default=None,
                       help="output path (default: iCloud Drive/WalkingPad/daily.json)")

    args = parser.parse_args()
    if args.command == "capture":
        asyncio.run(run_capture(args.db, args.address))
    elif args.command == "serve":
        asyncio.run(run_serve(args.db, args.host, args.port, args.address,
                              args.vault))
    elif args.command == "today":
        cmd_today(args.db)
    elif args.command == "sessions":
        cmd_sessions(args.db, args.date)
    elif args.command == "export":
        from walkingpad.health_export import write_export
        store = Store(args.db)
        path = write_export(store, args.path)
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
