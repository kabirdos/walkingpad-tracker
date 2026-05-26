import asyncio

from ph4_walkingpad.pad import WalkingPad

from walkingpad.pad_client import PadClient


class FakeCtrl:
    def __init__(self):
        self.mode = None

    async def switch_mode(self, mode):
        self.mode = mode


def test_wake_switches_pad_to_manual_mode():
    # "Wake" brings a connected pad out of standby into manual mode so the belt
    # can run; it must select MANUAL, not standby/automatic.
    pad = PadClient()
    pad.ctrl = FakeCtrl()
    asyncio.run(pad.wake())
    assert pad.ctrl.mode == WalkingPad.MODE_MANUAL
