# WalkingPad Tracker — Design Spec

- **Date:** 2026-05-23
- **Status:** Approved (ready for implementation planning)
- **Owner:** Craig

## Goal

Replace the unreliable WalkingPad phone app with a self-hosted Mac setup that captures every walk losslessly, controls the pad, and auto-syncs daily totals into Apple Health — so the official app can be abandoned entirely.

## Problem

The official WalkingPad/KS Fit iPhone app loses data: stepping off the pad stops the belt and erases the in-progress session, and it does not reliably write to Apple Health. The remote handles start/stop but nothing else. The result is no trustworthy daily step/distance history.

## Success Criteria

1. **Lossless capture** — walks are recorded continuously and survive hop-offs, auto-pauses, and counter resets. The daily total is the sum of all sessions that day and is never erased.
2. **Control from the Mac** — start, stop, and set speed from the macOS menubar and the web dashboard, without opening the official app.
3. **Two views** — glanceable today's stats (steps / distance / time) in the menubar; deeper daily/weekly/monthly trends and streaks in a local web dashboard.
4. **Hands-off Health sync** — every morning, yesterday's walking is logged into Apple Health automatically as a Walking workout (distance + duration), zero taps.
5. **Runs unattended** — lives on the main Mac via launchd, auto-reconnects when the pad wakes, and fully replaces the phone app.

## Key Facts & Constraints

- **Device:** KingSmith WALKINGPAD P1 (Amazon B0D2ZVB8DJ), in the WalkingPad protocol family.
- **BLE fingerprint (confirmed by scan):**
  - Service `0000fe00-0000-1000-8000-00805f9b34fb`
  - `0000fe01` `[notify, read]` — live status stream (speed, distance, steps, time, state)
  - `0000fe02` `[write-without-response]` — command channel (start/stop/speed/mode)
  - Service `00010203-0405-0607-0809-0a0b0c0d1912` — firmware OTA, ignored.
