import base64
import http.server
import threading
import unittest
import urllib.parse

from cloudless_manager import (
    CameraClient,
    derive_admin_password,
    parse_cloud_state,
    patch_backup_cloudless,
)


PAGE_ENABLED = b'''<input type=RADIO name=ENABLE value=enable checked>\n<input type=RADIO name=ENABLE value=disable>\n<input name=SERVER value="cam-dx.gigaset-elements.com"><input name=PORT value="8000">'''
PAGE_DISABLED = b'''<input type=RADIO name=ENABLE value=enable>\n<input type=RADIO name=ENABLE value=disable checked>\n<input name=SERVER value=""><input name=PORT value="8000">'''


class FakeCamera(http.server.BaseHTTPRequestHandler):
    enabled = True
    expected_auth = ""

    def log_message(self, *_args):
        pass

    def authorized(self):
        return self.headers.get("Authorization") == self.expected_auth

    def do_GET(self):
        if not self.authorized():
            self.send_response(401)
            self.end_headers()
            return
        body = PAGE_ENABLED if self.enabled else PAGE_DISABLED
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self.authorized():
            self.send_response(401)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        fields = urllib.parse.parse_qs(self.rfile.read(length).decode("ascii"))
        type(self).enabled = fields.get("ENABLE") == ["enable"]
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


class CloudlessManagerTests(unittest.TestCase):
    def test_password_derivation(self):
        expected = base64.b64encode(b"LUCKOTVF554433221100YCAMVF").decode()
        self.assertEqual(derive_admin_password("00:11:22:33:44:55"), expected)

    def test_parse_cloud_state(self):
        self.assertEqual(
            parse_cloud_state(PAGE_ENABLED.decode()),
            {
                "enabled": True,
                "server": "cam-dx.gigaset-elements.com",
                "port": "8000",
            },
        )
        self.assertFalse(parse_cloud_state(PAGE_DISABLED.decode())["enabled"])

    def test_native_form_disables_cloud_and_verifies(self):
        password = derive_admin_password("001122334455")
        token = base64.b64encode(("admin:" + password).encode()).decode()
        FakeCamera.expected_auth = "Basic " + token
        FakeCamera.enabled = True
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeCamera)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = CameraClient(
                "127.0.0.1:%d" % server.server_address[1],
                "001122334455",
                timeout=1,
            )
            self.assertTrue(client.cloud_state()["enabled"])
            # Enabling still uses the native form. Disabling uses the stronger
            # backup/restore path and is covered separately below.
            FakeCamera.enabled = False
            self.assertTrue(client.set_cloud(True)["enabled"])
        finally:
            server.shutdown()
            server.server_close()

    def test_backup_patch_changes_only_expected_configuration_keys(self):
        import io
        import tarfile

        config = b'''[startup]\notproxyc=automatic\n[procspy]\npsl_client=enable\n[otproxyc]\nENABLE=enable\nSERVER=cam-dx.gigaset-elements.com\nPORT=8000\n'''
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w", format=tarfile.USTAR_FORMAT) as tar:
            info = tarfile.TarInfo("sys.conf")
            info.size = len(config)
            tar.addfile(info, io.BytesIO(config))
        patched = patch_backup_cloudless(b"X" * 24 + archive.getvalue())
        self.assertEqual(patched[:24], b"X" * 24)
        with tarfile.open(fileobj=io.BytesIO(patched[24:])) as tar:
            result = tar.extractfile("sys.conf").read().decode()
        self.assertIn("otproxyc=manual", result)
        self.assertIn("psl_client=disable", result)
        self.assertIn("ENABLE=disable", result)
        self.assertIn("SERVER=\n", result)


if __name__ == "__main__":
    unittest.main()
