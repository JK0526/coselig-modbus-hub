"""S402 packet parsing and option validation remain side-effect free."""

from unittest.mock import Mock

import pytest

from custom_components.coselig_modbus_hub.event import S402DIEvent
from custom_components.coselig_modbus_hub.hub import Hub
from custom_components.coselig_modbus_hub.protocol import frame
from custom_components.coselig_modbus_hub.s402 import (
    S402Event,
    is_s402_candidate,
    parse_s402_event,
    s402_devices_from_options,
)


@pytest.mark.parametrize(
    "device_id,di_bit,di",
    [(3, 0x01, 1), (7, 0x02, 2), (12, 0x04, 3), (255, 0x08, 4), (0, 0x01, 1)],
)
def test_parse_valid_s402_frames(device_id, di_bit, di):
    packet = frame(bytes([1, 6, 1, 0xA0, device_id, di_bit]))
    assert is_s402_candidate(packet)
    assert parse_s402_event(packet) == S402Event(device_id, di)


@pytest.mark.parametrize(
    "packet",
    [
        frame(bytes([2, 6, 1, 0xA0, 7, 1])),
        frame(bytes([1, 5, 1, 0xA0, 7, 1])),
        frame(bytes([1, 6, 1, 0xA1, 7, 1])),
        frame(bytes([1, 6, 1, 0xA0, 7, 0x03])),
        frame(bytes([1, 6, 1, 0xA0, 7, 0x10])),
        frame(bytes([1, 6, 1, 0xA0, 7, 0x20])),
        b"\x01\x06\x01\xa0\x07\x01\x00\x00",
        frame(bytes([1, 6, 1, 0xA0, 7, 1]))[:-1],
        frame(bytes([1, 6, 1, 0xA0, 7, 1])) + b"\x00",
    ],
)
def test_invalid_s402_frames_are_not_parsed(packet):
    assert parse_s402_event(packet) is None


def test_signature_classifies_invalid_candidates_for_consumption():
    assert is_s402_candidate(b"\x01\x06\x01\xa0")
    assert not is_s402_candidate(b"\x01\x05\x01\xa0\x07\x01\x00\x00")


@pytest.mark.parametrize(
    "options,expected",
    [
        ({}, ()),
        ({"s402_devices": []}, ()),
        ({"s402_devices": [{"id": 0}, {"id": 255}]}, (0, 255)),
    ],
)
def test_s402_options_default_and_valid_range(options, expected):
    assert s402_devices_from_options(options) == expected


def test_hub_dispatches_only_configured_id_and_unsubscribes_cleanly():
    hub = Hub(Mock(), Mock(), (7,))
    seen = []

    def first_listener(event):
        seen.append(event)

    def second_listener(event):
        seen.append(event)

    first_unsubscribe = hub.subscribe_s402(7, 1, first_listener)
    second_unsubscribe = hub.subscribe_s402(7, 1, second_listener)
    with pytest.raises(ValueError, match="not configured"):
        hub.subscribe_s402(8, 1, seen.append)

    hub._dispatch_s402_event(S402Event(8, 1))
    hub._dispatch_s402_event(S402Event(7, 1))
    assert seen == [S402Event(7, 1), S402Event(7, 1)]
    first_unsubscribe()
    assert (7, 1) in hub._s402_listeners
    hub._dispatch_s402_event(S402Event(7, 1))
    assert len(seen) == 3
    second_unsubscribe()
    assert (7, 1) not in hub._s402_listeners
    second_unsubscribe()


def test_event_entity_ignores_nonmatching_event():
    hub = Mock()
    hub.coordinator = Mock()
    entity = S402DIEvent(hub, "entry", 7, 1)
    trigger = Mock()
    write_state = Mock()
    entity._trigger_event = trigger
    entity.async_write_ha_state = write_state

    entity._handle_event(S402Event(8, 1))

    trigger.assert_not_called()
    write_state.assert_not_called()


@pytest.mark.parametrize(
    "devices",
    [
        [{"id": -1}],
        [{"id": 256}],
        [{"id": True}],
        [{"id": "7"}],
        [{"id": 7}, {"id": 7}],
        [7],
        None,
    ],
)
def test_invalid_s402_options_are_rejected(devices):
    with pytest.raises(ValueError):
        s402_devices_from_options({"s402_devices": devices})
