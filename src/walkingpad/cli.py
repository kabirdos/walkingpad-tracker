import argparse
import asyncio
import datetime as dt
import os
import sys
import time

from walkingpad.store import Store
from walkingpad.recorder import Recorder
from walkingpad.pad_client import PadClient, PadNotFoundError

MI = 0.621371  # km -> miles, km/h -> mph

# If the BLE scanner returns no matching device this many times in a row, exit
# and let launchd respawn us. The macOS CoreBluetooth daemon caches discovery
# state per-process; once a Python-side BleakScanner falls into the "always
# empty" pathology it does not recover on its own. With the 1→2→4→8→16→30
# backoff this triggers after ~60s of empty scans — long enough that a genuinely
# powered-off pad doesn't cause a respawn loop (launchd's own throttle would
# slow that down anyway), short enough that a stale-cache day isn't lost.
NOT_FOUND_RESPAWN_THRESHOLD = 6


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

    def on_status(status):
        _print_status(status)
        recorder.handle(status)

    client = PadClient()
    backoff = 1
    not_found_streak = 0
    while True:
        try:
            addr = await client.connect(address)
            print(f"connected to {addr}", flush=True)
            backoff = 1
            not_found_streak = 0
            await client.capture(on_status)
        except PadNotFoundError as e:
            not_found_streak += 1
            await client.disconnect()
            if not_found_streak >= NOT_FOUND_RESPAWN_THRESHOLD:
                _force_respawn(not_found_streak)
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


async def run_serve(db_path, host="0.0.0.0", port=8787, address=None):
    """Run the capture loop and the HTTP API together in one asyncio process."""
    import uvicorn

    from walkingpad.state import DaemonState
    from walkingpad.app import create_app
    from walkingpad.health_export import write_export

    store = Store(db_path)
    recorder = Recorder(store)
    client = PadClient()
    state = DaemonState(store, pad_client=client)

    # If the daemon restarted mid-walk, resume the still-open session so the
    # ongoing run isn't recorded twice.
    latest = store.get_latest_session()
    if latest and latest["end_ts"] and (time.time() - latest["end_ts"] < 30):
        recorder.resume(latest)

    def on_status(status):
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
                    _force_respawn(not_found_streak)
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

    print(f"serving API on http://{host}:{port}", flush=True)
    await asyncio.gather(server.serve(), capture_loop(), export_loop())


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
        asyncio.run(run_serve(args.db, args.host, args.port, args.address))
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
