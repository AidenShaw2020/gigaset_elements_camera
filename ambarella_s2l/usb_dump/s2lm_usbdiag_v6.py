import argparse
import ctypes
import hashlib
import struct
import sys
import time
import zipfile
import zlib

import libusb_package
import usb.core
import usb.util


VID = 0x4255
PID_BOOTROM = 0x000A
PID_BLD = 0x0001
TOOL_VERSION = "6.0-sparse-verify"
SOC_PROFILE = "S2Lm"

USB_TIMEOUT_MS = 5000

READ_TYPE = 0xC0
WRITE_TYPE = 0x40
ROM_REQUEST = 0x00
WRITE_OPCODE = 0x30000000
CALL_OPCODE = 0x00010000

BLD_SUFFIX = "platform/s2l/s2lm_bld_nand.bin"
BLD_SHA256 = "17783c311888f690f585bfa8512483a342cb07c702946221b99566f95d76bcef"

# Raw BLD is linked at address 0.
BLD_LOAD_ADDRESS = 0x00000000
BLD_ENTRY_ADDRESS = 0x00000000

# AmbaUSB 3.4.8 does one mandatory runtime patch while uploading
# a Boot Loader image. At file offset 0x3C the stock BLD contains
# zero; AmbaUSB replaces it with the selected boot media.
#
# Values recovered from ambausb.exe:
#   1 = NAND
#   4 = EMMC
#   8 = SPI NOR
#
# This camera uses NAND. Without this patch the BLD derives media
# from the current power-on straps, which are USB boot straps.
BLD_BOOT_MEDIA_OFFSET = 0x0000003C
BLD_BOOT_MEDIA_STOCK = 0x00000000
BLD_BOOT_MEDIA_NAND = 0x00000001

# Diagnostic main patch for the exact S2Lm BLD from AmbaUSB 3.4.8.
#
# Stock code at 0x80E0 first decides/initializes boot-media related
# state and only later enters the USB download function at 0xF188.
# For this diagnostic we bypass that path and call the known USB
# download function directly with argument 2:
#
#   0x80E0  MOV r0,#2
#   0x80E4  BL  0xF188
#   0x80E8  B   0x8128   ; spin if USB task ever returns
#
# This is RAM-only and deliberately avoids NAND/media initialization.
USB_DIAG_PATCH_OFFSET = 0x000080E0
USB_DIAG_STOCK_WORDS = [
    0xE59F306C,
    0xE5930000,
    0xE3500000,
]
USB_DIAG_PATCH_WORDS = [
    0xE3A00002,
    0xEB001C27,
    0xEA00000E,
]
USB_DOWNLOAD_ENTRY = 0x0000F188
USB_DIAG_SPIN_ADDRESS = 0x00008128
USB_DESCRIPTOR_OFFSET = 0x00015708
USB_DIAG_DESCRIPTION = [
    "0x80E0: MOV r0,#2",
    "0x80E4: BL  0xF188  (enter USB download mode)",
    "0x80E8: B   0x8128",
]
USB_DIAG_BYPASSES_MEDIA = True
# Some boards sample the USB boot strap again during BLD USB bring-up.  Keep
# this configurable so diagnostic wrappers can explicitly test that path.
KEEP_USB_BOOT_STRAP = False

# Command dispatch table inside this exact stock S2Lm BLD.
# Original handlers:
#   cmd 0 -> 0x0000E764
#   cmd 1 -> 0x0000E72C
#   cmd 2 -> 0x0000E720  GET
#   cmd 3 -> 0x0000E650  SEND
#   cmd 4 -> 0x0000E5FC
#
# 0x0000E770 is the BLD's own "Unknown command" handler.
DISPATCH_TABLE_OFFSET = 0x0000E568
ORIGINAL_DISPATCH = [
    0x0000E764,
    0x0000E72C,
    0x0000E720,
    0x0000E650,
    0x0000E5FC,
]
UNKNOWN_COMMAND_HANDLER = 0x0000E770

# Additional sanity checks proving that 0xE770 really is the
# "Unknown command" handler in this exact binary.
UNKNOWN_HANDLER_FIRST_WORD = 0xE59F00C8
UNKNOWN_HANDLER_LITERAL_OFFSET = 0x0000E840
UNKNOWN_COMMAND_STRING_OFFSET = 0x000152C4
UNKNOWN_COMMAND_STRING = b"Unknown command\x00"

