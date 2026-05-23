import argparse
import asyncio
import datetime as dt
import os

from walkingpad.store import Store
from walkingpad.recorder import Recorder
from walkingpad.pad_client import PadClient


def default_db_path():
    base = os.path.expanduser("~/.local/share/walkingpad")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "walkingpad.db")


def _print_status(status):
    print(f"  speed={status.speed_kmh:>4.1f} km/h  "
          f"dist={status.distance_m / 1000:>5.2f} km  "
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
    while True:
        try:
            addr = await client.connect(address)
            print(f"connected to {addr}", flush=True)
            backoff = 1
            await client.capture(on_status)
        except Exception as e:
            recorder.close()
            await client.disconnect()
            print(f"connection lost ({e}); retrying in {backoff}s", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(30, backoff * 2)


def cmd_today(db_path):
    store = Store(db_path)
    today = dt.date.today().strftime("%Y-%m-%d")
    t = store.daily_totals(today)
    print(f"{today}: {t['distance_m'] / 1000:.2f} km, {t['steps']} steps, "
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
        print(f"  #{r['id']} {start}  {r['distance_m'] / 1000:.2f} km  "
              f"{r['steps']} steps  {r['duration_s'] // 60} min  "
              f"max {r['max_speed_kmh']:.1f} km/h")


def main():
    parser = argparse.ArgumentParser(prog="walkingpad")
    parser.add_argument("--db", default=default_db_path(), help="SQLite path")
    sub = parser.add_subparsers(dest="command", required=True)

    p_cap = sub.add_parser("capture", help="connect and log walks")
    p_cap.add_argument("--address", default=os.environ.get("WALKINGPAD_ADDRESS"),
                       help="BLE address/UUID (default: auto-discover by name)")

    sub.add_parser("today", help="print today's totals")

    p_ses = sub.add_parser("sessions", help="list sessions for a date")
    p_ses.add_argument("--date", default=dt.date.today().strftime("%Y-%m-%d"))

    args = parser.parse_args()
    if args.command == "capture":
        asyncio.run(run_capture(args.db, args.address))
    elif args.command == "today":
        cmd_today(args.db)
    elif args.command == "sessions":
        cmd_sessions(args.db, args.date)


if __name__ == "__main__":
    main()
