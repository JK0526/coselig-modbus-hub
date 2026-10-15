"""Pure parsing and configuration helpers for S402 asynchronous inputs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .protocol import frame

S402_SIGNATURE = b"\x01\x06\x01\xa0"
S402_DI_BITS = {0x01: 1, 0x02: 2, 0x04: 3, 0x08: 4}
S402_ID_MIN = 0
S402_ID_MAX = 255


@dataclass(frozen=True)
class S402Event:
    """One complete single-click report from an S402 DI."""

    device_id: int
    di: int


def is_s402_candidate(packet: bytes) -> bool:
    """Return whether a frame uses the reserved unsolicited S402 signature."""
    return len(packet) >= len(S402_SIGNATURE) and packet.startswith(S402_SIGNATURE)


def parse_s402_event(packet: bytes) -> S402Event | None:
    """Parse a valid S402 frame; return None for any non-event or invalid frame."""
    if len(packet) != 8 or not packet.startswith(S402_SIGNATURE):
        return None
    if frame(packet[:-2]) != packet:
        return None
    di = S402_DI_BITS.get(packet[5])
    if di is None:
        return None
    return S402Event(device_id=packet[4], di=di)


def s402_devices_from_options(options: dict[str, Any]) -> tuple[int, ...]:
    """Read optional, future-extensible S402 device objects from entry options."""
    devices = options.get("s402_devices", [])
    if not isinstance(devices, list):
        raise ValueError("s402_devices must be a list")
    ids: list[int] = []
    for device in devices:
        if not isinstance(device, dict):
            raise ValueError("Each S402 device must be an object")
        device_id = device.get("id")
        if type(device_id) is not int or not S402_ID_MIN <= device_id <= S402_ID_MAX:
            raise ValueError("S402 ID must be 0..255")
        if device_id in ids:
            raise ValueError("S402 IDs must be unique")
        ids.append(device_id)
    return tuple(ids)
