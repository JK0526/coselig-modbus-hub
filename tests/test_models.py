import pytest

from custom_components.coselig_modbus_hub.models import (
    Device,
    GroupMode,
    Model,
    brightness_to_raw,
    devices_from_options,
    kelvin_to_raw,
    raw_to_brightness,
    raw_to_kelvin,
)


@pytest.mark.parametrize(
    "a,b,count",
    [
        ("single", "single", 4),
        ("cct", "single", 3),
        ("single", "cct", 3),
        ("cct", "cct", 2),
    ],
)
def test_p404_profiles(a, b, count):
    device = Device(3, Model.P404, GroupMode(a), GroupMode(b))
    assert len(device.outputs) == count
    registers = [register for output in device.outputs for register in output.registers]
    assert sorted(registers) == list(range(0x082A, 0x082E))
    assert device.poll_count == 4
    assert Device.from_dict(device.as_dict()) == device
    assert all(
        output.unique_id("entry", 3).startswith("entry_slave_3_")
        for output in device.outputs
    )


def test_defaults_and_p210():
    assert [o.name for o in Device(1, Model.P404).outputs] == [
        "CH1",
        "CH2",
        "CH3",
        "CH4",
    ]
    p210 = Device(2, Model.P210)
    assert [o.name for o in p210.outputs] == ["CH1", "CH2"]
    assert p210.poll_count == 2
    assert p210.as_dict() == {"slave": 2, "model": "p210"}
    assert not any(output.cct for output in p210.outputs)


@pytest.mark.parametrize(
    "args",
    [
        (0, "p404"),
        (248, "p404"),
        (True, "p404"),
        (1, "U4"),
        (1, "p210", "single"),
        (1, "p404", "invalid"),
    ],
)
def test_invalid_devices(args):
    with pytest.raises(ValueError):
        Device(*args)


def test_duplicate_devices():
    with pytest.raises(ValueError):
        devices_from_options({"devices": [{"slave": 1, "model": "p404"}] * 2})


@pytest.mark.parametrize("value", [0, 1, 5, 64, 128, 192, 255])
def test_brightness(value):
    raw = brightness_to_raw(value)
    assert raw == 0 if value == 0 else raw >= 2
    if value >= 5:
        assert abs(raw_to_brightness(raw) - value) <= 2


@pytest.mark.parametrize("value", [-1, 256, True, 1.1])
def test_invalid_brightness(value):
    with pytest.raises(ValueError):
        brightness_to_raw(value)


@pytest.mark.parametrize("value", [-1, 101])
def test_invalid_raw(value):
    with pytest.raises(ValueError):
        raw_to_brightness(value)
    with pytest.raises(ValueError):
        raw_to_kelvin(value)


@pytest.mark.parametrize("raw", list(range(101)))
def test_cct_round_trip(raw):
    assert abs(kelvin_to_raw(raw_to_kelvin(raw)) - raw) <= 1
