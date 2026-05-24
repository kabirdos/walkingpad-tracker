#!/usr/bin/env bash
# Install WalkingPad Tracker as launchd agents on macOS. Idempotent — safe to
# re-run after pulling new code. Generates the plists from *your* paths, so
# nothing machine-specific is committed to the repo.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UV="$(command -v uv || true)"
if [ -z "$UV" ]; then
  echo "error: 'uv' not found on PATH. Install it first: https://docs.astral.sh/uv/" >&2
  exit 1
fi
LOGS="$HOME/Library/Logs/walkingpad"
AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$LOGS" "$AGENTS"

write_plist() {
  local label="$1"; shift
  local short="${label##*.}"          # com.walkingpad.daemon -> daemon
  local out="$AGENTS/$label.plist"
  {
    echo '<?xml version="1.0" encoding="UTF-8"?>'
    echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
    echo '<plist version="1.0"><dict>'
    echo "  <key>Label</key><string>$label</string>"
    echo '  <key>ProgramArguments</key><array>'
    for a in "$@"; do echo "    <string>$a</string>"; done
    echo '  </array>'
    echo '  <key>RunAtLoad</key><true/>'
    echo '  <key>KeepAlive</key><true/>'
    echo "  <key>StandardOutPath</key><string>$LOGS/$short.log</string>"
    echo "  <key>StandardErrorPath</key><string>$LOGS/$short.err.log</string>"
    echo '</dict></plist>'
  } > "$out"
  echo "wrote $out"
}

write_plist com.walkingpad.daemon  "$UV" run --directory "$DIR" python -m walkingpad.cli serve
write_plist com.walkingpad.menubar "$UV" run --directory "$DIR" --extra menubar python -m walkingpad.menubar

for label in com.walkingpad.daemon com.walkingpad.menubar; do
  launchctl unload "$AGENTS/$label.plist" 2>/dev/null || true
  launchctl load "$AGENTS/$label.plist"
done

echo "loaded. dashboard: http://127.0.0.1:8787   logs: $LOGS"
echo "tip: keep the official WalkingPad/KS Fit app closed (one BLE connection at a time)."
