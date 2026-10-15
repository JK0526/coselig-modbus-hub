"""R410 Cover validation and FC06 momentary request construction."""

from copy import deepcopy
from uuid import uuid4

import pytest
from homeassistant.components.cover import CoverDeviceClass

from custom_components.coselig_modbus_hub.models import Device, Model
from custom_components.coselig_modbus_hub.protocol import (
    validate,
    write_single_register,
)
from custom_components.coselig_modbus_hub.r410_cover import (
    MOMENTARY_REGISTER,
    R410CoverConfigError,
    R410CoverMode,
    expected_cover_unique_ids,
    expected_physical_output_unique_ids,
    occupied_relays,
    parse_r410_cover,
    r410_covers_from_options,
    unique_cover_id,
)


def raw_cover(**values):
    return {
        "id": uuid4().hex,
        "slave": 63,
        "name": "Garage curtain",
        "device_class": "garage",
        "mode": "two_relay",
        "open_relay": 1,
        "close_relay": 2,
        "pulse_tenths": 5,
        **values,
    }


def test_momentary_fc06_values_and_crc():
    cases = [
        (raw_cover(open_relay=1, close_relay=2), "open", 0x0501),
        (raw_cover(open_relay=2, close_relay=1), "open", 0x0502),
        (raw_cover(open_relay=1, close_relay=2), "stop", 0x0503),
        (raw_cover(open_relay=4, close_relay=1), "open", 0x0508),
        (raw_cover(open_relay=4, close_relay=1), "stop", 0x0509),
        (
            raw_cover(
                mode="three_relay",
                open_relay=3,
                stop_relay=1,
                close_relay=4,
                pulse_tenths=10,
            ),
            "open",
            0x0A04,
        ),
    ]
    for raw, action, value in cases:
        request = parse_r410_cover(raw).request(action)
        assert request == write_single_register(63, MOMENTARY_REGISTER, value)
        assert validate(request, request) == request


def test_three_relay_mapping_and_two_relay_free_mapping():
    two = parse_r410_cover(raw_cover(open_relay=4, close_relay=1))
    assert two.action_mask("open") == 0x08
    assert two.action_mask("close") == 0x01
    assert two.action_mask("stop") == 0x09
    three = parse_r410_cover(
        raw_cover(
            mode="three_relay",
            open_relay=3,
            stop_relay=1,
            close_relay=4,
        )
    )
    assert [three.action_mask(action) for action in ("open", "stop", "close")] == [
        0x04,
        0x01,
        0x08,
    ]
    with pytest.raises(ValueError, match="Unknown R410 Cover action"):
        two.request("toggle")


@pytest.mark.parametrize(
    "changes",
    [
        {"slave": True},
        {"slave": 0},
        {"name": "  "},
        {"mode": "unknown"},
        {"open_relay": 0},
        {"open_relay": 5},
        {"open_relay": True},
        {"close_relay": 1},
        {"pulse_tenths": 0},
        {"pulse_tenths": 251},
        {"pulse_tenths": 5.0},
        {"device_class": "not_a_cover_class"},
        {"id": "not-a-uuid"},
        {"stop_relay": 3},
    ],
)
def test_invalid_cover_record_fails_closed(changes):
    with pytest.raises(R410CoverConfigError):
        parse_r410_cover(raw_cover(**changes))


def test_cover_parser_rejects_non_object_record():
    with pytest.raises(R410CoverConfigError, match="must be an object"):
        parse_r410_cover(["not", "a", "cover"])


def test_three_relay_requires_distinct_stop_relay():
    missing_stop = raw_cover(mode="three_relay")
    with pytest.raises(R410CoverConfigError):
        parse_r410_cover(missing_stop)
    with pytest.raises(R410CoverConfigError):
        parse_r410_cover(raw_cover(mode="three_relay", stop_relay=1))
    with pytest.raises(R410CoverConfigError):
        parse_r410_cover(
            raw_cover(mode="three_relay", open_relay=3, stop_relay=3, close_relay=4)
        )


def test_cover_class_none_name_trim_and_identity():
    cover = parse_r410_cover(
        raw_cover(name="  車庫  ", device_class=None, open_relay=4, close_relay=1)
    )
    assert cover.name == "車庫"
    assert cover.device_class is None
    assert cover.unique_id("entry") == f"entry_slave_63_cover_{cover.id}"
    assert cover.as_dict()["mode"] == R410CoverMode.TWO_RELAY
    assert CoverDeviceClass.GARAGE.value == "garage"


def test_missing_covers_and_cover_ids_are_immutable_uuid_values():
    devices = (Device(63, Model.R410),)
    assert r410_covers_from_options({}, devices) == ()
    assert len(unique_cover_id()) == 32
    assert unique_cover_id() != unique_cover_id()


def test_config_parser_rejects_duplicate_ids_or_wrong_devices():
    item = raw_cover()
    with pytest.raises(R410CoverConfigError, match="Duplicate"):
        r410_covers_from_options(
            {"r410_covers": [item, deepcopy(item)]}, (Device(63, Model.R410),)
        )
    with pytest.raises(R410CoverConfigError, match="configured R410"):
        r410_covers_from_options({"r410_covers": [item]}, ())
    with pytest.raises(R410CoverConfigError, match="configured R410"):
        r410_covers_from_options({"r410_covers": [item]}, (Device(63, Model.P404),))
    with pytest.raises(R410CoverConfigError):
        r410_covers_from_options({"r410_covers": {}}, (Device(63, Model.R410),))
    missing_slave = raw_cover()
    del missing_slave["slave"]
    with pytest.raises(R410CoverConfigError):
        r410_covers_from_options(
            {"r410_covers": [missing_slave]}, (Device(63, Model.R410),)
        )


def test_cover_resource_validation_is_per_r410_slave():
    a = raw_cover(open_relay=1, close_relay=2)
    b = raw_cover(open_relay=2, close_relay=1)
    with pytest.raises(R410CoverConfigError, match="already assigned"):
        r410_covers_from_options({"r410_covers": [a, b]}, (Device(63, Model.R410),))
    b["slave"] = 64
    covers = r410_covers_from_options(
        {"r410_covers": [a, b]}, (Device(63, Model.R410), Device(64, Model.R410))
    )
    assert occupied_relays(covers, 63) == frozenset({1, 2})
    assert occupied_relays(covers, 64) == frozenset({1, 2})
    assert expected_cover_unique_ids("entry", covers) == {
        f"entry_slave_{cover.slave}_cover_{cover.id}" for cover in covers
    }
    # The physical Relay identities remain in the registry expectation while occupied.
    assert expected_physical_output_unique_ids("entry", (Device(63, Model.R410),)) == {
        f"entry_slave_63_relay{i}" for i in range(1, 5)
    }
