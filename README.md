# WalkingPad Tracker

Self-hosted capture, control, and Apple Health sync for a KingSmith **WALKINGPAD P1**,
replacing the unreliable phone app. Walks are recorded **losslessly** off the pad
over Bluetooth — hop-offs and the pad's counter resets never erase a session, and
the daily total is the sum of all sessions.

> **Compatibility:** macOS (uses CoreBluetooth, launchd, and a `rumps` menu-bar
> app; the Health sync uses an iPhone Shortcut). Works with KingSmith **WalkingPad**
> belts that speak the `FE00` BLE protocol (P1/A1/R-series and similar) via
> [`ph4-walkingpad`](https://github.com/ph4r05/ph4-walkingpad). Run `python scan_pad.py`
> to check what your unit exposes.

## What it does

- **Capture** — a background daemon holds the BLE connection and logs every walk
  to SQLite, resilient to resets/disconnects (auto-reconnect).
- **Control** — start / stop / set speed from the menubar or web dashboard.
- **Dashboard** — `http://127.0.0.1:8787`: live cockpit, daily/weekly/monthly
  trends, streak, sessions.
- **Menubar** — glanceable today's distance/steps + controls.
- **Health sync** — writes a daily summary to iCloud Drive that an iPhone Shortcut
  logs into Apple Health each morning (zero taps).

## Architecture

```
pad ──BLE (FE00/FE01/FE02)──> pad_client ──> recorder ──> SQLite
                                   │                         │
                              DaemonState <───────── store queries
                                   │
                    FastAPI (127.0.0.1:8787): /status /today /history /sessions /control/*
                       │            │                    │
                   dashboard     menubar           health_export ──> iCloud daily.json ──> iPhone Shortcut ──> Health
```

| Module                           | Responsibility                                                   |
| -------------------------------- | ---------------------------------------------------------------- |
| `pad_client.py`                  | BLE adapter over `ph4-walkingpad` (only file touching Bluetooth) |
| `status.py`                      | wire-unit → SI conversion (`PadStatus`)                          |
| `recorder.py`                    | session detection + accumulation (splits on counter reset)       |
| `store.py`                       | SQLite schema + queries                                          |
| `app.py` / `state.py`            | FastAPI API + shared daemon state                                |
| `web/dashboard.html`             | single-page instrument-panel dashboard                           |
| `menubar.py` / `menubar_core.py` | macOS menubar widget (`rumps`, optional extra)                   |
| `health_export.py`               | iCloud `daily.json` for the Health Shortcut                      |
| `cli.py`                         | `capture` / `serve` / `today` / `sessions` / `export`            |

## Quickstart (development)

```bash
uv run pytest                              # 33 tests
uv run python -m walkingpad.cli capture    # connect + log (pad on, official app closed)
uv run python -m walkingpad.cli today      # today's totals
uv run python -m walkingpad.cli serve      # capture + API + dashboard at :8787
uv run --extra menubar python -m walkingpad.menubar   # menubar widget
```

**Bluetooth:** the pad allows one connection at a time — keep the official
WalkingPad/KS Fit app closed. Grant your terminal Bluetooth permission
(System Settings → Privacy & Security → Bluetooth) on first run.

## Deploy (run unattended)

```bash
./deploy/install.sh      # generates + loads the launchd agents from your paths
```

Then set up the one-time iPhone Shortcut for Apple Health. Full details and
troubleshooting (incl. the macOS Bluetooth-permission gotcha) are in
[`deploy/README.md`](deploy/README.md).

## Stack

Python 3.11+ (run via [`uv`](https://docs.astral.sh/uv/)), `ph4-walkingpad` +
`bleak`, `sqlite3`, FastAPI + uvicorn, Chart.js, `rumps`. Design spec and plans
live in `docs/superpowers/`.

## License

MIT — see [`LICENSE`](LICENSE).
