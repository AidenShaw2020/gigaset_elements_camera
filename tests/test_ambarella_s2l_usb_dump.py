import sys
import unittest
from pathlib import Path


USB_DUMP = Path(__file__).parents[1] / "ambarella_s2l" / "usb_dump"
sys.path.insert(0, str(USB_DUMP))

import s2lm_libusb0_fulldump_v14 as full_dump  # noqa: E402
import s2lm_libusb0_rawnand_bld_v14 as raw_bld  # noqa: E402
import s2lm_libusb0_rootfs_writer_v17 as rootfs_writer  # noqa: E402


class AmbarellaS2LUsbDumpTests(unittest.TestCase):
    def test_dump_is_bounded_to_validated_capacity(self):
        self.assertEqual(full_dump.DEFAULT_SIZE, 128 * 1024 * 1024)
        self.assertLessEqual(full_dump.DEFAULT_CHUNK, full_dump.MAX_CHUNK)

    def test_write_capable_dispatch_entries_are_disabled(self):
        tool = raw_bld.tool
        expected = [
            tool.UNKNOWN_COMMAND_HANDLER,
            tool.UNKNOWN_COMMAND_HANDLER,
            tool.ORIGINAL_DISPATCH[2],
            tool.ORIGINAL_DISPATCH[3],
            tool.UNKNOWN_COMMAND_HANDLER,
        ]
        self.assertEqual(expected, [0xE770, 0xE770, 0xE720, 0xE650, 0xE770])

    def test_raw_get_patch_uses_nand_read_and_normal_send_path(self):
        self.assertEqual(raw_bld.RAW_GET_PATCH_OFFSET, 0xE1AC)
        self.assertEqual(raw_bld.RAW_GET_PATCH_WORDS[4], 0xEBFFF62E)
        self.assertEqual(raw_bld.RAW_GET_PATCH_WORDS[-1], 0xEAFFFFD4)
        self.assertEqual(raw_bld.tool.CMD_GET, 2)
        self.assertEqual(raw_bld.tool.CMD_SEND, 3)

    def test_rootfs_package_avoids_stock_usb_command_buffer(self):
        self.assertEqual(
            rootfs_writer.IMAGE_ADDRESS,
            rootfs_writer.TRANSFER_ADDRESS + rootfs_writer.TRANSFER_PREFIX,
        )
        self.assertGreaterEqual(rootfs_writer.TRANSFER_PREFIX, 32)
        largest_package = rootfs_writer.PARTITION_SIZE + 256
        self.assertLess(
            rootfs_writer.IMAGE_ADDRESS + largest_package,
            rootfs_writer.WRAPPER_ADDRESS,
        )

    def test_camera_rootfs_layout_replaces_generic_bld_layout(self):
        self.assertEqual(rootfs_writer.PARTITION_OFFSET, 0x012A0000)
        self.assertEqual(rootfs_writer.PARTITION_SIZE, 0x04200000)
        self.assertEqual(rootfs_writer.GENERIC_LNX_START_BLOCK, 821)
        self.assertEqual(rootfs_writer.GENERIC_LNX_BLOCKS, 1024)
        self.assertEqual(rootfs_writer.CAMERA_LNX_START_BLOCK, 149)
        self.assertEqual(rootfs_writer.CAMERA_LNX_BLOCKS, 528)

    def test_camera_rootfs_program_payload_is_contiguous(self):
        payload = b"A" * (2 * rootfs_writer.ERASEBLOCK)
        self.assertEqual(rootfs_writer.prepare_program_payload(payload), payload)


if __name__ == "__main__":
    unittest.main()
