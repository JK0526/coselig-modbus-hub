import unittest

from custom_components.sunwave_modbus_hub.devices import (
    Channel,
    channel_from_mapping,
    validate_channels,
)


class DeviceModelTests(unittest.TestCase):
    def test_model_poll_shapes(self):
        p210 = Channel("p210", 2, "2", "single", "test p210")
        p404 = Channel("p404", 1, "a", "dual", "test dual")
        self.assertEqual(p210.poll_count, 2)
        self.assertEqual(p404.poll_count, 4)
        self.assertEqual(p404.brightness_command(2)[5], 2)

    def test_registry_rejects_overlap(self):
        dual = Channel("p404", 54, "a", "dual", "雙色溫")
        single = Channel("p404", 54, "1", "single", "重疊")
        with self.assertRaises(ValueError):
            validate_channels([dual, single])

    def test_mapping_defaults_are_validated(self):
        channel = channel_from_mapping(
            {"model": "U4", "slave": "61", "channel": "1", "kind": "single", "name": "門口"}
        )
        self.assertEqual(channel.minimum, 2)
        with self.assertRaises(ValueError):
            channel_from_mapping(
                {"model": "U4", "slave": 61, "channel": "2", "kind": "single", "name": "錯誤"}
            )
