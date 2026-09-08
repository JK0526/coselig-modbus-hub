import json
import unittest

from custom_components.sunwave_modbus_hub.mqtt_bridge import (
    availability_message,
    discovery_message,
)


CHANNEL = {
    "model": "p404",
    "slave": 1,
    "channel": "a",
    "kind": "dual",
    "name": "test dual",
    "minimum": 2,
    "mired_min": 175,
    "mired_max": 455,
}


class MqttBridgeTests(unittest.TestCase):
    def test_new_identity_and_payload(self):
        topic, raw = discovery_message(CHANNEL, "main")
        payload = json.loads(raw)
        self.assertEqual(topic, "homeassistant/light/sunwave_main_1_a/config")
        self.assertEqual(payload["unique_id"], "sunwave_main_1_a")
        self.assertFalse(payload["optimistic"])
        self.assertEqual(payload["brightness_command_topic"], "sunwave/main/light/1/a/set/brightness")
        self.assertEqual(payload["min_mireds"], 175)
        self.assertEqual(payload["max_mireds"], 455)
        self.assertNotIn("/set", payload["state_topic"])

    def test_availability_is_retained_by_caller(self):
        self.assertEqual(availability_message("main", True), ("sunwave/main/availability", "online"))
        self.assertEqual(availability_message("main", False), ("sunwave/main/availability", "offline"))


if __name__ == "__main__":
    unittest.main()
