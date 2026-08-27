import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).parents[1]
    / "gigaset_elements_camera"
    / "app"
    / "migrate_camera_config.py"
)
SPEC = importlib.util.spec_from_file_location("migrate_camera_config", MODULE_PATH)
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


class AddonConfigMigrationTests(unittest.TestCase):
    def test_migrates_legacy_single_camera_once(self):
        options = {
            "camera_ip": "192.0.2.10",
            "camera_mac": "02:00:00:00:00:01",
            "camera_name": "Front door",
            "camera_user": "admin",
            "camera_password": "secret",
            "http_token": "token",
        }
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "options.json"
            target = Path(folder) / "cameras.json"
            source.write_text(json.dumps(options), encoding="utf-8")
            self.assertTrue(MIGRATION.migrate(source, target))
            stored = json.loads(target.read_text(encoding="utf-8"))["cameras"]
            self.assertEqual(stored[0]["name"], "Front door")
            self.assertEqual(stored[0]["password"], "secret")
            target.write_text('{"cameras": []}\n', encoding="utf-8")
            self.assertFalse(MIGRATION.migrate(source, target))
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8")), {"cameras": []}
            )


if __name__ == "__main__":
    unittest.main()
