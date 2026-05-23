"""macOS menubar widget. Run with: uv run --extra menubar python -m walkingpad.menubar

Reads the local daemon API (default http://127.0.0.1:8787) and shows today's
stats at a glance plus start/stop/speed controls. Requires the `menubar` extra
(rumps); kept separate from the core so the test suite needs no GUI deps.
"""
import json
import os
import urllib.request
import webbrowser

import rumps

from walkingpad.menubar_core import format_title, summary_lines

API = os.environ.get("WALKINGPAD_API", "http://127.0.0.1:8787")


def _get(path):
    with urllib.request.urlopen(API + path, timeout=2) as r:
        return json.loads(r.read())


def _post(path, body=None):
    data = json.dumps(body).encode() if body else b""
    req = urllib.request.Request(API + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=2).read()


class WalkingPadBar(rumps.App):
    def __init__(self):
        super().__init__("\U0001f6b6 …", quit_button="Quit")
        self.summary_items = [rumps.MenuItem(t) for t in summary_lines(None)]
        speed_menu = ("Speed", [
            rumps.MenuItem(f"{s:.1f} km/h", callback=self._make_speed(s))
            for s in (2.0, 3.0, 4.0, 5.0, 6.0)
        ])
        self.menu = [
            *self.summary_items,
            None,
            rumps.MenuItem("Start", callback=self.on_start),
            rumps.MenuItem("Stop", callback=self.on_stop),
            speed_menu,
            None,
            rumps.MenuItem("Open Dashboard", callback=self.on_dashboard),
        ]

    @rumps.timer(3)
    def refresh(self, _):
        try:
            status = _get("/status")
        except Exception:
            status = None
        try:
            today = _get("/today")
        except Exception:
            today = None
        self.title = format_title(status, today)
        for item, text in zip(self.summary_items, summary_lines(today)):
            item.title = text

    def _make_speed(self, kmh):
        def cb(_):
            self._safe(lambda: _post("/control/speed", {"kmh": kmh}))
        return cb

    def on_start(self, _):
        self._safe(lambda: _post("/control/start"))

    def on_stop(self, _):
        self._safe(lambda: _post("/control/stop"))

    def on_dashboard(self, _):
        webbrowser.open(API)

    def _safe(self, fn):
        try:
            fn()
        except Exception as e:
            rumps.notification("WalkingPad", "Command failed", str(e))


def main():
    WalkingPadBar().run()


if __name__ == "__main__":
    main()
