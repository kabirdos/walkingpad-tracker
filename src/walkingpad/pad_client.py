import asyncio
import time

from bleak import BleakScanner
from ph4_walkingpad.pad import Controller, WalkingPad

from walkingpad.status import cur_status_to_padstatus

# Cap how long we'll wait inside ph4's `Controller.run` (which covers the BLE
# connect AND service/characteristic discovery + notify setup) before bailing.
# A healthy full setup lands in ~1-3s; anything past this is a hung
# CoreBluetooth handle (observed when the pad's BLE state machine was wedged
# from the official app having held the central role). Without this cap the
# capture loop can block indefinitely and the retry/backoff never runs.
CONNECT_TIMEOUT_S = 20.0

# Disconnect can hang under the same wedged-BLE pathology that triggered the
# connect timeout, so cap it tightly — we're throwing away the client anyway.
DISCONNECT_TIMEOUT_S = 5.0


def kmh_to_pad_speed(kmh):
    """Convert km/h to the pad's 0.1 km/h integer units, clamped to 0.5-6.0."""
    raw = int(round(kmh * 10))
    return max(5, min(60, raw))


class PadClient:
    """Thin async adapter over ph4_walkingpad.Controller."""

    def __init__(self, name_hint="WalkingPad"):
        self.name_hint = name_hint
        self.ctrl = Controller()
        self._callback = None
        self._address = None
        self._last_rx = 0.0
        # ph4 may call the handler as (sender, record) or (record); take last arg.
        self.ctrl.handler_cur_status = self._on_cur_status

    def _on_cur_status(self, *args):
        record = args[-1]
        if record is not None:
            self._last_rx = time.time()
        if self._callback and record is not None:
            self._callback(cur_status_to_padstatus(record, time.time()))

    async def discover_address(self):
        devices = await BleakScanner.discover(timeout=8.0)
        for d in devices:
            if d.name and self.name_hint.lower() in d.name.lower():
                return d.address
        return None

    async def connect(self, address=None):
        if address is None:
            address = await self.discover_address()
        if address is None:
            raise RuntimeError(
                "WalkingPad not found. Is it powered on and the official app closed?"
            )
        try:
            await asyncio.wait_for(self.ctrl.run(address),
                                   timeout=CONNECT_TIMEOUT_S)
        except asyncio.TimeoutError:
            # Tear down whatever half-state bleak left behind so the next
            # attempt starts from a clean client; then re-raise as a plain
            # RuntimeError so the caller's reconnect loop logs and backs off.
            await self.disconnect()
            raise RuntimeError(
                f"connect timed out after {CONNECT_TIMEOUT_S:.0f}s; BLE link wedged"
            )
        self._address = address
        self._last_rx = time.time()
        return address

    async def disconnect(self):
        # Cap the disconnect — bleak/CoreBluetooth can hang here under the same
        # wedged-handle state that hangs connect. We're tearing the client down
        # either way, so swallow the timeout (TimeoutError is an Exception).
        try:
            await asyncio.wait_for(self.ctrl.disconnect(),
                                   timeout=DISCONNECT_TIMEOUT_S)
        except Exception:
            pass

    async def poll(self):
        await self.ctrl.ask_stats()

    async def wake(self):
        """Bring a connected pad out of standby into manual mode so the belt
        can run. Only reachable over BLE — a deep-asleep pad (radio off, not
        discoverable) can't be woken from software; it needs the remote."""
        await self.ctrl.switch_mode(WalkingPad.MODE_MANUAL)

    async def start_belt(self):
        await self.ctrl.start_belt()

    async def stop_belt(self):
        await self.ctrl.stop_belt()

    async def set_speed(self, kmh):
        await self.ctrl.change_speed(kmh_to_pad_speed(kmh))

    async def capture(self, callback, interval=0.8, stale_timeout=15.0):
        """Poll the pad forever, pushing PadStatus to callback.

        Raises on connection loss — or when no data has arrived for
        `stale_timeout` seconds (a half-dead BLE link that stops delivering
        notifications without erroring) — so the caller's reconnect loop runs.
        """
        self._callback = callback
        self._last_rx = time.time()
        while True:
            await self.poll()
            await asyncio.sleep(interval)
            if time.time() - self._last_rx > stale_timeout:
                raise RuntimeError(
                    f"no pad data for {stale_timeout:.0f}s; connection stale"
                )