- This matches the [`ph4-walkingpad`](https://github.com/ph4r05/ph4-walkingpad) protocol exactly — frame parsing and command encoding are already solved; reuse the library.
- **One connection at a time.** Whatever process holds the BLE connection owns the pad; the official app cannot connect simultaneously. This is intended (we are replacing the app). It also means **control commands must route through the same daemon that holds the connection** — there is no second connection for a separate control app.
- **Apple Health (HealthKit) is iOS-only.** A Mac process cannot write to Health directly. The bridge is a file in iCloud Drive read by an iPhone Shortcut.
- **Capture host:** the user's main Mac (not an always-on device). Capture happens whenever the Mac is awake and in Bluetooth range; occasional gaps when the laptop is away/closed are acceptable.

## Architecture

A single background **daemon** holds the BLE connection, records stats, and exposes a local HTTP API for both reading status and issuing control commands. A separate menubar process and the browser dashboard are both thin clients of that API. A daily export drops a summary into iCloud Drive for the iPhone Shortcut.

| Unit             | Responsibility                                                                    | Depends on                        |
| ---------------- | --------------------------------------------------------------------------------- | --------------------------------- |
| `pad_client`     | BLE connect, parse status frames, encode commands, auto-reconnect                 | `ph4-walkingpad` / `bleak`        |
| `recorder`       | Detect session boundaries, accumulate totals, persist sessions + live state       | `pad_client`, `store`             |
| `store`          | SQLite schema + read/query helpers (sessions, daily rollups)                      | —                                 |
| `api`            | Local HTTP API: status, history, and control endpoints (in-process with recorder) | `recorder`, `pad_client`, `store` |
| `web`            | Browser dashboard: live tile, controls, daily/weekly/monthly trends, streaks      | `api`                             |
| `menubar`        | macOS menubar widget: today's stats + start/stop/speed controls                   | `api`                             |
| `health_export`  | Maintain a daily-totals summary file in iCloud Drive                              | `store`                           |
| `launchd` plists | Keep daemon + menubar alive, start at login, restart on crash                     | —                                 |

**Process layout:** two processes, both managed by launchd.

- **Daemon process** = `pad_client` + `recorder` + `api` (serving the web dashboard) + `health_export`. Single asyncio process; the API and BLE share one event loop so control commands use the live connection.
- **Menubar process** = `menubar` (rumps requires the macOS main runloop, so it runs separately and talks to the daemon's API over `localhost`).

## Data Model (SQLite)

- **`sessions`** — one row per finalized walk segment.
  - `id`, `start_ts`, `end_ts`, `duration_s`, `distance_m`, `steps`, `avg_speed_kmh`, `max_speed_kmh`, `created_at`.
- **`samples`** — periodic snapshots during a session (for live view, debugging, and recomputation). Lightweight; prunable.
  - `id`, `session_id`, `ts`, `speed_kmh`, `distance_m`, `steps`, `belt_state`.
- **Daily rollup** — a SQL view/query summing `sessions` by **local** date: total distance, steps, duration, session count.
- **Live state** — current speed/distance/steps/time/connection/belt-state held in the daemon and served via the API (also mirrored to a `current.json` for resilience if the API is briefly unavailable).

## Session Detection & Accumulation (the crux)

The pad reports cumulative counters **for the current run only**, and resets them when it stops/sleeps. The recorder turns that into durable history:

1. Subscribe to `FE01` notifications (fall back to polling `ask_stats` if needed).
2. **Session start:** counters begin increasing from ~0 / belt enters a running state.
3. **During a session:** continuously track the running maximum of distance/steps/time and write periodic `samples`.
4. **Session end:** belt stops, connection drops, or counters reset toward 0. Finalize the session row from the last-seen values (it is already persisted incrementally, so nothing is lost).
5. **New session after a reset** starts a fresh row.
6. **Daily total = sum of all sessions for the local date.** Hop-offs simply create multiple sessions that roll up together.
7. **Midnight rollover:** sessions are attributed to the local date of their start; rollups group by local date.

> **Open question to confirm during build:** whether stepping off makes the belt _auto-pause_ (counters freeze, same session resumes) or _fully reset_ (counters zero, new session). The continuous-recording design handles both; the real byte stream from the first walk will determine exact thresholds for session-boundary detection, which will be tuned then.

## Control

Commands are issued through the daemon (sole BLE owner) via `ph4-walkingpad`'s command encoding on `FE02`:

- **Start** the belt.
- **Stop** the belt.
- **Set speed** (0.5–6.0 km/h, fine-grained).
- (Mode switch standby/manual/auto available; not surfaced in v1 UI unless needed.)

Both the menubar and the web dashboard call the same control endpoints.

## Local HTTP API (daemon)

- `GET /status` — live state: connected, belt_state, speed, distance, steps, elapsed.
- `GET /today` — today's rollup.
- `GET /history?range=week|month` — rollup series for charts.
- `GET /sessions?date=YYYY-MM-DD` — session list.
- `POST /control/start` — start the belt.
- `POST /control/stop` — stop the belt.
- `POST /control/speed` `{ "kmh": <float> }` — set speed.

Bound to `127.0.0.1` only (no network exposure).

## Web Dashboard

- **Live tile:** current speed, distance, steps, elapsed; updates every ~1s from `/status`.
- **Controls:** start / stop buttons + speed slider/presets.
- **Trends:** daily / weekly / monthly distance and steps charts (Chart.js).
- **Streak:** current consecutive-day walking streak.
- **Sessions:** per-day session breakdown.

## Menubar Widget

- Title shows today's steps (or distance) at a glance, refreshed periodically.
- Dropdown: today's steps / distance / time, connection + belt state, **Start / Stop / Speed** controls, and an "Open Dashboard" link.

## Apple Health Sync

- `health_export` maintains `~/Library/Mobile Documents/com~apple~CloudDocs/WalkingPad/daily.json` — an array of `{ date, distance_km, steps, duration_min }` per local day.
- **iPhone Shortcut automation** (time-of-day, each morning): reads `daily.json` from iCloud Drive, selects **yesterday's** entry, and logs a **Walking workout** (distance + duration) into Health.
- **No double-logging:** the automation logs only the previous, completed day, once per morning. The export file may also carry a marker so a re-run is a no-op.
- v1 logs distance + duration as a Walking workout. Adding a step-count sample and an energy estimate are optional enhancements.

## Reliability & Error Handling

- **Auto-reconnect** with exponential backoff (cap ~30s) when the pad sleeps, goes out of range, or the link drops.
- **One-connection rule:** if the official app ever grabs the pad, connection fails and the daemon retries; documented guidance is to not open the app.
- **Crash-safe:** launchd `KeepAlive` restarts the daemon and menubar; SQLite is durable; at worst an in-flight partial session is lost, never saved history.
- **Local timestamps** for all sessions; rollups group by local date.

## Tech Stack

- **Python**, run via `uv` (matches `scan_pad.py`; avoids Homebrew-Python pip restrictions).
- **`ph4-walkingpad`** for BLE frame parsing + command encoding (wraps `bleak`).
- **SQLite** for storage.
- **FastAPI** (+ uvicorn) for the local API and serving the dashboard; single HTML page using **Chart.js**.
- **`rumps`** for the macOS menubar app.
- **launchd** LaunchAgents for process supervision.
- _Acceptable swaps:_ Flask for FastAPI; SwiftBar for rumps.

## Scope

**In v1:**

- Lossless capture with session accumulation across resets.
- Control (start / stop / set speed) from menubar and web.
- Web dashboard (live + trends + streak) and menubar widget.
- Daily Apple Health sync via iCloud Drive + Shortcut.
- launchd supervision + auto-reconnect.

**Deferred (not v1):**

- Mode switching UI (standby/manual/auto).
- Step-count and energy samples in Health (workout distance+duration only for v1).
- Goals/targets, notifications, multi-pad support.
- Running the capture on an always-on device (Mac mini / Raspberry Pi).

## Testing Strategy

- **`pad_client`:** unit-test frame parsing against sample/captured byte frames.
- **`recorder`:** unit-test session detection + accumulation with synthetic status streams (single session, multi-session day, hop-off reset, midnight rollover).
- **`store`:** test schema + rollup queries.
- **Replay mode:** feed recorded BLE frames through the pipeline to validate end-to-end without the physical pad.
- **Manual e2e:** a real walk → verify dashboard, menubar, and the next-morning Health entry.
