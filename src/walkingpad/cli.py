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
# consider replacing our process image to get fresh CoreBluetooth state. The
# macOS CoreBluetooth daemon caches discovery state per-process; once a
# Python-side BleakScanner falls into the "always empty" pathology it does not
# recover on its own. With the 1→2→4→8→16→30 backoff this triggers after ~60s of
# empty scans. A genuinely powered-off pad produces the same empty scans, so the
# actual re-exec is gated by the throttle below — hitting this threshold is
# necessary but not sufficient.
NOT_FOUND_RESPAWN_THRESHOLD = 6

# Clearing a stuck CoreBluetooth cache needs a fresh process image, which we get
# by re-execing ourselves in place. We used to exit(75) and let launchd's
# KeepAlive respawn us, but that is no longer safe on this machine: during the
# crash-loop era (900+ runs) launchd permanently classified the job as
# inefficient — `launchctl print gui/$(id -u)/com.walkingpad.daemon` reports
# `pended nondemand spawn = inefficient` — and now declines to respawn it at
# all. On 2026-07-21 a single, correctly-throttled exit left the daemon dead for
# 4.5 hours (menubar and dashboard dead with it) until a manual `launchctl
# kickstart`. The verdict is sticky for the life of the job, so any strategy
# that hands control back to launchd is a coin flip we lose.
#
# We still throttle, for a different reason than before: a powered-off pad
# produces the same endless empty scans as a wedged cache, and re-execing every
# ~60s forever would spam the log and drop the HTTP API repeatedly for no gain.
# A persisted ledger allows a few quick re-execs to clear a genuine wedge, then
# falls back to at most one per SLOW interval while the pad stays absent.
# Receiving real status data clears the ledger, restoring fast recovery for the
# next real wedge.
FAST_RESPAWN_LIMIT = 3           # quick re-execs allowed to clear a stuck cache
SLOW_RESPAWN_INTERVAL_S = 1800   # afterwards, re-exec at most this often (30m)
RESPAWN_STALE_S = 24 * 3600      # drop ledger entries older than a day (hygiene)


def _reexec_self(streak):
    """Replace this process image with a fresh one, keeping the same PID.

    execv is what makes this safe where exit-for-respawn was not: launchd sees a
    process that never exited, so its respawn machinery — and the sticky
    "inefficient" verdict described above — is never consulted. The image itself
    is replaced wholesale, which is what actually clears the wedged Bluetooth
    state: the old bluetoothd XPC connection dies with the old image and the new
    one connects fresh. Python sockets are non-inheritable (PEP 446), so
    uvicorn's listener on 8787 is closed by the exec and the new image rebinds
    it.

    Re-exec via `-m walkingpad.cli` rather than sys.argv[0] so we normalize both
    launch styles (`python -m walkingpad.cli ...` from the plist and the
    `walkingpad` console script) to the same command line.

    Does not return on success. Returns on failure, in which case we are still
    the old, possibly-wedged process and the caller should keep scanning: a live
    daemon serving stale scans beats no daemon at all, which is exactly the hole
    exit(75) used to fall into.
    """
    if not sys.executable:
        print(f"scanner returned empty {streak}x; no interpreter path to "
              f"re-exec, scanning on instead", flush=True)
        return
    print(f"scanner returned empty {streak}x; re-execing in place for fresh "
          f"CoreBluetooth state (pid {os.getpid()} retained)", flush=True)
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execv(sys.executable, [sys.executable, "-m", "walkingpad.cli",
                                  *sys.argv[1:]])
    except OSError as e:
        print(f"re-exec failed ({e}); scanning on in place", flush=True)


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
    NOT re-exec on a False return: an unrecorded re-exec defeats the throttle
    entirely, leaving a powered-off pad to churn the process every ~60s."""
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
    """Decide whether to re-exec given prior re-execs since the pad last
    delivered data. Fast for the first few (clear a stuck cache), then at most
    one per SLOW_RESPAWN_INTERVAL_S so a powered-off pad can't churn us."""
    if len(respawns) < FAST_RESPAWN_LIMIT:
        return True
    return now - max(respawns) >= SLOW_RESPAWN_INTERVAL_S


def _maybe_reexec(db_path, streak, now=None):
    """Re-exec for fresh CoreBluetooth state if the throttle allows it (does not
    return in that case); otherwise log and return so the caller keeps scanning."""
    now = time.time() if now is None else now
    path = _respawn_ledger_path(db_path)
    respawns = _load_respawns(path, now)
    if not _respawn_allowed(respawns, now):
        print(f"scanner returned empty {streak}x; already re-execed "
              f"{len(respawns)}x with no connection — pad appears powered off, "
              f"scanning on instead", flush=True)
        return
    respawns.append(now)
    if not _save_respawns(path, respawns):
        # Can't record this re-exec, so we can't throttle the next one. Skip it
        # rather than risk untracked churn; keep scanning.
        print(f"scanner returned empty {streak}x; could not persist respawn "
              f"ledger at {path} — scanning on instead", flush=True)
        return
    _reexec_self(streak)
    # Only reachable if the exec failed — a successful one never returns. Give
    # the budget back: a re-exec that didn't happen must not count toward the
    # throttle, or three failed execs would push a genuinely wedged scanner into
    # the 30-minute slow path with nothing to show for it.
    respawns.pop()
    _save_respawns(path, respawns)


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
                # Re-execs only if the throttle allows it; otherwise we keep
                # scanning (pad is powered off, not a stuck cache).
                _maybe_reexec(db_path, not_found_streak)
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
                    # Re-execs only if the throttle allows it; otherwise keep
                    # scanning (pad off, not a stuck cache).
                    _maybe_reexec(db_path, not_found_streak)
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
