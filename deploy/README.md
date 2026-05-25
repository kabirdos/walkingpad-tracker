# Deploying WalkingPad Tracker

Two pieces run unattended on your Mac via `launchd`, plus a one-time iPhone
Shortcut that logs walks into Apple Health.

- **`com.walkingpad.daemon`** — holds the Bluetooth connection, records walks to
  SQLite, serves the API + dashboard on `http://127.0.0.1:8787`, and refreshes
  the iCloud `daily.json` summary.
- **`com.walkingpad.menubar`** — the menu-bar widget (reads the daemon's API).

## 1. Install the launchd agents

The installer generates the plists from _your_ paths (`uv` location, repo
directory, `$HOME`) and loads them — nothing machine-specific is committed.

```bash
./deploy/install.sh
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
inherit the grant you gave Terminal. So after first install, the daemon may
connect to nothing even with the pad on. Check
`~/Library/Logs/walkingpad/daemon.err.log`:

- If it shows BLE/permission errors or only ever "retrying", open
  **System Settings → Privacy & Security → Bluetooth** and enable the entry for
  the daemon (it may appear as `uv`, `python`, or `walkingpad`). If nothing
  appears to toggle, run `uv run python -m walkingpad.cli capture` once from
  Terminal, approve the Bluetooth prompt, then
  `launchctl kickstart -k gui/$(id -u)/com.walkingpad.daemon`.
- Confirm the official WalkingPad/KS Fit app is fully closed (single connection).

### Update / uninstall

```bash
# after pulling new code, restart the daemon:
launchctl kickstart -k gui/$(id -u)/com.walkingpad.daemon

# remove entirely:
./deploy/uninstall.sh
```

## 2. Apple Health sync (iPhone Shortcut)

The daemon serves yesterday's totals at **`http://<your-mac>.local:8787/yesterday`**
(find `<your-mac>` with `scutil --get LocalHostName`). On the same Wi-Fi, your
phone can read it with the easy-to-find **Get Contents of URL** action:

```json
{
  "date": "2026-05-23",
  "distance_km": 0.08,
  "distance_mi": 0.05,
  "steps": 155,
  "duration_min": 2,
  "sessions": 1
}
```

> The daemon binds to `0.0.0.0` so the phone can reach it on your LAN — fine on a
> trusted home network (anyone on it could view the dashboard / control the pad).
> The same data is also written to `iCloud Drive/WalkingPad/yesterday.json` as an
> offline fallback.

### One-time phone setup

1. **Health app → your profile photo → Apps and Services → Shortcuts → Allow
   Shortcuts to Write Data** → turn on **Workouts**, **Walking + Running
   Distance**, **Active Energy**, and **Steps**. Logging a walk with distance
   and steps needs all of these — if one is off, the log action fails.
2. Open **Shortcuts**, create **"Log WalkingPad"** with these actions:
   1. **Get Contents of URL** → `http://<your-mac>.local:8787/yesterday`
      — plain `http`, **not** https; returns JSON, which Shortcuts treats as a
      dictionary. If the `.local` name is unreliable in Shortcuts, use your
      Mac's LAN IP instead, e.g. `http://192.168.1.50:8787/yesterday`.
   2. **Date** (current date) → **Adjust Date** → **Subtract** `1` **Day**.
      Its output ("yesterday") is fed into the log actions below so entries land
      on the correct day instead of the moment the Shortcut runs.
   3. **Get Dictionary Value** → key `distance_mi` (input: _Contents of URL_).
   4. **Get Dictionary Value** → key `duration_min` (input: _Contents of URL_).
   5. **Get Dictionary Value** → key `steps` (input: _Contents of URL_).
   6. **Log Workout** → Activity **Walking**, **Start Date** = the adjusted date,
      **Duration** = `duration_min` (minutes), **Distance** = `distance_mi`
      (miles), **Calories** = `0`. Calories is required — an empty field makes
      this action error with a generic message; the pad reports no calories.
   7. **Log Health Sample** → type **Steps**, **Value** = `steps`, **Start/End
      Date** = the adjusted date. (Log Workout can't write steps — they're a
      separate Health data type, so this second action is what records them.)
3. Run it once and confirm a Walking workout **and** the step count appear in
   Health under yesterday's date.

### Make it hands-off

**Shortcuts → Automation → New → Time of Day → 6:00 AM, Daily** → **Run
Immediately** → run **"Log WalkingPad"**. (On older iOS, turn **Ask Before
Running** OFF instead.)

Each morning it logs _yesterday's_ completed total once — no taps. (It only ever
reads the previous, finished day, so it won't double-log today.)
