"""Device profiles and pure conversions, shared by flows and entities."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .profiles import DeviceProfile, OutputSpec

from .const import MINIMUM_BRIGHTNESS, MIRED_MAX, MIRED_MIN


class Model(StrEnum):
    P404 = "p404"
    P210 = "p210"
    R410 = "r410"


class GroupMode(StrEnum):
    SINGLE = "single"
    CCT = "cct"


@dataclass(frozen=True)
class Device:
    slave: int
    model: Model
    group_a: GroupMode | None = None
    group_b: GroupMode | None = None

    def __post_init__(self) -> None:
        if type(self.slave) is not int or not 1 <= self.slave <= 247:
            raise ValueError("Slave must be an integer 1..247")
        object.__setattr__(self, "model", Model(self.model))
        profile = self.profile
        if self.group_b is not None and "group_b" not in profile.group_fields:
            raise ValueError(f"{self.model} does not support Group B")
        if self.group_a is not None and "group_a" not in profile.group_fields:
            raise ValueError(f"{self.model} does not support Group A")
        for field in profile.group_fields:
            object.__setattr__(
                self, field, GroupMode(getattr(self, field) or GroupMode.SINGLE)
            )

    @property
    def profile(self) -> DeviceProfile:
        from .profiles import PROFILES

        return PROFILES[self.model]

    @property
    def poll_count(self) -> int:
        return self.profile.poll_specs(self)[0].count

    @property
    def outputs(self) -> tuple[OutputSpec, ...]:
        return self.profile.outputs(self)

    def as_dict(self) -> dict[str, Any]:
        result = {"slave": self.slave, "model": self.model.value}
        result.update(
            {field: getattr(self, field).value for field in self.profile.group_fields}
        )
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Device:
        return cls(
            value["slave"],
            Model(value["model"]),
            value.get("group_a"),
            value.get("group_b"),
        )


def devices_from_options(options: dict[str, Any]) -> tuple[Device, ...]:
    devices = tuple(Device.from_dict(value) for value in options.get("devices", []))
    if len({device.slave for device in devices}) != len(devices):
        raise ValueError("Duplicate slave")
    return devices


def brightness_to_raw(value: int) -> int:
    if type(value) is not int or not 0 <= value <= 255:
        raise ValueError("Brightness must be 0..255")
    return max(MINIMUM_BRIGHTNESS, round(value * 100 / 255)) if value else 0


def raw_to_brightness(value: int) -> int:
    if not 0 <= value <= 100:
        raise ValueError("Invalid raw brightness")
    return round(value * 255 / 100)


def kelvin_to_raw(kelvin: int) -> int:
    # Preserve the legacy linear *mired* mapping; HA exposes Kelvin only.
    mired = min(MIRED_MAX, max(MIRED_MIN, 1_000_000 / kelvin))
    return round((MIRED_MAX - mired) * 100 / (MIRED_MAX - MIRED_MIN))


def raw_to_kelvin(value: int) -> int:
    if not 0 <= value <= 100:
        raise ValueError("Invalid raw temperature")
    return round(1_000_000 / (MIRED_MAX - value * (MIRED_MAX - MIRED_MIN) / 100))
