"""Device and channel rules used by the Home Assistant runtime."""

from __future__ import annotations

from dataclasses import dataclass

from .protocol import write


@dataclass(frozen=True)
class Channel:
    model: str
    slave: int
    channel: str
    kind: str
    name: str
    minimum: int = 2
    mired_min: int = 175
    mired_max: int = 455

    def __post_init__(self) -> None:
        allowed = {
            "p404": {"1", "2", "3", "4", "a", "b"},
            "p210": {"1", "2"},
            "U4": {"1"},
        }
        if self.channel not in allowed.get(self.model, set()):
            raise ValueError("Unsupported model/channel")
        expected = "dual" if self.channel in ("a", "b") else "single"
        if self.kind != expected:
            raise ValueError("Channel type mismatch")
        if not 1 <= self.slave <= 247 or not 1 <= self.minimum <= 100:
            raise ValueError("Invalid slave/brightness")
        if not 0 < self.mired_min < self.mired_max:
            raise ValueError("Invalid color temperature range")

    @property
    def registers(self) -> tuple[int, ...]:
        return {
            "1": (0x082A,),
            "2": (0x082B,),
            "3": (0x082C,),
            "4": (0x082D,),
            "a": (0x082A, 0x082B),
            "b": (0x082C, 0x082D),
        }[self.channel]

    @property
    def topic(self) -> str:
        return f"homeassistant/light/{self.kind}/{self.slave}/{self.channel}"

    @property
    def poll_count(self) -> int:
        return 2 if self.model == "p210" else 4

    def brightness_command(self, value: int) -> bytes:
        if type(value) is not int or not 0 <= value <= 100:
            raise ValueError("Brightness must be an integer 0..100")
        return write(self.slave, self.registers[0], max(self.minimum, value) if value else 0)

    def temperature_command(self, mired: int) -> bytes:
        if self.kind != "dual" or not self.mired_min <= mired <= self.mired_max:
            raise ValueError("Invalid color temperature")
        raw = round((self.mired_max - mired) * 100 / (self.mired_max - self.mired_min))
        return write(self.slave, self.registers[1], raw)

    def states(self, registers: list[int]) -> dict[str, str]:
        brightness = registers[self.registers[0] - 0x082A] & 0xFF
        if not 0 <= brightness <= 100:
            raise ValueError("Invalid raw brightness")
        result = {
            f"{self.topic}/state": "ON" if brightness else "OFF",
            f"{self.topic}/brightness": str(brightness),
        }
        if self.kind == "dual":
            raw = registers[self.registers[1] - 0x082A] & 0xFF
            if not 0 <= raw <= 100:
                raise ValueError("Invalid raw color temperature")
            result[f"{self.topic}/colortemp"] = str(
                round(self.mired_max - raw / 100 * (self.mired_max - self.mired_min))
            )
        return result


def validate_channels(channels: list[Channel]) -> None:
    """Reject duplicate registers and conflicting models for one slave."""
    occupied: set[tuple[int, int]] = set()
    models: dict[int, str] = {}
    for channel in channels:
        if models.setdefault(channel.slave, channel.model) != channel.model:
            raise ValueError("Conflicting device model")
        for register in channel.registers:
            key = channel.slave, register
            if key in occupied:
                raise ValueError("Overlapping channels")
            occupied.add(key)


def channel_from_mapping(value: dict[str, object]) -> Channel:
    """Validate and construct a channel from persisted entry data."""
    return Channel(
        model=str(value["model"]),
        slave=int(value["slave"]),
        channel=str(value["channel"]),
        kind=str(value["kind"]),
        name=str(value["name"]),
        minimum=int(value.get("minimum", 2)),
        mired_min=int(value.get("mired_min", 175)),
        mired_max=int(value.get("mired_max", 455)),
    )
