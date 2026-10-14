"""Device profiles and pure conversions, shared by flows and entities."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .const import MINIMUM_BRIGHTNESS, MIRED_MAX, MIRED_MIN, REGISTER_BASE


class Model(StrEnum):
    P404 = "p404"
    P210 = "p210"


class GroupMode(StrEnum):
    SINGLE = "single"
    CCT = "cct"


@dataclass(frozen=True)
class Output:
    key: str
    name: str
    registers: tuple[int, ...]

    @property
    def cct(self) -> bool:
        return len(self.registers) == 2

    def unique_id(self, entry_id: str, slave: int) -> str:
        return f"{entry_id}_slave_{slave}_{self.key}"


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
        if self.model == Model.P210:
            if self.group_b is not None:
                raise ValueError("P210 does not support Group B")
            object.__setattr__(
                self, "group_a", GroupMode(self.group_a or GroupMode.SINGLE)
            )
        else:
            object.__setattr__(
                self, "group_a", GroupMode(self.group_a or GroupMode.SINGLE)
            )
            object.__setattr__(
                self, "group_b", GroupMode(self.group_b or GroupMode.SINGLE)
            )

    @property
    def poll_count(self) -> int:
        return 4 if self.model == Model.P404 else 2

    @property
    def outputs(self) -> tuple[Output, ...]:
        result: list[Output] = []
        for group, offset, mode in (("a", 0, self.group_a), ("b", 2, self.group_b)):
            if offset == 2 and self.model == Model.P210:
                break
            if mode == GroupMode.CCT:
                result.append(
                    Output(
                        f"group_{group}",
                        f"雙色溫 {group.upper()}",
                        (REGISTER_BASE + offset, REGISTER_BASE + offset + 1),
                    )
                )
            else:
                for index in (offset, offset + 1):
                    result.append(
                        Output(
                            f"ch{index + 1}", f"CH{index + 1}", (REGISTER_BASE + index,)
                        )
                    )
        return tuple(result)

    def as_dict(self) -> dict[str, Any]:
        result = {"slave": self.slave, "model": self.model.value}
        if self.model == Model.P210:
            result["group_a"] = self.group_a.value
        else:
            result.update(group_a=self.group_a.value, group_b=self.group_b.value)
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
