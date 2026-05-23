#!/usr/bin/env python3
"""
WalkingPad BLE fingerprint scanner.

Goal: figure out *exactly* what your WALKINGPAD P1 exposes over Bluetooth LE,
so we know which integration path to build (known WalkingPad protocol vs. the
standard FTMS fitness profile vs. something locked/unknown).

It does two things:
  1. Scans for nearby BLE devices and prints them (name, address, signal,
     advertised service UUIDs) so we can spot the pad.
  2. Connects to the most likely pad (or one you pass as an argument) and dumps
     every GATT service + characteristic, then prints a verdict.

USAGE
  pip install bleak
  python3 scan_pad.py                 # scan, auto-pick a likely pad, inspect it
  python3 scan_pad.py --scan-only     # just list everything, don't connect
  python3 scan_pad.py <ADDRESS>       # inspect a specific device by address/UUID

BEFORE RUNNING
  - Turn the walking pad ON (it must be powered/awake to advertise BLE).
  - FORCE-CLOSE the official WalkingPad / KS Fit app on your phone. The pad only
    allows ONE Bluetooth connection at a time; if the app is connected, this
    script can't connect.
  - macOS: the first run may need Bluetooth permission for your terminal.
    System Settings -> Privacy & Security -> Bluetooth -> enable your terminal
    app (Terminal / iTerm). If you get no results, that's usually why.
"""

import asyncio
import sys

try:
    from bleak import BleakScanner, BleakClient
except ImportError:
    sys.exit("Missing dependency. Run:  pip install bleak")

# Names that strongly suggest a walking pad / treadmill.
PAD_NAME_HINTS = ("walkingpad", "ks", "kingsmith", "foot", "tread", "wlk", "r1", "r2", "a1", "c1", "p1")

# UUIDs we recognize, so the verdict can be concrete instead of a guess.
KNOWN_SERVICES = {
    "00001826-0000-1000-8000-00805f9b34fb": "Standard FTMS (Fitness Machine Service) -> EASY, off-the-shelf tooling works",
    "00001000-0000-1000-8000-00805f9b34fb": "Classic WalkingPad proprietary service -> ph4-walkingpad library should work",
    "0000fe00-0000-1000-8000-00805f9b34fb": "Vendor service (FE00) often used by WalkingPad/Xiaomi-style pads -> likely supported",
    "0000180a-0000-1000-8000-00805f9b34fb": "Device Information (model/firmware) - informational",
    "0000180f-0000-1000-8000-00805f9b34fb": "Battery Service - informational",
}


def looks_like_pad(name: str | None) -> bool:
    if not name:
        return False
    low = name.lower()
    return any(hint in low for hint in PAD_NAME_HINTS)


async def scan(timeout: float = 8.0):
    print(f"Scanning for BLE devices for {timeout:.0f}s ...\n")
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)

    rows = []
    for address, (dev, adv) in found.items():
        rows.append((adv.rssi if adv.rssi is not None else -999, address, dev.name, adv.service_uuids))
    rows.sort(reverse=True)  # strongest signal first

    if not rows:
        print("No BLE devices found.")
        print("  - Is the pad powered on?")
        print("  - macOS: did you grant Bluetooth permission to your terminal?")
        return []

    print(f"{'RSSI':>5}  {'ADDRESS':36}  NAME")
    print("-" * 78)
    candidates = []
    for rssi, address, name, svc_uuids in rows:
        flag = "  <-- likely the pad" if looks_like_pad(name) else ""
        print(f"{rssi:>5}  {address:36}  {name or '(no name)'}{flag}")
        if svc_uuids:
            for u in svc_uuids:
                print(f"{'':>5}  {'':36}    adv service: {u}")
        if looks_like_pad(name):
            candidates.append((rssi, address, name))

    print()
    return candidates


async def inspect(address: str):
    print(f"\nConnecting to {address} ...")
    print("(If this hangs or fails, the official app is probably still connected.)\n")
    try:
        async with BleakClient(address) as client:
            print(f"Connected: {client.is_connected}\n")
            print("GATT services & characteristics")
            print("=" * 78)

            found_uuids = []
            for service in client.services:
                label = KNOWN_SERVICES.get(service.uuid.lower(), "")
                tag = f"   [{label}]" if label else ""
                print(f"\nSERVICE  {service.uuid}  ({service.description}){tag}")
                found_uuids.append(service.uuid.lower())
                for ch in service.characteristics:
                    props = ",".join(ch.properties)
                    print(f"   CHAR  {ch.uuid}  [{props}]  {ch.description}")

            print("\n" + "=" * 78)
            print("VERDICT")
            print("=" * 78)
            verdicts = [KNOWN_SERVICES[u] for u in found_uuids if u in KNOWN_SERVICES
                        and "informational" not in KNOWN_SERVICES[u]]
            if verdicts:
                for v in verdicts:
                    print(f"  ✓ {v}")
            else:
                print("  ? No service I recognize. Paste the full dump above back to me and")
                print("    I'll work out the protocol from the characteristic UUIDs/properties.")
    except Exception as e:
        print(f"Could not connect/inspect: {e}")
        print("  - Make sure the official app is fully closed (only one BLE connection allowed).")
        print("  - Try passing the address explicitly:  python3 scan_pad.py <ADDRESS>")


async def main():
    args = [a for a in sys.argv[1:]]

    if args and args[0] == "--scan-only":
        await scan()
        return

    # If an address was passed, skip scanning and inspect it directly.
    if args and not args[0].startswith("-"):
        await inspect(args[0])
        return

    candidates = await scan()
    if not candidates:
        print("No obvious walking-pad-looking device found.")
        print("Look at the list above for an unnamed device with strong signal that")
        print("appears only when the pad is on, then run:  python3 scan_pad.py <ADDRESS>")
        return

    # Inspect the strongest-signal candidate automatically.
    _, address, name = candidates[0]
    print(f"Auto-inspecting best candidate: {name or '(no name)'} @ {address}")
    await inspect(address)


if __name__ == "__main__":
    asyncio.run(main())
