"""Dry-run the stock S2Lm partition-writer entry point without touching NAND.

The currently running RAM-only BLD is given a deliberately invalid partition
header (wrong magic).  A tiny ARM wrapper calls the stock
flprog_write_partition(PART_LNX, image, length) entry point, stores its return
value in DDR, and restarts the patched BLD.  The expected return is -1 at the
header validation gate, before PTB access, erase, or program operations.

This diagnostic never constructs or uploads a valid partition image header.
"""

import binascii
import struct
import usb.util

import s2lm_libusb0_ramprobe_v15 as transport


tool = transport.tool

PART_LNX = 0x0B
WRITER_ENTRY = 0x000062A4
WRAPPER_ADDRESS = 0x0D000000
RESULT_ADDRESS = 0x0D000100
IMAGE_ADDRESS = 0x0D100000
RESULT_SENTINEL = 0x52575244  # "DRWR" in little endian memory
EXPECTED_BAD_HEADER_RESULT = 0xFFFFFFFF
ORIGINAL_INQUIRY_HANDLER = tool.ORIGINAL_DISPATCH[4]

CMD_DRY_RUN = 4
DRY_RUN_DISPATCH_ENTRY = tool.DISPATCH_TABLE_OFFSET + CMD_DRY_RUN * 4

# cmp r0,#14; movle r3,#0; movgt r3,#1; cmp r0,#2
WRITER_FINGERPRINT = bytes.fromhex(
    "0e 00 50 e3 00 30 a0 d3 01 30 a0 c3 02 00 50 e3"
)


def upload_blob(dev, ep_out, ep_in, address, payload, label):
    crc = binascii.crc32(payload) & 0xFFFFFFFF
    transport._write(
        dev,
        ep_out,
        transport._command(
            transport.UCMD_MAGIC,
            transport.CMD_READY_TO_RECEIVE,
            transport.FLAG_ADDRESS,
            address,
        ),
        "%s address" % label,
    )
    transport._response(dev, ep_in, "%s address" % label)
    transport._write(
        dev,
        ep_out,
        transport._command(
            transport.UCMD_MAGIC,
            transport.CMD_RECEIVE_DATA,
            crc,
        ),
        "%s receive command" % label,
    )
    transport._write(dev, ep_out, payload, label)
    transport._response(dev, ep_in, "%s completion" % label)


def read_memory(dev, ep_out, ep_in, address, length, label):
    transport._write(
        dev,
        ep_out,
        transport._command(
            transport.UCMD_MAGIC,
            transport.CMD_READY_TO_SEND,
            transport.FLAG_COMMAND,
            transport.GET_MEMORY,
            address,
            length,
        ),
        "%s GET" % label,
    )
    actual_length, crc = transport._response(
        dev, ep_in, "%s metadata" % label
    )
    if actual_length != length:
        raise RuntimeError(
            "%s length mismatch: wanted %d got %d"
            % (label, length, actual_length)
        )
    transport._write(
        dev,
        ep_out,
        transport._command(
            transport.UCMD_MAGIC,
            transport.CMD_SEND_DATA,
            transport.FLAG_COMMAND,
            transport.GET_MEMORY,
        ),
        "%s SEND" % label,
    )
    data = transport._read_exact(dev, ep_in, length, label)
    transport._response(dev, ep_in, "%s completion" % label)
    actual_crc = binascii.crc32(data) & 0xFFFFFFFF
    if actual_crc != crc:
        raise RuntimeError(
            "%s CRC mismatch: metadata 0x%08X readback 0x%08X"
            % (label, crc, actual_crc)
        )
    return data


def build_invalid_image():
    payload = b"\xA5"
    header = bytearray(256)
    struct.pack_into(
        "<7I",
        header,
        0,
        binascii.crc32(payload) & 0xFFFFFFFF,
        0,
        0,
        len(payload),
        0,
        0,
        0xDEADBEEF,  # deliberately NOT PARTHD_MAGIC 0xA324EB90
    )
    return bytes(header) + payload


