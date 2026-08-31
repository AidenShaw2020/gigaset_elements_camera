"""Verify the stock S2Lm BLD RAM upload protocol without touching NAND.

This diagnostic restores the vendor BLD's commands 0 and 1 only in the DDR
copy, keeps GET/SEND, and leaves command 4 disabled.  It uploads a deliberately
short-packet-terminated test pattern to a high DDR address, reads it back through
GET MEMORY, and compares the bytes and SHA-256 digest.

No flash erase/program/PTB command is sent by this tool.
"""

import argparse
import binascii
import hashlib
import struct
import time

import usb.core
import usb.util

import s2lm_libusb0_stock_readonly_v11 as profile


tool = profile.tool

UCMD_MAGIC = 0x55434D44
URSP_MAGIC = 0x55525350
CMD_READY_TO_RECEIVE = 0
CMD_RECEIVE_DATA = 1
CMD_READY_TO_SEND = 2
CMD_SEND_DATA = 3
FLAG_ADDRESS = 0x02
FLAG_COMMAND = 0x08
GET_MEMORY = 1

# The camera has 256 MiB DDR.  This address is outside the low BLD area and
# outside the 0x0c000000 buffer used by the read-only NAND dumper.
PROBE_ADDRESS = 0x0D000000
PROBE_LENGTH = 4097  # Not divisible by USB max-packet size: emits short packet.


def _command(*words):
    if len(words) > 8:
        raise ValueError("UCMD has at most eight words")
    return struct.pack("<8I", *(list(words) + [0] * (8 - len(words))))


def _response(dev, ep_in, label):
    raw = bytes(dev.read(ep_in, 16, timeout=10000))
    if len(raw) != 16:
        raise RuntimeError("%s: expected 16 bytes, got %d" % (label, len(raw)))
    magic, status, p0, p1 = struct.unpack("<4I", raw)
    if magic != URSP_MAGIC:
        raise RuntimeError("%s: bad magic 0x%08X" % (label, magic))
    if status != 0:
        raise RuntimeError("%s: status 0x%08X" % (label, status))
    return p0, p1


def _write(dev, ep_out, payload, label):
    sent = int(dev.write(ep_out, payload, timeout=30000))
    if sent != len(payload):
        raise RuntimeError("%s: sent %d of %d bytes" % (label, sent, len(payload)))


def _read_exact(dev, ep_in, size, label):
    out = bytearray()
    deadline = time.monotonic() + 20.0
    while len(out) < size:
        try:
            chunk = bytes(dev.read(ep_in, size - len(out), timeout=3000))
        except usb.core.USBTimeoutError:
            if time.monotonic() >= deadline:
                raise RuntimeError("%s timed out at %d/%d" % (label, len(out), size))
            continue
        out.extend(chunk)
    return bytes(out)


def _bulk_endpoints(dev):
    cfg = dev.get_active_configuration()
    for intf in cfg:
        ep_out = None
        ep_in = None
        for ep in intf:
            if usb.util.endpoint_type(ep.bmAttributes) != usb.util.ENDPOINT_TYPE_BULK:
                continue
            if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_OUT:
                ep_out = ep.bEndpointAddress
            else:
                ep_in = ep.bEndpointAddress
        if ep_out is not None and ep_in is not None:
            return ep_out, ep_in
    raise RuntimeError("BLD bulk endpoints not found")


def load_ramprobe_bld(zip_path):
    member, original, common, current_dispatch = tool.load_and_patch_bld(zip_path)
    expected = [
        tool.UNKNOWN_COMMAND_HANDLER,
        tool.UNKNOWN_COMMAND_HANDLER,
        tool.ORIGINAL_DISPATCH[2],
        tool.ORIGINAL_DISPATCH[3],
        tool.UNKNOWN_COMMAND_HANDLER,
    ]
    if current_dispatch != expected:
        raise RuntimeError("Unexpected common read-only dispatcher")

    image = bytearray(common)
    ram_dispatch = [
        tool.ORIGINAL_DISPATCH[0],  # address/finish/jump control
        tool.ORIGINAL_DISPATCH[1],  # receive bytes into DDR
        tool.ORIGINAL_DISPATCH[2],  # read DDR back
        tool.ORIGINAL_DISPATCH[3],  # transmit bytes from DDR
        tool.UNKNOWN_COMMAND_HANDLER,
    ]
    struct.pack_into(
        "<5I", image, tool.DISPATCH_TABLE_OFFSET, *ram_dispatch
    )
    return member, original, bytes(image), ram_dispatch


