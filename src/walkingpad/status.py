from dataclasses import dataclass


@dataclass
class PadStatus:
    ts: float            # epoch seconds when observed
    speed_kmh: float
    distance_m: int
    steps: int
    elapsed_s: int
    belt_state: int


def cur_status_to_padstatus(record, ts):
    """Convert a ph4_walkingpad WalkingPadCurStatus into our PadStatus.

    Wire units: speed=0.1 km/h, dist=0.01 km (so *10 = metres), time=seconds.
    """
    return PadStatus(
        ts=ts,
        speed_kmh=record.speed / 10.0,
        distance_m=int(record.dist) * 10,
        steps=int(record.steps),
        elapsed_s=int(record.time),
        belt_state=int(record.belt_state),
    )