# Expected DDR state after s2lm_ddr_init.py --apply.
DDR_EXPECTED = {
    0xEC1700DC: 0x1B100100,
    0xDFFE0800: 0x0000000F,
    0xDFFE0804: 0x08270974,
    0xDFFE0808: 0x10A3CCD4,
    0xDFFE0810: 0x504470A3,
    0xDFFE0828: 0x00000026,
}

# Amboot/BLD USB protocol recovered from AmbaUSB 3.4.8.
UCMD_MAGIC = 0x55434D44
URSP_MAGIC = 0x55525350
CMD_GET = 2
CMD_SEND = 3
GET_PTB = 16
PTB_SIZE = 0x1000


def backend():
    b = libusb_package.get_libusb1_backend()
    if b is None:
        raise RuntimeError("Could not load libusb backend.")
    return b


def find_device(pid):
    return usb.core.find(
        idVendor=VID,
        idProduct=pid,
        backend=backend(),
    )


def safe_string(dev, index):
    if not index:
        return ""
    try:
        return usb.util.get_string(dev, index)
    except Exception:
        return "?"


def show_device(dev):
    print("Device:       %04X:%04X" % (dev.idVendor, dev.idProduct))
    print("Manufacturer: %s" % safe_string(dev, dev.iManufacturer))
    print("Product:      %s" % safe_string(dev, dev.iProduct))
    print("Serial:       %s" % safe_string(dev, dev.iSerialNumber))


def read32(dev, address):
    data = dev.ctrl_transfer(
        READ_TYPE,
        ROM_REQUEST,
        (address >> 16) & 0xFFFF,
        address & 0xFFFF,
        4,
        timeout=USB_TIMEOUT_MS,
    )
    raw = bytes(data)
    if len(raw) != 4:
        raise RuntimeError(
            "read32(0x%08X): expected 4 bytes, got %d"
            % (address, len(raw))
        )
    return struct.unpack("<I", raw)[0]


def write32(dev, address, value):
    payload = struct.pack(
        "<III",
        WRITE_OPCODE,
        address & 0xFFFFFFFF,
        value & 0xFFFFFFFF,
    )
    sent = dev.ctrl_transfer(
        WRITE_TYPE,
        ROM_REQUEST,
        0,
        0,
        payload,
        timeout=USB_TIMEOUT_MS,
    )
    if sent != len(payload):
        raise RuntimeError(
            "write32(0x%08X): sent %d of %d bytes"
            % (address, sent, len(payload))
        )


def call_address(dev, address):
    payload = struct.pack(
        "<III",
        CALL_OPCODE,
        address & 0xFFFFFFFF,
        0,
    )
    return dev.ctrl_transfer(
        WRITE_TYPE,
        ROM_REQUEST,
        0,
        0,
        payload,
        timeout=USB_TIMEOUT_MS,
    )


def find_bld_member(zf):
    candidates = [
        name
        for name in zf.namelist()
        if name.replace("\\", "/").lower().endswith(BLD_SUFFIX.lower())
    ]
    if len(candidates) != 1:
        raise RuntimeError(
            "Expected exactly one %s in archive, found %d"
            % (BLD_SUFFIX, len(candidates))
        )
    return candidates[0]


