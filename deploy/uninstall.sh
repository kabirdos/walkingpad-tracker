#!/usr/bin/env bash
# Remove the WalkingPad launchd agents.
set -euo pipefail
AGENTS="$HOME/Library/LaunchAgents"
for label in com.walkingpad.daemon com.walkingpad.menubar; do
  launchctl unload "$AGENTS/$label.plist" 2>/dev/null || true
  rm -f "$AGENTS/$label.plist"
  echo "removed $label"
done
echo "done."
