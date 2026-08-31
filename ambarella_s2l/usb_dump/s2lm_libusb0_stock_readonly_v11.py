"""Launch the S2Lm stock BLD through the native libusb-win32 API.

Unlike the previous launchers this profile uses PyUSB's libusb0 backend and
Windows' installed 64-bit libusb0.dll end-to-end.  The RAM BLD keeps its stock
NAND/USB initialization, while commands 0, 1 and 4 are disabled before launch.
"""

import struct

import usb.backend.libusb0
import usb.core
import usb.util

import s2lm_usbdiag_v6 as tool


LIBUSB0_DLL = r"C:\Windows\System32\libusb0.dll"


def libusb0_backend():
    backend = usb.backend.libusb0.get_backend(
        find_library=lambda _name: LIBUSB0_DLL
    )
    if backend is None:
        raise RuntimeError("Could not load %s" % LIBUSB0_DLL)
    return backend


class Libusb0Bootrom:
    """FastBootrom-compatible transport backed by native libusb0.dll."""

    def __init__(self, vid=tool.VID, pid=tool.PID_BOOTROM):
        self.dev = usb.core.find(
            idVendor=vid,
            idProduct=pid,
            backend=libusb0_backend(),
        )
        if self.dev is None:
            raise RuntimeError(
                "Ambarella BootROM %04X:%04X not found via libusb0."
                % (vid, pid)
            )

    def close(self):
        if self.dev is not None:
            try:
                usb.util.dispose_resources(self.dev)
            finally:
                self.dev = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def send_words(self, opcode, address, value):
        payload = struct.pack(
            "<III",
            opcode & 0xFFFFFFFF,
            address & 0xFFFFFFFF,
            value & 0xFFFFFFFF,
        )
        return int(
            self.dev.ctrl_transfer(
                tool.WRITE_TYPE,
                tool.ROM_REQUEST,
                0,
                0,
                payload,
                timeout=tool.USB_TIMEOUT_MS,
            )
        )

    def read32(self, address):
        data = self.dev.ctrl_transfer(
            tool.READ_TYPE,
            tool.ROM_REQUEST,
            (address >> 16) & 0xFFFF,
            address & 0xFFFF,
            4,
            timeout=tool.USB_TIMEOUT_MS,
        )
        raw = bytes(data)
        if len(raw) != 4:
            raise RuntimeError(
                "libusb0 read32(0x%08X): expected 4 bytes, got %d"
                % (address, len(raw))
            )
        return raw


tool.TOOL_VERSION = "11.0-libusb0-s2lm-stock-readonly"
tool.SOC_PROFILE = "S2Lm stock / native libusb-win32"
tool.backend = libusb0_backend
# The common launcher names its high-speed transport FastLibusbBootrom.
# Replacing only the historical FastBootrom alias leaves the upload loop on
# libusb-1.0, which cannot reliably drive a libusb-win32-bound device.
tool.FastLibusbBootrom = Libusb0Bootrom

# Keep the complete official S2Lm startup path.  The common patcher accepts an
# empty main patch and still applies the NAND media word and read-only command
# dispatcher.
tool.USB_DIAG_PATCH_OFFSET = 0
tool.USB_DIAG_STOCK_WORDS = []
tool.USB_DIAG_PATCH_WORDS = []
tool.USB_DIAG_DESCRIPTION = [
    "No main-function patch (official S2Lm startup retained).",
    "Transport: native libusb0.dll / libusb-win32 driver.",
]
tool.USB_DIAG_BYPASSES_MEDIA = False
tool.KEEP_USB_BOOT_STRAP = True


if __name__ == "__main__":
    tool.main()
