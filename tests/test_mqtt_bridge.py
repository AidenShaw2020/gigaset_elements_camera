import json
import tempfile
import types
import unittest
from pathlib import Path

from mqtt_bridge import (
    CAMERA_EDITOR_HTML,
    CameraOptions,
    derive_s2l_password,
    discovery_messages,
    load_camera_mappings,
    load_camera_options,
    normalize_camera_mappings,
    parse_duration,
    parse_stream_info,
    parse_system_info,
    slugify,
    save_camera_mappings,
)


class MqttBridgeTests(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(slugify("Hall Camera 7C:2F"), "hall_camera_7c_2f")

    def test_ambarella_s2l_password_and_options(self):
        self.assertEqual(
            derive_s2l_password("00:E0:4C:B1:6F:13"), "LQ6PgaUWMeRJ"
        )
        camera = CameraOptions(
            camera="192.0.2.20",
            mac="00:E0:4C:B1:6F:13",
            model="s2l",
            stream="video1",
        )
        self.assertTrue(camera.is_s2l)
        self.assertEqual(camera.camera_password, "LQ6PgaUWMeRJ")
        self.assertEqual(
            camera.rtsp_url,
            "rtsp://admin:LQ6PgaUWMeRJ@192.0.2.20/video1",
        )

    def test_home_assistant_discovery(self):
        messages = discovery_messages(
            "homeassistant", "gigaset/hall", "gigaset_hall", "Hall Camera"
        )
        self.assertGreaterEqual(len(messages), 10)
        topics = [topic for topic, _ in messages]
        self.assertIn(
            "homeassistant/binary_sensor/gigaset_hall/motion/config", topics
        )
        self.assertIn("homeassistant/camera/gigaset_hall/snapshot/config", topics)
        self.assertIn(
            "homeassistant/button/gigaset_hall/refresh_snapshot/config", topics
        )
        self.assertIn(
            "homeassistant/sensor/gigaset_hall/system_uptime/config", topics
        )
        motion = messages[0][1]
        self.assertEqual(motion["state_topic"], "gigaset/hall/motion")
        self.assertEqual(motion["device_class"], "motion")
        self.assertEqual(motion["availability_mode"], "all")
        json.dumps(motion)

    def test_parses_camera_telemetry(self):
        page = """
        <tr><td>Model:</td><td>Gigaset-camera</td></tr>
        <tr><td>System up time:</td><td>2 Days 01:02:03</td></tr>
        <tr><td>Total up time:</td><td>19 days 5 hours 44 mins</td></tr>
        <tr><td>Firmware version:</td><td>1.10 (build 20140802)</td></tr>
        <tr><td>Status:</td><td>No connection</td></tr>
        <tr><td>Status:</td><td>connected</td></tr>
        """
        values = parse_system_info(page)
        self.assertEqual(values["firmware_version"], "1.10 (build 20140802)")
        self.assertEqual(values["system_uptime_seconds"], 176523)
        self.assertEqual(values["total_uptime_seconds"], 1662240)
        self.assertEqual(values["ethernet_status"], "connected")
        self.assertEqual(parse_duration("0 Days 00:09:45"), 585)

    def test_parses_stream_settings(self):
        page = """
        if (cf.SIZE0.options[i].value == "fsize") {}
        if (cf.FRAMERATE0.options[i].value == "15") {}
        if (cf.H264BITRATE0.options[i].value == "512") {}
        """
        values = parse_stream_info(page)
        self.assertEqual(values["primary_resolution"], "1280x720")
        self.assertEqual(values["primary_fps"], 15)
        self.assertEqual(values["primary_h264_kbps"], 512)

    def test_loads_multiple_cameras(self):
        options = {
            "cameras": [
                {"name": "Front", "ip": "192.0.2.10", "mac": "02:00:00:00:00:01"},
                {"name": "Garage", "ip": "192.0.2.11", "mac": "02:00:00:00:00:02"},
            ],
            "motion_hold": 20,
            "snapshot_interval": 5,
            "telemetry_interval": 30,
            "mqtt_topic": "gigaset/camera",
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "options.json"
            path.write_text(json.dumps(options), encoding="utf-8")
            args = types.SimpleNamespace(
                config=str(path),
                motion_hold=15,
                snapshot_interval=10,
                telemetry_interval=60,
                topic="gigaset/camera",
            )
            cameras = load_camera_options(args)
        self.assertEqual(len(cameras), 2)
        self.assertNotEqual(cameras[0].key, cameras[1].key)
        self.assertEqual(args.snapshot_interval, 5)

    def test_visual_editor_camera_storage(self):
        values = [
            {
                "name": "Front door",
                "ip": "192.0.2.10",
                "mac": "020000000001",
                "user": "admin",
                "password": "",
                "token": "local-token",
                "model": "ambarella_s2l",
                "stream": "video0",
            }
        ]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cameras.json"
            stored = save_camera_mappings(str(path), values)
            loaded = load_camera_mappings(str(path))
        self.assertEqual(stored, loaded)
        self.assertEqual(loaded[0]["mac"], "02:00:00:00:00:01")
        self.assertEqual(loaded[0]["password"], "")
        self.assertEqual(loaded[0]["model"], "ambarella_s2l")
        self.assertIn("Přidat kameru", CAMERA_EDITOR_HTML)
        self.assertIn("Novější / Ambarella S2L", CAMERA_EDITOR_HTML)

    def test_visual_editor_rejects_duplicate_camera_ids(self):
        values = [
            {"name": "Camera", "ip": "192.0.2.10", "mac": "02:00:00:00:00:01"},
            {"name": "Camera", "ip": "192.0.2.11", "mac": "02:00:00:00:00:01"},
        ]
        with self.assertRaisesRegex(ValueError, "unique IDs"):
            normalize_camera_mappings(values)


if __name__ == "__main__":
    unittest.main()