def load_and_patch_bld(zip_path):
    with zipfile.ZipFile(zip_path, "r") as zf:
        member = find_bld_member(zf)
        original = zf.read(member)

    digest = hashlib.sha256(original).hexdigest()
    if digest.lower() != BLD_SHA256.lower():
        raise RuntimeError(
            "Unexpected stock BLD SHA256.\n"
            "Expected: %s\n"
            "Actual:   %s"
            % (BLD_SHA256, digest)
        )

    b = bytearray(original)

    actual_dispatch = list(
        struct.unpack_from("<IIIII", b, DISPATCH_TABLE_OFFSET)
    )
    if actual_dispatch != ORIGINAL_DISPATCH:
        raise RuntimeError(
            "BLD dispatch table does not match the expected stock binary."
        )

    first_word = struct.unpack_from("<I", b, UNKNOWN_COMMAND_HANDLER)[0]
    if first_word != UNKNOWN_HANDLER_FIRST_WORD:
        raise RuntimeError(
            "Unknown-command handler sanity check failed."
        )

    literal = struct.unpack_from("<I", b, UNKNOWN_HANDLER_LITERAL_OFFSET)[0]
    if literal != UNKNOWN_COMMAND_STRING_OFFSET:
        raise RuntimeError(
            "Unknown-command string literal sanity check failed."
        )

    if (
        b[
            UNKNOWN_COMMAND_STRING_OFFSET:
            UNKNOWN_COMMAND_STRING_OFFSET + len(UNKNOWN_COMMAND_STRING)
        ]
        != UNKNOWN_COMMAND_STRING
    ):
        raise RuntimeError(
            "Unknown-command string sanity check failed."
        )

    # Mirror the mandatory AmbaUSB Boot Loader patch:
    # force the RAM BLD to operate on NAND even though the SoC
    # itself was power-on-strapped into USB boot mode.
    stock_media = struct.unpack_from(
        "<I", b, BLD_BOOT_MEDIA_OFFSET
    )[0]

    if stock_media != BLD_BOOT_MEDIA_STOCK:
        raise RuntimeError(
            "Unexpected stock boot-media word at BLD+0x3C: "
            "0x%08X" % stock_media
        )

    struct.pack_into(
        "<I",
        b,
        BLD_BOOT_MEDIA_OFFSET,
        BLD_BOOT_MEDIA_NAND,
    )

    # Sanity-check and apply the USB-only diagnostic main patch.
    diag_format = "<" + "I" * len(USB_DIAG_STOCK_WORDS)
    stock_usb_diag = list(
        struct.unpack_from(diag_format, b, USB_DIAG_PATCH_OFFSET)
    )
    if stock_usb_diag != USB_DIAG_STOCK_WORDS:
        raise RuntimeError(
            "Unexpected instructions at the %s main USB decision point."
            % SOC_PROFILE
        )

    if len(USB_DIAG_PATCH_WORDS) != len(USB_DIAG_STOCK_WORDS):
        raise RuntimeError("USB diagnostic patch length mismatch.")

    struct.pack_into(
        diag_format,
        b,
        USB_DIAG_PATCH_OFFSET,
        *USB_DIAG_PATCH_WORDS
    )

    # Disable every command except GET (2) and SEND (3).
    patched_dispatch = [
        UNKNOWN_COMMAND_HANDLER,  # cmd 0 disabled
        UNKNOWN_COMMAND_HANDLER,  # cmd 1 disabled
        ORIGINAL_DISPATCH[2],     # cmd 2 GET kept
        ORIGINAL_DISPATCH[3],     # cmd 3 SEND kept
        UNKNOWN_COMMAND_HANDLER,  # cmd 4 disabled
    ]
    struct.pack_into(
        "<IIIII",
        b,
        DISPATCH_TABLE_OFFSET,
        *patched_dispatch
    )

    return member, original, bytes(b), patched_dispatch


def verify_ddr_state(dev):
    print()
    print("DDR state check")
    print("---------------")

    ok = True
    for address, expected in DDR_EXPECTED.items():
        actual = read32(dev, address)
        match = actual == expected
        print(
            "0x%08X = 0x%08X  expected 0x%08X  %s"
            % (
                address,
                actual,
                expected,
                "OK" if match else "MISMATCH",
            )
        )
        ok = ok and match

    if not ok:
        raise RuntimeError(
            "DDR is not in the expected initialized state.\n"
            "Re-enter 4255:000A, then run:\n"
            "  py .\\s2lm_ddr_init.py .\\platform.zip --apply"
        )


class FastLibusbBootrom:
    def __init__(self, vid=VID, pid=PID_BOOTROM):
        lib_path = libusb_package.get_library_path()
        if lib_path is None:
            raise RuntimeError(
                "libusb-package did not provide a libusb shared library."
            )

        self.lib = ctypes.CDLL(str(lib_path))
        self.ctx = ctypes.c_void_p()
        self.handle = None

        self.lib.libusb_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        self.lib.libusb_init.restype = ctypes.c_int

        self.lib.libusb_exit.argtypes = [ctypes.c_void_p]
        self.lib.libusb_exit.restype = None

        self.lib.libusb_open_device_with_vid_pid.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint16,
            ctypes.c_uint16,
        ]
        self.lib.libusb_open_device_with_vid_pid.restype = ctypes.c_void_p

        self.lib.libusb_close.argtypes = [ctypes.c_void_p]
        self.lib.libusb_close.restype = None

        self.lib.libusb_control_transfer.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint8,
            ctypes.c_uint8,
            ctypes.c_uint16,
            ctypes.c_uint16,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_uint16,
            ctypes.c_uint,
        ]
        self.lib.libusb_control_transfer.restype = ctypes.c_int

        rc = self.lib.libusb_init(ctypes.byref(self.ctx))
        if rc != 0:
            raise RuntimeError("libusb_init failed: %d" % rc)

        self.handle = self.lib.libusb_open_device_with_vid_pid(
            self.ctx, vid, pid
        )
        if not self.handle:
            self.close()
            raise RuntimeError(
                "Could not open %04X:%04X using direct libusb." % (vid, pid)
            )

        self.buf = (ctypes.c_ubyte * 12)()

    def close(self):
        if getattr(self, "handle", None):
            self.lib.libusb_close(self.handle)
            self.handle = None
        if getattr(self, "ctx", None):
            if self.ctx:
                self.lib.libusb_exit(self.ctx)
            self.ctx = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def send_words(self, opcode, address, value):
        struct.pack_into(
            "<III",
            self.buf,
            0,
            opcode & 0xFFFFFFFF,
            address & 0xFFFFFFFF,
            value & 0xFFFFFFFF,
        )
        rc = self.lib.libusb_control_transfer(
            self.handle,
            WRITE_TYPE,
            ROM_REQUEST,
            0,
            0,
            self.buf,
            12,
            USB_TIMEOUT_MS,
        )
        return rc

    def read32(self, address):
        buf = (ctypes.c_ubyte * 4)()
        rc = self.lib.libusb_control_transfer(
            self.handle,
            READ_TYPE,
            ROM_REQUEST,
            (address >> 16) & 0xFFFF,
            address & 0xFFFF,
            buf,
            4,
            USB_TIMEOUT_MS,
        )
        if rc != 4:
            raise RuntimeError(
                "Fast read32 failed at 0x%08X, rc=%d"
                % (address, rc)
            )
        return bytes(buf)


