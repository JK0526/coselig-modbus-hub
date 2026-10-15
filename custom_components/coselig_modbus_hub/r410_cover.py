"""Pure R410 momentary-cover configuration and request encoding."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from homeassistant.components.cover import CoverDeviceClass

from .const import DOMAIN
from .models import Device, Model
from .protocol import write_single_register

MOMENTARY_REGISTER = 0x019B


class R410CoverMode(StrEnum):
    """Physical relay arrangement for one dry-contact controller."""

    TWO_RELAY = "two_relay"
    THREE_RELAY = "three_relay"


class R410CoverConfigError(ValueError):
    """Persisted R410 Cover configuration is malformed or unsafe."""


@dataclass(frozen=True)
class R410CoverConfig:
    id: str
    slave: int
    name: str
    device_class: CoverDeviceClass | None
    mode: R410CoverMode
    open_relay: int
    close_relay: int
    pulse_tenths: int
    stop_relay: int | None = None

    @property
    def relays(self) -> tuple[int, ...]:
        if self.mode == R410CoverMode.TWO_RELAY:
            return (self.open_relay, self.close_relay)
        return (self.open_relay, self.stop_relay, self.close_relay)

    def action_mask(self, action: str) -> int:
        """Return the configured relay mask for OPEN, STOP, or CLOSE."""
        relay_numbers = {
            "open": (self.open_relay,),
            "close": (self.close_relay,),
            "stop": (
                (self.open_relay, self.close_relay)
                if self.mode == R410CoverMode.TWO_RELAY
                else (self.stop_relay,)
            ),
        }.get(action)
        if relay_numbers is None:
            raise ValueError("Unknown R410 Cover action")
        return sum(1 << (relay - 1) for relay in relay_numbers)

    def request(self, action: str) -> bytes:
        """Build one hardware-timed FC06 momentary command."""
        value = (self.pulse_tenths << 8) | self.action_mask(action)
        return write_single_register(self.slave, MOMENTARY_REGISTER, value)

    def unique_id(self, entry_id: str) -> str:
        return f"{entry_id}_slave_{self.slave}_cover_{self.id}"

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "slave": self.slave,
            "name": self.name,
            "device_class": self.device_class.value if self.device_class else None,
            "mode": self.mode.value,
            "open_relay": self.open_relay,
            "close_relay": self.close_relay,
            "pulse_tenths": self.pulse_tenths,
        }
        if self.mode == R410CoverMode.THREE_RELAY:
            result["stop_relay"] = self.stop_relay
        return result


def parse_r410_cover(value: dict[str, Any]) -> R410CoverConfig:
    """Parse one Cover record without guessing or coercing unsafe values."""
    if not isinstance(value, dict):
        raise R410CoverConfigError("Cover record must be an object")
    try:
        cover_id = UUID(str(value["id"])).hex
        slave = value["slave"]
        if type(slave) is not int or not 1 <= slave <= 247:
            raise ValueError("Invalid Cover Slave")
        name = value["name"]
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Cover name must not be empty")
        name = name.strip()
        mode = R410CoverMode(value["mode"])
        open_relay = _relay(value["open_relay"])
        close_relay = _relay(value["close_relay"])
        if mode == R410CoverMode.TWO_RELAY:
            if "stop_relay" in value:
                raise ValueError("Two-relay Cover cannot set stop_relay")
            stop_relay = None
        else:
            stop_relay = _relay(value["stop_relay"])
        configured_relays = [open_relay, close_relay]
        if stop_relay is not None:
            configured_relays.append(stop_relay)
        if len(set(configured_relays)) != len(configured_relays):
            raise ValueError("A Cover cannot reuse a Relay")
        pulse_tenths = value["pulse_tenths"]
        if type(pulse_tenths) is not int or not 1 <= pulse_tenths <= 250:
            raise ValueError("pulse_tenths must be an integer from 1 to 250")
        device_class_raw = value.get("device_class")
        device_class = (
            None if device_class_raw is None else CoverDeviceClass(device_class_raw)
        )
        return R410CoverConfig(
            id=cover_id,
            slave=slave,
            name=name,
            device_class=device_class,
            mode=mode,
            open_relay=open_relay,
            close_relay=close_relay,
            pulse_tenths=pulse_tenths,
            stop_relay=stop_relay,
        )
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise R410CoverConfigError("Invalid R410 Cover settings") from error


def _relay(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= 4:
        raise ValueError("Relay must be an integer from 1 to 4")
    return value


def r410_covers_from_options(
    options: dict[str, Any], devices: tuple[Device, ...]
) -> tuple[R410CoverConfig, ...]:
    """Validate all stored Covers against the configured physical R410s."""
    raw_covers = options.get("r410_covers", [])
    if not isinstance(raw_covers, list):
        raise R410CoverConfigError("r410_covers must be a list")
    devices_by_slave = {device.slave: device for device in devices}
    covers: list[R410CoverConfig] = []
    ids: set[str] = set()
    occupied: dict[int, set[int]] = {}
    for raw in raw_covers:
        cover = parse_r410_cover(raw)
        if cover.id in ids:
            raise R410CoverConfigError("Duplicate R410 Cover ID")
        ids.add(cover.id)
        device = devices_by_slave.get(cover.slave)
        if device is None or device.model != Model.R410:
            raise R410CoverConfigError("Cover must reference a configured R410")
        used = occupied.setdefault(cover.slave, set())
        if used.intersection(cover.relays):
            raise R410CoverConfigError("R410 Relay is already assigned to a Cover")
        used.update(cover.relays)
        covers.append(cover)
    return tuple(covers)


def occupied_relays(covers: tuple[R410CoverConfig, ...], slave: int) -> frozenset[int]:
    """Relay numbers assigned to Covers on one physical R410."""
    return frozenset(
        relay for cover in covers if cover.slave == slave for relay in cover.relays
    )


def expected_cover_unique_ids(
    entry_id: str, covers: tuple[R410CoverConfig, ...]
) -> set[str]:
    return {cover.unique_id(entry_id) for cover in covers}


def expected_physical_output_unique_ids(
    entry_id: str, devices: tuple[Device, ...]
) -> set[str]:
    """Keep every physical output identity while a Cover temporarily owns it."""
    return {
        output.unique_id(entry_id, device.slave)
        for device in devices
        for output in device.outputs
    }


def unique_cover_id() -> str:
    """Allocate a new immutable Cover identity."""
    from uuid import uuid4

    return uuid4().hex


def cover_device_identifier(entry_id: str, slave: int) -> tuple[str, str]:
    """Return the existing physical R410 Parent Device identifier."""
    return DOMAIN, f"{entry_id}_slave_{slave}"
