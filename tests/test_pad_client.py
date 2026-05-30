import asyncio

import pytest
from ph4_walkingpad.pad import WalkingPad

import walkingpad.pad_client as pad_client
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


class HangingCtrl:
    """Stand-in for ph4 Controller whose connect (`run`) hangs forever — the
    failure mode we observed when CoreBluetooth's peripheral handle goes stale
    and bleak never returns from connect."""

    def __init__(self):
        self.disconnected = False

    async def run(self, address):
        await asyncio.Event().wait()  # hangs until cancelled

    async def disconnect(self):
        self.disconnected = True


def test_connect_times_out_when_bleak_hangs(monkeypatch):
    # If bleak.connect hangs (stale CoreBluetooth handle), PadClient.connect
    # must bail within CONNECT_TIMEOUT_S, force a disconnect to clear state,
    # and raise so the capture loop's backoff runs instead of locking up.
    monkeypatch.setattr(pad_client, "CONNECT_TIMEOUT_S", 0.05)
    pad = PadClient()
    pad.ctrl = HangingCtrl()

    with pytest.raises(RuntimeError, match="connect timed out"):
        asyncio.run(pad.connect(address="fake-address"))

    assert pad.ctrl.disconnected, (
        "must disconnect on timeout so the next attempt starts clean"
    )


class FullyWedgedCtrl:
    """Worst case: both connect AND disconnect hang. Reproduces the failure
    mode where CoreBluetooth's stale-handle state breaks both calls — we still
    need to raise so the capture loop can recover (vs. silently locking up
    inside the timeout handler itself)."""

    async def run(self, address):
        await asyncio.Event().wait()

    async def disconnect(self):
        await asyncio.Event().wait()


def test_connect_still_raises_when_disconnect_also_hangs(monkeypatch):
    monkeypatch.setattr(pad_client, "CONNECT_TIMEOUT_S", 0.05)
    monkeypatch.setattr(pad_client, "DISCONNECT_TIMEOUT_S", 0.05)
    pad = PadClient()
    pad.ctrl = FullyWedgedCtrl()

    # Must complete (not hang) and raise; otherwise the capture loop sits
    # forever in the timeout handler and we've just moved the bug.
    with pytest.raises(RuntimeError, match="connect timed out"):
        asyncio.run(asyncio.wait_for(pad.connect(address="fake-address"),
                                     timeout=2.0))