def upload_bld_fast(data, expected_dispatch_values=None):
    padded = data
    if len(padded) % 4:
        padded += b"\x00" * (4 - (len(padded) % 4))

    words = len(padded) // 4

    print()
    print("Uploading patched read-only BLD to DDR")
    print("--------------------------------------")
    print("Load address: 0x%08X" % BLD_LOAD_ADDRESS)
    print("Bytes:        %d" % len(data))
    print("Words:        %d" % words)
    print("Transport:    direct libusb (fast path)")
    print()

    start = time.perf_counter()
    next_pct = 0
    last_address = BLD_LOAD_ADDRESS

    with FastLibusbBootrom() as fast:
        for i in range(words):
            value = struct.unpack_from("<I", padded, i * 4)[0]
            address = BLD_LOAD_ADDRESS + i * 4
            last_address = address

            rc = fast.send_words(WRITE_OPCODE, address, value)
            if rc != 12:
                elapsed = time.perf_counter() - start
                raise RuntimeError(
                    "Fast BLD upload failed at word %d/%d, "
                    "address 0x%08X, rc=%d, elapsed %.3f s"
                    % (i, words, address, rc, elapsed)
                )

            pct = int(((i + 1) * 100) / words)
            if pct >= next_pct:
                elapsed = max(time.perf_counter() - start, 0.000001)
                rate = (i + 1) / elapsed
                print(
                    "  upload %3d%%  %7.0f words/s  %.3f s"
                    % (pct, rate, elapsed)
                )
                next_pct += 10

        elapsed = time.perf_counter() - start
        rate = words / max(elapsed, 0.000001)
        print()
        print(
            "Upload completed in %.3f s (%.0f words/s)."
            % (elapsed, rate)
        )

        print()
        print("Sparse BLD verification before jump")
        print("-----------------------------------")
        print(
            "Full byte-for-byte verification was already proven in v4."
        )
        print(
            "This run checks critical patches, USB descriptor, "
            "start/end and distributed samples only."
        )

        def verify_word(offset):
            if offset < 0 or offset + 4 > len(padded):
                raise RuntimeError(
                    "Invalid verify offset 0x%X" % offset
                )

            expected = padded[offset:offset + 4]
            actual = fast.read32(BLD_LOAD_ADDRESS + offset)

            if actual != expected:
                raise RuntimeError(
                    "Sparse readback mismatch at BLD+0x%X: "
                    "wanted %s got %s"
                    % (
                        offset,
                        expected.hex(" "),
                        actual.hex(" "),
                    )
                )

        # Header / vectors including BLD+0x3C boot-media patch.
        critical_offsets = list(range(0x0000, 0x0080, 4))

        # USB-only diagnostic main patch.
        critical_offsets += [
            USB_DIAG_PATCH_OFFSET + i * 4
            for i in range(len(USB_DIAG_PATCH_WORDS))
        ]

        # Read-only command dispatcher.
        critical_offsets += [
            DISPATCH_TABLE_OFFSET + i * 4
            for i in range(5)
        ]

        # Spread samples throughout the BLD.
        sample_count = 48
        max_aligned = (len(padded) - 4) & ~3

        for i in range(sample_count):
            if sample_count == 1:
                off = 0
            else:
                off = int(
                    (max_aligned * i) / (sample_count - 1)
                ) & ~3
            critical_offsets.append(off)

        # Final 64 bytes.
        tail_start = max(0, len(padded) - 64) & ~3
        critical_offsets += list(
            range(tail_start, len(padded) - 3, 4)
        )

        # Deduplicate while keeping deterministic order.
        seen = set()
        verify_offsets = []
        for off in critical_offsets:
            if off not in seen:
                seen.add(off)
                verify_offsets.append(off)

        for off in verify_offsets:
            verify_word(off)

        print(
            "Verified %d critical/distributed 32-bit words."
            % len(verify_offsets)
        )

        media_word = fast.read32(
            BLD_LOAD_ADDRESS + BLD_BOOT_MEDIA_OFFSET
        )
        if media_word != struct.pack("<I", BLD_BOOT_MEDIA_NAND):
            raise RuntimeError(
                "NAND boot-media patch is not present in DDR."
            )

        diag_words = b"".join(
            fast.read32(
                BLD_LOAD_ADDRESS
                + USB_DIAG_PATCH_OFFSET
                + i * 4
            )
            for i in range(len(USB_DIAG_PATCH_WORDS))
        )
        expected_diag = struct.pack(
            "<" + "I" * len(USB_DIAG_PATCH_WORDS),
            *USB_DIAG_PATCH_WORDS
        )
        if diag_words != expected_diag:
            raise RuntimeError(
                "USB-only diagnostic patch is not intact."
            )

        dispatch_raw = b"".join(
            fast.read32(
                BLD_LOAD_ADDRESS
                + DISPATCH_TABLE_OFFSET
                + i * 4
            )
            for i in range(5)
        )
        if expected_dispatch_values is None:
            expected_dispatch_values = [
                UNKNOWN_COMMAND_HANDLER,
                UNKNOWN_COMMAND_HANDLER,
                ORIGINAL_DISPATCH[2],
                ORIGINAL_DISPATCH[3],
                UNKNOWN_COMMAND_HANDLER,
            ]
        if len(expected_dispatch_values) != 5:
            raise RuntimeError(
                "Expected dispatcher must contain exactly five entries."
            )
        expected_dispatch = struct.pack(
            "<IIIII", *expected_dispatch_values
        )
        if dispatch_raw != expected_dispatch:
            raise RuntimeError(
                "Expected command dispatcher patch is not intact."
            )

        desc_offset = USB_DESCRIPTOR_OFFSET
        desc_raw = b"".join(
            fast.read32(
                BLD_LOAD_ADDRESS + desc_offset + i * 4
            )
            for i in range(5)
        )
        descriptor = desc_raw[:18]

        expected_descriptor = bytes.fromhex(
            "12 01 00 02 00 00 00 40 55 42 01 00 "
            "00 00 01 02 03 01"
        )

        print("USB descriptor in DDR:")
        print("  %s" % descriptor.hex(" "))

        if descriptor != expected_descriptor:
            raise RuntimeError(
                "Embedded 4255:0001 USB descriptor is not intact."
            )

        print("NAND media patch is intact.")
        print("USB-only main patch is intact.")
        print("Expected command dispatcher is intact.")
        print("Embedded 4255:0001 USB descriptor is intact.")

        print()
        print("IMPORTANT")
        print("---------")
        print("RESET must be RELEASED now.")
        print("Do NOT hold the reset button while BLD is running.")
        print()
        if KEEP_USB_BOOT_STRAP:
            print("Keep the 4.7k USB-boot resistor connected through RUN")
            print("and until 4255:0001 has enumerated.")
        else:
            print("The 4.7k USB-boot resistor may be released after")
            print("4255:000A has enumerated.")
        print("Do not change any other wiring before RUN.")
        print()
        if USB_DIAG_BYPASSES_MEDIA:
            print("This patched BLD bypasses NAND/media initialization and")
            print("enters Ambarella's USB download function directly.")
        else:
            print("This patched BLD keeps the vendor NAND/media and USB")
            print("initialization path, with write-capable commands disabled.")
        print()

        answer = input(
            "Type exactly RUN to execute USB-only diagnostic BLD: "
        ).strip()

        if answer != "RUN":
            raise RuntimeError(
                "Cancelled before BLD execution."
            )

        print("Starting BLD at 0x%08X ..." % BLD_ENTRY_ADDRESS)
        rc = fast.send_words(CALL_OPCODE, BLD_ENTRY_ADDRESS, 0)
        print("BootROM CALL transfer returned: %d" % rc)

    return last_address


