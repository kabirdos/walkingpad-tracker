# Deploying WalkingPad Tracker

Two pieces run unattended on your Mac via `launchd`, plus a one-time iPhone
Shortcut that logs walks into Apple Health.

- **`com.walkingpad.daemon`** — holds the Bluetooth connection, records walks to
  SQLite, serves the API + dashboard on `http://127.0.0.1:8787`, and refreshes
  the iCloud `daily.json` summary.
- **`com.walkingpad.menubar`** — the menu-bar widget (reads the daemon's API).

> The plists are pre-filled for this machine: `uv` at `/opt/homebrew/bin/uv`,
> project at `/Users/craigdossantos/Coding/walkingpad`. Edit those paths if they
> change.

## 1. Install the launchd agents

```bash
# one-time: log directory the plists write to
mkdir -p ~/Library/Logs/walkingpad

# copy and load
cp deploy/com.walkingpad.daemon.plist  ~/Library/LaunchAgents/
cp deploy/com.walkingpad.menubar.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.walkingpad.daemon.plist
launchctl load ~/Library/LaunchAgents/com.walkingpad.menubar.plist
```

Verify:

```bash
launchctl list | grep walkingpad        # both listed
curl -s http://127.0.0.1:8787/today      # JSON totals
open http://127.0.0.1:8787               # the dashboard
tail -f ~/Library/Logs/walkingpad/daemon.log
```

The daemon auto-reconnects whenever the pad wakes and is in range. **Keep the
official WalkingPad/KS Fit app closed** — the pad allows only one Bluetooth
connection, and the daemon owns it.

### Troubleshooting: Bluetooth permission

macOS grants Bluetooth access per-binary (TCC), and a `launchd` agent does **not**
inherit the grant you gave Terminal. So after first load, the daemon may connect
to nothing even with the pad on. Check `~/Library/Logs/walkingpad/daemon.err.log`:

- If it shows BLE/permission errors or only ever "retrying", open
  **System Settings → Privacy & Security → Bluetooth** and enable the entry for
  the daemon (it may appear as `uv`, `python`, or `walkingpad`). If nothing
  appears to toggle, run `uv run python -m walkingpad.cli capture` once from
  Terminal, approve the Bluetooth prompt, then `launchctl kickstart -k
gui/$(id -u)/com.walkingpad.daemon`.
- Confirm the official WalkingPad/KS Fit app is fully closed (single connection).

### Update / uninstall

```bash
# after pulling new code, restart the daemon:
launchctl kickstart -k gui/$(id -u)/com.walkingpad.daemon

# remove entirely:
launchctl unload ~/Library/LaunchAgents/com.walkingpad.daemon.plist
launchctl unload ~/Library/LaunchAgents/com.walkingpad.menubar.plist
rm ~/Library/LaunchAgents/com.walkingpad.{daemon,menubar}.plist
```

## 2. Apple Health sync (iPhone Shortcut)

The daemon writes `~/Library/Mobile Documents/com~apple~CloudDocs/WalkingPad/daily.json`
(visible on the phone as **iCloud Drive → WalkingPad → daily.json**). Each entry:

```json
{
  "date": "2026-05-23",
  "distance_km": 0.08,
  "steps": 155,
  "duration_min": 2,
  "sessions": 1
}
```

### One-time phone setup

1. **Settings → Health → Data Access & Devices → Shortcuts** → enable, and make
   sure **Workouts** is allowed to be written.
2. Open **Shortcuts** and create a new shortcut named **"Log WalkingPad"**:
   1. **Get File** (Files action) → service **iCloud Drive**, path
      `WalkingPad/daily.json`. Turn _Show Document Picker_ OFF.
   2. **Get Contents of File** → then **Get Dictionary from Input**.
   3. **Get Dictionary Value** → key `days` (this is the list of days).
   4. **Get Dictionary Value** → for "yesterday": add a **Date** action set to
      _Current Date_, **Adjust Date** by −1 day, **Format Date** as `yyyy-MM-dd`.
      Then **Filter** the `days` list where `date` _is_ that formatted string and
      take the **First Item**.
   5. From that item, read `distance_km` and `duration_min`
      (**Get Dictionary Value** twice).
   6. **Log Workout** → Activity **Walking**, **Duration** = `duration_min`
      minutes, **Distance** = `distance_km` kilometers.
3. Test: run the shortcut once after a walk and confirm a Walking workout appears
   in the Health app for yesterday.

### Make it hands-off

**Shortcuts → Automation → New → Time of Day → 6:00 AM, Daily** → run
**"Log WalkingPad"** → turn **Ask Before Running** OFF.

Each morning it logs _yesterday's_ completed total once — no taps. (It only ever
reads the previous, finished day, so it won't double-log today.)
