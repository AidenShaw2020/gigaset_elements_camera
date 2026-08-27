import hashlib
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bootloader_dump


class FakeSerial:
    def __init__(self, data: bytes, read_size: int = 4096):
        self.data = bytearray(data)
        self.read_size = read_size

    def read(self, requested: int) -> bytes:
        if not self.data:
            return b""
        count = min(requested, self.read_size, len(self.data))
        result = bytes(self.data[:count])
        del self.data[:count]
        return result


class ReceiverTests(unittest.TestCase):
    def test_preserves_payload_bytes_after_start_marker(self):
        flash = bytearray(b"\xff" * bootloader_dump.FLASH_SIZE)
        flash[:8] = b"GM8126\0\0"
        flash[0x20000:0x20004] = b"MEF\x7f"
        stream = (
            b"MyLoader output before payload\r\n"
            + bootloader_dump.START_MARKER
            + flash
            + bootloader_dump.END_MARKER
        )
        serial = FakeSerial(stream)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "flash.bin"
            digest = bootloader_dump.receive_dump(serial, output, timeout=1)
            self.assertEqual(output.read_bytes(), flash)
            self.assertEqual(digest, hashlib.sha256(flash).hexdigest())


if __name__ == "__main__":
    unittest.main()
