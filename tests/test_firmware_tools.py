import base64
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "firmware_tools"))

import patch_oncamera_manager


class FirmwareToolTests(unittest.TestCase):
    def test_mac_derived_password(self):
        actual = patch_oncamera_manager.default_admin_password("00:11:22:33:44:55")
        expected = base64.b64encode(b"LUCKOTVF554433221100YCAMVF")
        self.assertEqual(actual, expected)

    def test_invalid_mac_is_rejected(self):
        with self.assertRaises(ValueError):
            patch_oncamera_manager.default_admin_password("not-a-mac")

    def test_credential_summary_shows_admin_password(self):
        summary = patch_oncamera_manager.credential_summary("00-11-22-33-44-55")
        expected = base64.b64encode(b"LUCKOTVF554433221100YCAMVF").decode()
        self.assertIn("MAC address:    00:11:22:33:44:55", summary)
        self.assertIn("Web user:       admin", summary)
        self.assertIn(f"Web password:   {expected}", summary)
        self.assertIn("Root password:  root", summary)

    def test_no_device_password_placeholder_leaks(self):
        self.assertIn(b"@ADMIN_PASSWORD@", patch_oncamera_manager.INIT_SCRIPT)
        generated = patch_oncamera_manager.INIT_SCRIPT.replace(
            b"@ADMIN_PASSWORD@",
            patch_oncamera_manager.default_admin_password("00:11:22:33:44:55"),
        )
        self.assertNotIn(b"@ADMIN_PASSWORD@", generated)


if __name__ == "__main__":
    unittest.main()