def probe_after_upload_failure(timeout=6.0):
    print()
    print("Checking USB state after the failed transfer ...")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        bld = find_device(PID_BLD)
        if bld is not None:
            print("4255:0001 detected - BLD is running.")
            return "bld"

        boot = find_device(PID_BOOTROM)
        if boot is not None:
            print("4255:000A detected - BootROM is still/re-again active.")
            return "bootrom"

        time.sleep(0.2)

    print("No 4255:000A or 4255:0001 device was detected.")
    return "none"

def wait_for_bld(timeout=12.0):
    print()
    print("Waiting for Ambarella BLD USB device 4255:0001 ...")

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        dev = find_device(PID_BLD)
        if dev is not None:
            print("4255:0001 detected.")
            try:
                show_device(dev)
            except Exception:
                pass
            return dev
        time.sleep(0.25)

    return None


def command_launch(args):
    dev = find_device(PID_BOOTROM)
    if dev is None:
        raise RuntimeError(
            "4255:000A was not found.\n"
            "Put the camera into Ambarella USB boot mode first."
        )

    print("Ambarella %s read-only BLD launcher" % SOC_PROFILE)
    print("=====================================")
    show_device(dev)

    verify_ddr_state(dev)

    member, original, patched, dispatch = load_and_patch_bld(
        args.ambarella_zip
    )

    print()
    print("BLD")
    print("---")
    print("Archive member: %s" % member)
    print("Stock SHA256:   %s" % hashlib.sha256(original).hexdigest())
    print("Patched SHA256: %s" % hashlib.sha256(patched).hexdigest())
    print()
    print("AmbaUSB-compatible boot-media patch:")
    print(
        "  BLD+0x%02X: 0x%08X -> 0x%08X (NAND)"
        % (
            BLD_BOOT_MEDIA_OFFSET,
            BLD_BOOT_MEDIA_STOCK,
            BLD_BOOT_MEDIA_NAND,
        )
    )
    print()
    print("BLD startup path:")
    for line in USB_DIAG_DESCRIPTION:
        print("  %s" % line)
    if USB_DIAG_BYPASSES_MEDIA:
        print("  NAND/media pre-init path is bypassed.")
    else:
        print("  Stock NAND/media initialization is retained.")
    print()
    print("Command dispatcher after RAM-only patch:")
    print("  cmd 0 -> 0x%08X  DISABLED" % dispatch[0])
    print("  cmd 1 -> 0x%08X  DISABLED" % dispatch[1])
    print("  cmd 2 -> 0x%08X  GET kept" % dispatch[2])
    print("  cmd 3 -> 0x%08X  SEND kept" % dispatch[3])
    print("  cmd 4 -> 0x%08X  DISABLED" % dispatch[4])
    print()
    print("All patches exist only in the copy uploaded to DDR.")
    print("The ZIP file and NAND flash are not modified.")
    print()
    print("This step WILL:")
    print(
        "  - overwrite low DDR with the official %s NAND BLD"
        % SOC_PROFILE
    )
    print("  - upload it using direct libusb for AmbaUSB-like speed")
    print("  - execute that BLD from RAM")
    print()
    print("This tool does NOT send any NAND erase/program/write command.")
    if USB_DIAG_BYPASSES_MEDIA:
        print("The loaded BLD is forced directly into USB download mode; commands 0, 1 and 4 remain disabled.")
    else:
        print("The loaded BLD follows its stock startup path; commands 0, 1 and 4 remain disabled.")
    print()

    if not args.yes:
        answer = input("Type exactly LAUNCH to continue: ").strip()
        if answer != "LAUNCH":
            print("Cancelled.")
            return

    # Close PyUSB resources before opening the same device through
    # ctypes/libusb for the high-speed upload path.
    try:
        usb.util.dispose_resources(dev)
    except Exception:
        pass
    dev = None

    try:
        upload_bld_fast(patched)
    except Exception as exc:
        print()
        print("FAST UPLOAD ERROR: %s" % exc)
        state = probe_after_upload_failure()
        if state == "bld":
            print("Continuing because 4255:0001 appeared.")
        else:
            print()
            print("Do not retry immediately.")
            print("Power-cycle back to 4255:000A, re-run DDR init, then retry.")
            raise

    bld = wait_for_bld()

    print()
    if bld is not None:
        print("SUCCESS: USB-only BLD is running and 4255:0001 enumerated.")
        print()
        print("This proves the failure was in the pre-USB/media path.")
        print("Do not run PTB yet; send me this output first.")
    else:
        print("4255:0001 was not accessible through libusb yet.")
        print()
        print("Check Device Manager / Zadig.")
        print("Expected new USB ID:")
        print("  VID_4255&PID_0001")
        print()
        print("If it is present, install WinUSB for 4255:0001 with Zadig.")
        print("Do NOT power-cycle the camera while doing that.")
        print("Then run:")
        print(
            "  py .\\s2lm_readonly_dump.py ptb .\\ptb.bin"
        )


