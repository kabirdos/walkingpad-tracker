import asyncio
import time

from bleak import BleakScanner
from ph4_walkingpad.pad import Controller

from walkingpad.status import cur_status_to_padstatus


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
        # ph4 may call the handler as (sender, record) or (record); take last arg.
        self.ctrl.handler_cur_status = self._on_cur_status

    def _on_cur_status(self, *args):
        record = args[-1]
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
        await self.ctrl.run(address)
        self._address = address
        return address

    async def disconnect(self):
        try:
            await self.ctrl.disconnect()
        except Exception:
            pass

    async def poll(self):
        await self.ctrl.ask_stats()

    async def start_belt(self):
        await self.ctrl.start_belt()

    async def stop_belt(self):
        await self.ctrl.stop_belt()

    async def set_speed(self, kmh):
        await self.ctrl.change_speed(kmh_to_pad_speed(kmh))

    async def capture(self, callback, interval=0.8):
        """Poll the pad forever, pushing PadStatus to callback.

        Raises on connection loss so the caller's reconnect loop can take over.
        """
        self._callback = callback
        while True:
            await self.poll()
            await asyncio.sleep(interval)
