import pytest
from homeassistant.const import Platform

from custom_components.coselig_modbus_hub.models import Device, Model
from custom_components.coselig_modbus_hub.profiles import (
    PROFILES,
    ModbusData,
    encode_dimmer_value,
)
from custom_components.coselig_modbus_hub.protocol import ProtocolError, frame


@pytest.mark.parametrize(
    "model,mode,function,start,count",
    [
        (Model.P404, "single", 3, 0x082A, 4),
        (Model.P404, "cct", 3, 0x082A, 4),
        (Model.P210, "single", 3, 0x082A, 2),
        (Model.P210, "cct", 3, 0x082A, 2),
        (Model.R410, None, 1, 0, 4),
    ],
)
def test_profile_poll_identity(model, mode, function, start, count):
    device = Device(62, model, mode)
    specs = device.profile.poll_specs(device)
    assert len(specs) == 1
    assert (specs[0].function, specs[0].start, specs[0].count) == (
        function,
        start,
        count,
    )
    assert specs[0].request(62) == frame(
        bytes([62, function]) + start.to_bytes(2, "big") + count.to_bytes(2, "big")
    )


@pytest.mark.parametrize("raw,encoded", [(0, 0x0500), (49, 0x0531), (100, 0x0564)])
def test_dimmer_wire_codec(raw, encoded):
    assert encode_dimmer_value(raw) == encoded
    for model in (Model.P404, Model.P210):
        assert PROFILES[model].command(51, 0x082A, raw) == frame(
            bytes([51, 6, 8, 42]) + encoded.to_bytes(2, "big")
        )


def test_r410_config_and_outputs():
    device = Device.from_dict({"slave": 70, "model": "r410"})
    assert device.as_dict() == {"slave": 70, "model": "r410"}
    assert device.group_a is None and device.group_b is None
    assert device.profile.supported_platforms == (Platform.SWITCH,)
    assert [(o.key, o.name, o.registers, o.platform) for o in device.outputs] == [
        (f"relay{i + 1}", f"Relay {i + 1}", (i,), Platform.SWITCH) for i in range(4)
    ]
    assert device.profile.command(70, 3, True) == frame(bytes.fromhex("46050003ff00"))


@pytest.mark.parametrize("a,b", [("single", None), ("cct", None), (None, "single")])
def test_r410_rejects_groups(a, b):
    with pytest.raises(ValueError):
        Device(70, Model.R410, a, b)


@pytest.mark.parametrize(
    "model,address", [(Model.P404, 0), (Model.P210, 0x082C), (Model.R410, 4)]
)
def test_profile_rejects_wrong_output_address(model, address):
    with pytest.raises(ValueError):
        PROFILES[model].command(1, address, 0)


def test_profile_dimmer_low_byte_validation():
    profile = PROFILES[Model.P210]
    data = ModbusData()
    profile.decode(profile.poll, frame(bytes.fromhex("01030405000564")), data)
    assert data.holding_registers == {0x082A: 0, 0x082B: 100}
    with pytest.raises(ProtocolError):
        profile.decode(
            profile.poll, frame(bytes.fromhex("01030400650000")), ModbusData()
        )