def get_bulk_endpoints(dev):
    try:
        dev.set_configuration()
    except usb.core.USBError:
        # It may already be configured on Windows.
        pass

    cfg = dev.get_active_configuration()
    intf = cfg[(0, 0)]

    ep_out = None
    ep_in = None

    for ep in intf:
        direction = usb.util.endpoint_direction(ep.bEndpointAddress)
        transfer_type = usb.util.endpoint_type(ep.bmAttributes)

        if transfer_type != usb.util.ENDPOINT_TYPE_BULK:
            continue

        if direction == usb.util.ENDPOINT_OUT:
            ep_out = ep
        elif direction == usb.util.ENDPOINT_IN:
            ep_in = ep

    if ep_out is None or ep_in is None:
        raise RuntimeError("Could not locate BLD bulk endpoints.")

    try:
        usb.util.claim_interface(dev, intf.bInterfaceNumber)
    except usb.core.USBError:
        # WinUSB commonly already exposes it correctly.
        pass

    return intf.bInterfaceNumber, ep_out, ep_in


def bulk_write(dev, ep, data, label):
    sent = dev.write(
        ep.bEndpointAddress,
        data,
        timeout=USB_TIMEOUT_MS,
    )
    if sent != len(data):
        raise RuntimeError(
            "%s: sent %d of %d bytes"
            % (label, sent, len(data))
        )


