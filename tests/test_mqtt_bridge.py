import json
import unittest

from mqtt_bridge import discovery_messages, slugify


class MqttBridgeTests(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(slugify("Hall Camera 7C:2F"), "hall_camera_7c_2f")

    def test_home_assistant_discovery(self):
        messages = discovery_messages(
            "homeassistant", "gigaset/hall", "gigaset_hall", "Hall Camera"
        )
        self.assertEqual(len(messages), 2)
        topics = [topic for topic, _ in messages]
        self.assertIn(
            "homeassistant/binary_sensor/gigaset_hall/motion/config", topics
        )
        self.assertIn("homeassistant/camera/gigaset_hall/snapshot/config", topics)
        motion = messages[0][1]
        self.assertEqual(motion["state_topic"], "gigaset/hall/motion")
        self.assertEqual(motion["device_class"], "motion")
        json.dumps(motion)


if __name__ == "__main__":
    unittest.main()
