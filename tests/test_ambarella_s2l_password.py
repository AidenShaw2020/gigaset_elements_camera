import io
import unittest
from contextlib import redirect_stdout

from ambarella_s2l.password import (
    derive_stock_password,
    htdigest_value,
    main,
    normalize_mac,
)


class AmbarellaS2LPasswordTests(unittest.TestCase):
    def test_normalizes_common_mac_formats(self):
        self.assertEqual(normalize_mac("00:11:22:33:44:55"), "001122334455")
        self.assertEqual(normalize_mac("00-11-22-33-44-55"), "001122334455")
        self.assertEqual(normalize_mac("0011.2233.4455"), "001122334455")

    def test_rejects_invalid_mac(self):
        with self.assertRaises(ValueError):
            normalize_mac("00:11:22:33:44")
        with self.assertRaises(ValueError):
            normalize_mac("00:11:22:33:44:GG")

    def test_reference_vectors(self):
        # Vectors generated from the independently translated Thumb-2 byte
        # operations and retained to detect accidental algorithm changes.
        self.assertEqual(derive_stock_password("00:11:22:33:44:55"), "1ANJ6jcDIbj4")
        self.assertEqual(derive_stock_password("00:E0:4C:B1:6F:13"), "LQ6PgaUWMeRJ")

    def test_case_is_significant_and_upper_is_stock_default(self):
        self.assertEqual(derive_stock_password("00:e0:4c:b1:6f:13"), "LQ6PgaUWMeRJ")
        self.assertEqual(
            derive_stock_password("00:e0:4c:b1:6f:13", letter_case="lower"),
            "5rsnkbnwokjl",
        )

    def test_http_digest(self):
        self.assertEqual(
            htdigest_value("1ANJ6jcDIbj4"),
            "c0fb5c03ada1016c65a08c21fed2ffc7",
        )

    def test_cli_prints_admin_and_root_credentials(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["00:11:22:33:44:55"]), 0)
        text = output.getvalue()
        self.assertIn("Web password: 1ANJ6jcDIbj4", text)
        self.assertIn("Root password: 1ANJ6jcDIbj4", text)


if __name__ == "__main__":
    unittest.main()