def bulk_read_exact(dev, ep, size, label):
    chunks = bytearray()
    deadline = time.monotonic() + 10.0

    while len(chunks) < size:
        remaining = size - len(chunks)
        try:
            part = dev.read(
                ep.bEndpointAddress,
                remaining,
                timeout=USB_TIMEOUT_MS,
            )
        except usb.core.USBTimeoutError:
            if time.monotonic() >= deadline:
                raise
            continue

        chunks.extend(bytes(part))

    if len(chunks) != size:
        raise RuntimeError(
            "%s: expected %d bytes, got %d"
            % (label, size, len(chunks))
        )

    return bytes(chunks)


def build_ucmd(command, subtype, a=0, b=0, c=0, d=0):
    return struct.pack(
        "<IIIIIIII",
        UCMD_MAGIC,
        command,
        8,
        subtype,
        a & 0xFFFFFFFF,
        b & 0xFFFFFFFF,
        c & 0xFFFFFFFF,
        d & 0xFFFFFFFF,
    )


def parse_ursp(raw, label):
    if len(raw) != 16:
        raise RuntimeError(
            "%s: invalid response length %d"
            % (label, len(raw))
        )

    magic, status, length, crc = struct.unpack("<IIII", raw)

    if magic != URSP_MAGIC:
        raise RuntimeError(
            "%s: invalid response magic 0x%08X"
            % (label, magic)
        )

    return status, length, crc


def hexdump(data, max_bytes=128):
    data = data[:max_bytes]
    for offset in range(0, len(data), 16):
        chunk = data[offset:offset + 16]
        hx = " ".join("%02X" % x for x in chunk)
        asc = "".join(chr(x) if 32 <= x < 127 else "." for x in chunk)
        print("%04X  %-47s  %s" % (offset, hx, asc))


