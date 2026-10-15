"""The single source of model-specific maps, codecs, outputs and capabilities."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from homeassistant.const import Platform

from .const import REGISTER_BASE
from .models import Device, GroupMode, Model
from .protocol import (
    ProtocolError,
    read_coils,
    read_discrete_inputs,
    read_holding_registers,
    read_input_registers,
    write_single_coil,
    write_single_register,
)


class DeviceFamily(StrEnum):
    DIMMER = "dimmer"
    RELAY = "relay"
    UNKNOWN = "unknown"


@dataclass
class ModbusData:
    holding_registers: dict[int, int] = field(default_factory=dict)
    input_registers: dict[int, int] = field(default_factory=dict)
    coils: dict[int, bool] = field(default_factory=dict)
    discrete_inputs: dict[int, bool] = field(default_factory=dict)

    @property
    def populated(self) -> bool:
        return bool(
            self.holding_registers
            or self.input_registers
            or self.coils
            or self.discrete_inputs
        )


@dataclass(frozen=True)
class PollSpec:
    function: int
    start: int
    count: int

    def request(self, slave: int) -> bytes:
        return {
            1: read_coils,
            2: read_discrete_inputs,
            3: read_holding_registers,
            4: read_input_registers,
        }[self.function](slave, self.start, self.count)

    def decode(self, response: bytes, data: ModbusData) -> None:
        if self.function in (1, 2):
            target = data.coils if self.function == 1 else data.discrete_inputs
            target.update(
                {
                    self.start + index: bool(
                        response[3 + index // 8] & (1 << (index % 8))
                    )
                    for index in range(self.count)
                }
            )
        else:
            target = (
                data.holding_registers if self.function == 3 else data.input_registers
            )
            target.update(
                {
                    self.start + index: int.from_bytes(
                        response[3 + 2 * index : 5 + 2 * index], "big"
                    )
                    for index in range(self.count)
                }
            )


@dataclass(frozen=True)
class OutputSpec:
    key: str
    name: str
    registers: tuple[int, ...]
    platform: Platform = Platform.LIGHT

    @property
    def cct(self) -> bool:
        return len(self.registers) == 2

    def unique_id(self, entry_id: str, slave: int) -> str:
        return f"{entry_id}_slave_{slave}_{self.key}"


def encode_dimmer_value(raw: int) -> int:
    if type(raw) is not int or not 0 <= raw <= 100:
        raise ValueError("Invalid dimmer raw value")
    return 0x0500 | raw


def write_dimmer(slave: int, register: int, raw: int) -> bytes:
    if register not in range(REGISTER_BASE, REGISTER_BASE + 4):
        raise ValueError("Invalid dimmer register")
    return write_single_register(slave, register, encode_dimmer_value(raw))


@dataclass(frozen=True)
class DeviceProfile:
    model: Model
    family: DeviceFamily
    poll: PollSpec
    group_fields: tuple[str, ...]
    supported_platforms: tuple[Platform, ...]

    def poll_specs(self, device: Device) -> tuple[PollSpec, ...]:
        return (self.poll,)

    def outputs(self, device: Device) -> tuple[OutputSpec, ...]:
        if self.family == DeviceFamily.RELAY:
            return tuple(
                OutputSpec(
                    f"relay{index + 1}", f"Relay {index + 1}", (index,), Platform.SWITCH
                )
                for index in range(self.poll.count)
            )
        result = []
        for index, field_name in enumerate(self.group_fields):
            offset = index * 2
            group = chr(ord("a") + index)
            if getattr(device, field_name) == GroupMode.CCT:
                result.append(
                    OutputSpec(
                        f"group_{group}",
                        f"雙色溫 {group.upper()}",
                        (REGISTER_BASE + offset, REGISTER_BASE + offset + 1),
                    )
                )
            else:
                result.extend(
                    OutputSpec(
                        f"ch{channel + 1}",
                        f"CH{channel + 1}",
                        (REGISTER_BASE + channel,),
                    )
                    for channel in (offset, offset + 1)
                )
        return tuple(result)

    def decode(self, spec: PollSpec, response: bytes, data: ModbusData) -> None:
        spec.decode(response, data)
        if self.family == DeviceFamily.DIMMER:
            # Retain the legacy low-byte interpretation, including the 0x05 codec.
            values = {
                address: value & 0xFF
                for address, value in data.holding_registers.items()
            }
            if any(value > 100 for value in values.values()):
                raise ProtocolError("Invalid dimmer raw register value")
            data.holding_registers = values

    def command(self, slave: int, address: int, value: int | bool) -> bytes:
        if address not in range(self.poll.start, self.poll.start + self.poll.count):
            raise ValueError("Address outside profile output range")
        if self.family == DeviceFamily.DIMMER:
            return write_dimmer(slave, address, value)
        return write_single_coil(slave, address, value)


PROFILES = {
    Model.P404: DeviceProfile(
        Model.P404,
        DeviceFamily.DIMMER,
        PollSpec(3, REGISTER_BASE, 4),
        ("group_a", "group_b"),
        (Platform.LIGHT,),
    ),
    Model.P210: DeviceProfile(
        Model.P210,
        DeviceFamily.DIMMER,
        PollSpec(3, REGISTER_BASE, 2),
        ("group_a",),
        (Platform.LIGHT,),
    ),
    Model.R410: DeviceProfile(
        Model.R410, DeviceFamily.RELAY, PollSpec(1, 0, 4), (), (Platform.SWITCH,)
    ),
}

# Common identity-register discovery and read-only family capabilities.  Kept here
# with all other product register knowledge; Scanner only orchestrates probes.
DISCOVERY = PollSpec(3, 0, 1)
FAMILY_PROBES = (
    (PollSpec(3, REGISTER_BASE, 2), DeviceFamily.DIMMER, (Model.P404, Model.P210)),
    (PollSpec(2, 0, 4), DeviceFamily.RELAY, (Model.R410,)),
)