def build_wrapper(image_length):
    # ARM state, position-independent literal loads.  The BLD dispatcher keeps
    # the HOST_CMD pointer in r8.  flprog_write_partition follows AAPCS and
    # preserves r8, so after storing the result and restoring cmd 4 we jump to
    # the stock inquiry handler.  It sends the normal USB response and returns
    # to the dispatcher's common tail without resetting or re-enumerating USB.
    words = [
        0xE59F0024,  # ldr r0, [pc,#0x24] -> PART_LNX
        0xE59F1024,  # ldr r1, [pc,#0x24] -> image address
        0xE59F2024,  # ldr r2, [pc,#0x24] -> image length
        0xE59F3024,  # ldr r3, [pc,#0x24] -> writer entry
        0xE12FFF33,  # blx r3
        0xE59F1020,  # ldr r1, [pc,#0x20] -> result address
        0xE5810000,  # str r0, [r1]
        0xE59F101C,  # ldr r1, [pc,#0x1c] -> cmd 4 dispatch entry
        0xE59F201C,  # ldr r2, [pc,#0x1c] -> unknown handler
        0xE5812000,  # str r2, [r1]
        0xE59FF018,  # ldr pc, [pc,#0x18] -> stock inquiry handler
        PART_LNX,
        IMAGE_ADDRESS,
        image_length,
        WRITER_ENTRY,
        RESULT_ADDRESS,
        DRY_RUN_DISPATCH_ENTRY,
        tool.UNKNOWN_COMMAND_HANDLER,
        ORIGINAL_INQUIRY_HANDLER,
    ]
    return struct.pack("<%dI" % len(words), *words)


def main():
    print("Ambarella S2Lm partition-writer dry-run")
    print("========================================")
    print("Invalid header gate only; no valid flash image is uploaded.")
    print("No erase/program/PTB command is issued by this script.\n")

    dev = tool.find_device(tool.PID_BLD)
    if dev is None:
        raise RuntimeError("Running BLD 4255:0001 not found")
    tool.show_device(dev)
    ep_out, ep_in = transport._bulk_endpoints(dev)

    fingerprint = read_memory(
        dev,
        ep_out,
        ep_in,
        WRITER_ENTRY,
        len(WRITER_FINGERPRINT),
        "writer fingerprint",
    )
    if fingerprint != WRITER_FINGERPRINT:
        raise RuntimeError(
            "Writer fingerprint mismatch at 0x%08X" % WRITER_ENTRY
        )
    print("Writer entry fingerprint at 0x%08X: OK" % WRITER_ENTRY)

    invalid_image = build_invalid_image()
    wrapper = build_wrapper(len(invalid_image))
    sentinel = struct.pack("<I", RESULT_SENTINEL)

    upload_blob(
        dev, ep_out, ep_in, IMAGE_ADDRESS, invalid_image, "invalid image"
    )
    upload_blob(dev, ep_out, ep_in, WRAPPER_ADDRESS, wrapper, "ARM wrapper")
    upload_blob(dev, ep_out, ep_in, RESULT_ADDRESS, sentinel, "result sentinel")

    checks = [
        (IMAGE_ADDRESS, invalid_image, "invalid image"),
        (WRAPPER_ADDRESS, wrapper, "ARM wrapper"),
        (RESULT_ADDRESS, sentinel, "result sentinel"),
    ]
    for address, expected, label in checks:
        actual = read_memory(
            dev, ep_out, ep_in, address, len(expected), "%s verify" % label
        )
        if actual != expected:
            raise RuntimeError("%s readback mismatch" % label)
        print("%s DDR readback: OK" % label)

    dispatch_pointer = struct.pack("<I", WRAPPER_ADDRESS)
    upload_blob(
        dev,
        ep_out,
        ep_in,
        DRY_RUN_DISPATCH_ENTRY,
        dispatch_pointer,
        "temporary cmd 4 dispatcher",
    )
    if read_memory(
        dev,
        ep_out,
        ep_in,
        DRY_RUN_DISPATCH_ENTRY,
        4,
        "temporary cmd 4 dispatcher verify",
    ) != dispatch_pointer:
        raise RuntimeError("Temporary cmd 4 dispatcher readback mismatch")
    print("temporary cmd 4 dispatcher DDR readback: OK")

    print("\nCalling writer with deliberately invalid magic ...")
    transport._write(
        dev,
        ep_out,
        transport._command(
            transport.UCMD_MAGIC,
            CMD_DRY_RUN,
            1,  # USB_BLD_INQUIRY_CHIP: stock handler returns success
        ),
        "dry-run dispatch",
    )
    transport._response(dev, ep_in, "dry-run dispatch")
    try:
        result_raw = read_memory(
            dev, ep_out, ep_in, RESULT_ADDRESS, 4, "writer result"
        )
        result = struct.unpack("<I", result_raw)[0]
        print("Writer returned: 0x%08X (%d)" % (result, result - (1 << 32)))
        if result != EXPECTED_BAD_HEADER_RESULT:
            raise RuntimeError(
                "Unexpected dry-run result 0x%08X; expected -1" % result
            )
        print("PARTWRITER_DRY_RUN_OK")
        print("Header validation and BLD return path are proven.")
        print("NAND was not erased or programmed.")
    finally:
        try:
            usb.util.dispose_resources(dev)
        except Exception:
            pass


if __name__ == "__main__":
    main()
