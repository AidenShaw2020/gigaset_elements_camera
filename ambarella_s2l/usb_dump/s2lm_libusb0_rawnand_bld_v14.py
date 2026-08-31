"""Launch an S2Lm BLD whose GET subtype 1 performs bounded NAND reads.

The patch exists only in the DDR copy. Commands 0, 1 and 4 remain routed to
the BLD's unknown-command handler, so receive/program/erase/PTB-update paths
cannot be invoked over USB. GET subtype 1 calls the vendor NAND read routine
at 0xBA7C and exposes its DDR buffer through the existing SEND command.
"""

import struct

import s2lm_libusb0_stock_readonly_v11 as profile


tool = profile.tool

RAW_GET_PATCH_OFFSET = 0x0000E1AC
RAW_GET_STOCK_WORDS = [
    0xE5963010,  # LDR r3,[r6,#0x10]
    0xE596C014,  # LDR r12,[r6,#0x14]
    0xE5853010,  # STR r3,[r5,#0x10]
    0xE585C014,  # STR r12,[r5,#0x14]
    0xEAFFFFD7,  # B 0xE120
    0xEBFFD514,  # original following helper call
    0xEAFFFFD1,  # original following branch
]
RAW_GET_PATCH_WORDS = [
    0xE5961010,  # LDR r1,[r6,#0x10]  ; NAND byte offset
    0xE5962014,  # LDR r2,[r6,#0x14]  ; requested byte length
    0xE3A00303,  # MOV r0,#0x0C000000 ; verified BLD export buffer
    0xE5850010,  # STR r0,[r5,#0x10]  ; SEND buffer
    0xEBFFF62E,  # BL 0xBA7C           ; returns actual byte count in r0
    0xE5850014,  # STR r0,[r5,#0x14]  ; SEND/response length
    0xEAFFFFD4,  # B 0xE11C            ; reload r12, CRC and normal URSP
]

base_load_and_patch_bld = tool.load_and_patch_bld


def load_and_patch_bld(zip_path):
    member, original, common_patched, dispatch = base_load_and_patch_bld(zip_path)
    image = bytearray(common_patched)
    actual = list(struct.unpack_from("<7I", image, RAW_GET_PATCH_OFFSET))
    if actual != RAW_GET_STOCK_WORDS:
        raise RuntimeError(
            "Unexpected instructions at raw GET patch site: %s"
            % ", ".join("0x%08X" % word for word in actual)
        )
    struct.pack_into("<7I", image, RAW_GET_PATCH_OFFSET, *RAW_GET_PATCH_WORDS)
    return member, original, bytes(image), dispatch


tool.TOOL_VERSION = "14.0-libusb0-s2lm-rawnand-readonly"
tool.SOC_PROFILE = "S2Lm raw-NAND GET v14 / native libusb-win32"
tool.load_and_patch_bld = load_and_patch_bld
tool.USB_DIAG_DESCRIPTION = [
    "Stock NAND/media and USB initialization retained.",
    "GET subtype 1 calls vendor nand_read at 0x0000BA7C.",
    "The vendor routine's returned byte count feeds SEND and CRC.",
    "Read buffer: 0x0C000000; offset/length supplied by host.",
]


if __name__ == "__main__":
    tool.main()