def launch(zip_path):
    bootrom = tool.find_device(tool.PID_BOOTROM)
    if bootrom is None:
        raise RuntimeError("Ambarella BootROM 4255:000A not found")

    tool.verify_ddr_state(bootrom)
    member, original, patched, dispatch = load_ramprobe_bld(zip_path)

    print("\nRAM-only BLD profile")
    print("--------------------")
    print("Archive member: %s" % member)
    print("Stock SHA256:   %s" % hashlib.sha256(original).hexdigest())
    print("Patched SHA256: %s" % hashlib.sha256(patched).hexdigest())
    print("Dispatcher:     %s" % ", ".join("0x%08X" % x for x in dispatch))
    print("NAND command path: not used")

    try:
        usb.util.dispose_resources(bootrom)
    except Exception:
        pass

    tool.upload_bld_fast(
        patched, expected_dispatch_values=dispatch
    )
    bld = tool.wait_for_bld()
    if bld is None:
        raise RuntimeError("BLD 4255:0001 did not enumerate")
    return bld


def probe(dev):
    ep_out, ep_in = _bulk_endpoints(dev)
    pattern = bytes(((i * 73 + 41) & 0xFF) for i in range(PROBE_LENGTH))
    expected_crc = binascii.crc32(pattern) & 0xFFFFFFFF

    print("\nDDR round-trip")
    print("--------------")
    print("Address: 0x%08X" % PROBE_ADDRESS)
    print("Length:  %d" % len(pattern))
    print("SHA256:  %s" % hashlib.sha256(pattern).hexdigest())

    _write(
        dev,
        ep_out,
        _command(UCMD_MAGIC, CMD_READY_TO_RECEIVE, FLAG_ADDRESS, PROBE_ADDRESS),
        "set receive address",
    )
    _response(dev, ep_in, "set receive address")

    _write(
        dev,
        ep_out,
        _command(UCMD_MAGIC, CMD_RECEIVE_DATA, expected_crc),
        "start DDR receive",
    )
    _write(dev, ep_out, pattern, "DDR test pattern")
    _response(dev, ep_in, "DDR receive completion")

    _write(
        dev,
        ep_out,
        _command(
            UCMD_MAGIC,
            CMD_READY_TO_SEND,
            FLAG_COMMAND,
            GET_MEMORY,
            PROBE_ADDRESS,
            len(pattern),
        ),
        "GET MEMORY",
    )
    length, crc = _response(dev, ep_in, "GET MEMORY metadata")
    if length != len(pattern) or crc != expected_crc:
        raise RuntimeError(
            "GET MEMORY metadata mismatch: length=%d crc=0x%08X" % (length, crc)
        )

    _write(
        dev,
        ep_out,
        _command(UCMD_MAGIC, CMD_SEND_DATA, FLAG_COMMAND, GET_MEMORY),
        "SEND MEMORY",
    )
    reread = _read_exact(dev, ep_in, length, "DDR readback")
    _response(dev, ep_in, "DDR readback completion")

    if reread != pattern:
        raise RuntimeError("DDR readback differs from uploaded pattern")

    print("Readback SHA256: %s" % hashlib.sha256(reread).hexdigest())
    print("RAM_PROBE_OK")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ambarella_zip", help="Path to vendor Ambarella.zip")
    args = parser.parse_args()

    print("Ambarella S2Lm RAM-only USB probe")
    print("=================================")
    print("This run does not erase, program, or update NAND/PTB.")
    dev = launch(args.ambarella_zip)
    try:
        probe(dev)
    finally:
        try:
            usb.util.dispose_resources(dev)
        except Exception:
            pass


if __name__ == "__main__":
    main()