def command_ptb(args):
    dev = find_device(PID_BLD)
    if dev is None:
        raise RuntimeError(
            "4255:0001 was not found through libusb.\n"
            "If Device Manager shows VID_4255&PID_0001, use Zadig "
            "to install WinUSB for that PID first."
        )

    print("Ambarella S2Lm PTB reader")
    print("=========================")
    show_device(dev)

    intf_num, ep_out, ep_in = get_bulk_endpoints(dev)

    print()
    print("Interface: %d" % intf_num)
    print(
        "Bulk OUT: 0x%02X, maxpacket=%d"
        % (ep_out.bEndpointAddress, ep_out.wMaxPacketSize)
    )
    print(
        "Bulk IN:  0x%02X, maxpacket=%d"
        % (ep_in.bEndpointAddress, ep_in.wMaxPacketSize)
    )
    print()
    print("Read-only protocol sequence:")
    print("  UCMD GET subtype 16 (PTB)")
    print("  URSP metadata")
    print("  UCMD SEND")
    print("  4096 bytes PTB")
    print("  final URSP")
    print()
    print("No receive/program/erase command is implemented in this tool.")

    get_cmd = build_ucmd(CMD_GET, GET_PTB)

    bulk_write(dev, ep_out, get_cmd, "GET PTB command")

    rsp1_raw = bulk_read_exact(dev, ep_in, 16, "GET PTB response")
    status1, length, expected_crc = parse_ursp(
        rsp1_raw,
        "GET PTB response",
    )

    print()
    print("GET response:")
    print("  status: 0x%08X" % status1)
    print("  length: %d (0x%X)" % (length, length))
    print("  crc32:  0x%08X" % expected_crc)

    if status1 != 0:
        raise RuntimeError(
            "GET PTB returned non-zero status 0x%08X"
            % status1
        )

    if length != PTB_SIZE:
        raise RuntimeError(
            "Expected PTB size 4096, device reported %d"
            % length
        )

    send_cmd = build_ucmd(CMD_SEND, GET_PTB)
    bulk_write(dev, ep_out, send_cmd, "SEND command")

    data = bulk_read_exact(dev, ep_in, length, "PTB data")

    rsp2_raw = bulk_read_exact(dev, ep_in, 16, "final response")
    status2, final_length, final_crc = parse_ursp(
        rsp2_raw,
        "final response",
    )

    if status2 != 0:
        raise RuntimeError(
            "Final response returned non-zero status 0x%08X"
            % status2
        )

    actual_crc = zlib.crc32(data) & 0xFFFFFFFF

    print()
    print("Verification:")
    print("  device CRC32: 0x%08X" % expected_crc)
    print("  local CRC32:  0x%08X" % actual_crc)
    print("  final status: 0x%08X" % status2)
    print("  final length: %d" % final_length)
    print("  final crc:    0x%08X" % final_crc)

    if actual_crc != expected_crc:
        raise RuntimeError(
            "PTB CRC mismatch: device=0x%08X local=0x%08X"
            % (expected_crc, actual_crc)
        )

    out = args.output
    with open(out, "wb") as f:
        f.write(data)

    sha = hashlib.sha256(data).hexdigest()

    print()
    print("PTB saved:")
    print("  %s" % out)
    print("  size:   %d bytes" % len(data))
    print("  SHA256: %s" % sha)
    print()
    print("First 128 bytes:")
    hexdump(data, 128)
    print()
    print("SUCCESS: PTB was read and verified.")
    print("No NAND write command was sent.")


def command_status(args):
    boot = find_device(PID_BOOTROM)
    bld = find_device(PID_BLD)

    if boot is not None:
        print("4255:000A BootROM detected")
        show_device(boot)

    if bld is not None:
        if boot is not None:
            print()
        print("4255:0001 BLD detected")
        show_device(bld)

    if boot is None and bld is None:
        print("No Ambarella 4255:000A/0001 device accessible through libusb.")
        return 1

    return 0


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Read-only helper for Ambarella %s/A12: " % SOC_PROFILE +
            "launch a command-restricted BLD from DDR and read PTB."
        )
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser(
        "status",
        help="Show whether BootROM or BLD is connected",
    )
    p_status.set_defaults(func=command_status)

    p_launch = sub.add_parser(
        "launch",
        help=(
            "Patch stock %s BLD in RAM to keep only GET/SEND, "
            % SOC_PROFILE +
            "upload it to initialized DDR, and execute it"
        ),
    )
    p_launch.add_argument(
        "ambarella_zip",
        help="Full Ambarella.zip archive",
    )
    p_launch.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive LAUNCH confirmation",
    )
    p_launch.set_defaults(func=command_launch)

    p_ptb = sub.add_parser(
        "ptb",
        help="Read the 4096-byte PTB from running 4255:0001 BLD",
    )
    p_ptb.add_argument(
        "output",
        help="Output PTB file, e.g. ptb.bin",
    )
    p_ptb.set_defaults(func=command_ptb)

    args = parser.parse_args()

    try:
        result = args.func(args)
        if isinstance(result, int):
            sys.exit(result)
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
    except usb.core.USBError as exc:
        print()
        print("USB ERROR: %s" % exc)
        sys.exit(10)
    except Exception as exc:
        print()
        print("ERROR: %s" % exc)
        sys.exit(11)


if __name__ == "__main__":
    main()
