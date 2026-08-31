"""Program and verify only the S2Lm Linux/rootfs partition through RAM BLD.

The tool deliberately uses the vendor ``flprog_write_partition`` routine so
NAND bad-block handling and PTB updates remain owned by the camera's stock
bootloader.  It never writes BST/BLD/PTB directly.  A full pre-write DDR
round-trip and a post-write logical NAND readback are mandatory.
"""

import argparse
import binascii
import hashlib
import json
from pathlib import Path
import struct
import time

import usb.core
import usb.util

import s2lm_libusb0_partwriter_probe_v16 as probe


transport = probe.transport
tool = probe.tool

PART_LNX = 0x0B
PARTHD_MAGIC = 0xA324EB90
FLPART_MAGIC = 0x8732DFE6
ERASEBLOCK = 0x20000
PARTITION_SIZE = 0x04200000
PARTITION_OFFSET = 0x012A0000
# The BLD reuses the start of the selected DDR receive buffer for subsequent
# 32-byte UCMD packets.  Keep a sacrificial page before the actual package so
# its partition header cannot be overwritten by the verification/PTB command.
TRANSFER_ADDRESS = 0x02000000
TRANSFER_PREFIX = 0x1000
IMAGE_ADDRESS = TRANSFER_ADDRESS + TRANSFER_PREFIX
WRAPPER_ADDRESS = 0x0D000000
RESULT_ADDRESS = 0x0D000100
RESULT_SENTINEL = 0x52575257
CMD_WRITE = 4
DISPATCH_ENTRY = tool.DISPATCH_TABLE_OFFSET + CMD_WRITE * 4
USB_CHUNK = 1024 * 1024
# The RAM BLD accepts 1 and 2 MiB GET_MEMORY requests, but rejects a 4 MiB
# request with status 1 on this camera.  Keep verification conservatively
# below that transport limit; this affects DDR readback only, never NAND I/O.
VERIFY_CHUNK = 1 * 1024 * 1024
RAW_READ_CHUNK = 4 * 1024 * 1024
PTB_GET_SUBTYPE = 16
# The vendor RAM BLD is generic and initially describes the SDK reference
# layout, where LNX starts at block 821 and spans 1024 blocks.  This camera's
# dump proves that LNX starts at block 149 and spans 528 blocks.  The writer
# uses these live DDR tables when it chooses the NAND range.
PART_START_TABLE = 0x157EC
PART_BLOCKS_TABLE = 0x1583C
GENERIC_LNX_START_BLOCK = 821
GENERIC_LNX_BLOCKS = 1024
CAMERA_LNX_START_BLOCK = PARTITION_OFFSET // ERASEBLOCK
CAMERA_LNX_BLOCKS = PARTITION_SIZE // ERASEBLOCK

RAW_GET_PATCH_OFFSET = 0x0000E1AC
RAW_GET_STOCK_WORDS = [
    0xE5963010,
    0xE596C014,
    0xE5853010,
    0xE585C014,
    0xEAFFFFD7,
    0xEBFFD514,
    0xEAFFFFD1,
]
RAW_GET_PATCH_WORDS = [
    0xE5961010,
    0xE5962014,
    0xE3A00303,
    0xE5850010,
    0xEBFFF62E,
    0xE5850014,
    0xEAFFFFD4,
]

ERRORS = {
    0: "OK",
    -1: "invalid image magic",
    -2: "invalid image length",
    -3: "image CRC mismatch",
    -4: "version rejected",
    -5: "date rejected",
    -6: "NAND image programming failed",
    -7: "PTB read failed",
    -8: "PTB update failed",
    -9: "firmware file error",
    -10: "firmware flag error",
    -11: "not enough memory",
    -12: "FIFO open failed",
    -13: "FIFO read failed",
    -14: "payload error",
    -15: "illegal header",
    -16: "extras magic error",
    -17: "preprocess failed",
    -18: "postprocess failed",
    -19: "metadata update failed",
    -20: "metadata read failed",
    -21: "hardware signature verification failed",
    -22: "hardware signature operation failed",
}


def response_long(dev, ep_in, label, timeout=15 * 60 * 1000):
    raw = bytes(dev.read(ep_in, 16, timeout=timeout))
    if len(raw) != 16:
        raise RuntimeError("%s: expected 16 bytes, got %d" % (label, len(raw)))
    magic, status, p0, p1 = struct.unpack("<4I", raw)
    if magic != transport.URSP_MAGIC:
        raise RuntimeError("%s: bad response magic 0x%08X" % (label, magic))
    if status != 0:
        raise RuntimeError("%s: USB status 0x%08X" % (label, status))
    return p0, p1


def upload_stream(dev, ep_out, ep_in, address, payload, label):
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

    total = len(payload)
    sent_total = 0
    started = time.monotonic()
    while sent_total < total:
        end = min(sent_total + USB_CHUNK, total)
        block = payload[sent_total:end]
        sent = int(dev.write(ep_out, block, timeout=120000))
        if sent != len(block):
            raise RuntimeError(
                "%s: sent %d of %d bytes" % (label, sent, len(block))
            )
        sent_total += sent
        print(
            "\rUploading %s: %6.2f%%" % (label, 100.0 * sent_total / total),
            end="",
            flush=True,
        )
    if total % 64 == 0:
        try:
            zlp = int(dev.write(ep_out, b"", timeout=30000))
            if zlp != 0:
                raise RuntimeError(
                    "%s: zero-length packet returned %d" % (label, zlp)
                )
        except usb.core.USBError as error:
            if "timeout" not in str(error).lower():
                raise
            print(
                "\nZLP reached the loader; waiting for its long CRC pass ...",
                flush=True,
            )
    response_long(dev, ep_in, "%s completion" % label)
    elapsed = max(time.monotonic() - started, 0.001)
    print("  %.2f MiB/s" % (total / elapsed / (1024 * 1024)))


def read_memory_retry_status1(dev, ep_out, ep_in, address, length, label):
    """Retry the one transient status-1 response left by a large DDR upload."""
    try:
        return probe.read_memory(dev, ep_out, ep_in, address, length, label)
    except RuntimeError as error:
        if "status 0x00000001" not in str(error):
            raise
        print("%s: transient loader status 1; retrying once" % label)
        return probe.read_memory(
            dev, ep_out, ep_in, address, length, label + " retry"
        )


def read_memory_chunked(dev, ep_out, ep_in, address, length, label):
    digest = hashlib.sha256()
    offset = 0
    while offset < length:
        count = min(VERIFY_CHUNK, length - offset)
        data = read_memory_retry_status1(
            dev, ep_out, ep_in, address + offset, count, "%s @%08X" % (label, offset)
        )
        digest.update(data)
        offset += count
        print(
            "\rVerifying %s: %6.2f%%" % (label, 100.0 * offset / length),
            end="",
            flush=True,
        )
    print()
    return digest.hexdigest()


def reopen_bld(dev):
    """Release and reacquire the RAM BLD after a large host-to-DDR transfer.

    On the S30851-H2531 camera the first GET_MEMORY command issued through the
    same libusb handle after a multi-megabyte upload can return status 1.  The
    BLD and DDR contents remain intact; reacquiring the USB interface clears
    that transport state without rebooting or touching NAND.
    """
    try:
        usb.util.dispose_resources(dev)
    except Exception:
        pass
    reopened = tool.find_device(tool.PID_BLD)
    if reopened is None:
        raise RuntimeError("RAM BLD 4255:0001 disappeared after DDR upload")
    ep_out, ep_in = transport._bulk_endpoints(reopened)
    return reopened, ep_out, ep_in


def get_ptb(dev, ep_out, ep_in):
    transport._write(
        dev,
        ep_out,
        transport._command(
            transport.UCMD_MAGIC,
            transport.CMD_READY_TO_SEND,
            transport.FLAG_COMMAND,
            PTB_GET_SUBTYPE,
        ),
        "GET PTB",
    )
    length, expected_crc = transport._response(dev, ep_in, "GET PTB metadata")
    if length != 4096:
        raise RuntimeError("Unexpected PTB length %d" % length)
    transport._write(
        dev,
        ep_out,
        transport._command(
            transport.UCMD_MAGIC,
            transport.CMD_SEND_DATA,
            transport.FLAG_COMMAND,
            PTB_GET_SUBTYPE,
        ),
        "SEND PTB",
    )
    data = transport._read_exact(dev, ep_in, length, "PTB")
    transport._response(dev, ep_in, "PTB completion")
    actual_crc = binascii.crc32(data) & 0xFFFFFFFF
    if actual_crc != expected_crc:
        raise RuntimeError("PTB transfer CRC mismatch")
    return data


def lnx_metadata(ptb):
    values = struct.unpack_from("<7I", ptb, PART_LNX * 28)
    return dict(
        zip(
            ("crc32", "ver_num", "ver_date", "img_len", "mem_addr", "flag", "magic"),
            values,
        )
    )


def prepare_payload(path):
    full = path.read_bytes()
    if len(full) != PARTITION_SIZE:
        raise RuntimeError(
            "Expected a 0x%X-byte rootfs artifact, got 0x%X"
            % (PARTITION_SIZE, len(full))
        )
    if len(full) % ERASEBLOCK:
        raise RuntimeError("Rootfs artifact is not eraseblock-aligned")

    last = -1
    for index in range(len(full) // ERASEBLOCK):
        block = full[index * ERASEBLOCK : (index + 1) * ERASEBLOCK]
        if block != b"\xFF" * ERASEBLOCK:
            if block[:4] != b"UBI#":
                raise RuntimeError(
                    "Non-empty eraseblock %d has no UBI EC header" % index
                )
            last = index
    if last < 0:
        raise RuntimeError("Rootfs artifact is empty")
    payload = full[: (last + 1) * ERASEBLOCK]
    if len(payload) > PARTITION_SIZE:
        raise RuntimeError("Trimmed image exceeds rootfs partition")
    return full, payload


def prepare_program_payload(payload):
    """Return the contiguous payload for the camera's real LNX partition."""
    return payload


def install_camera_lnx_layout(dev, ep_out, ep_in):
    """Patch only the generic RAM BLD's live LNX start/size entries."""
    entries = (
        (
            PART_START_TABLE + PART_LNX * 4,
            GENERIC_LNX_START_BLOCK,
            CAMERA_LNX_START_BLOCK,
            "LNX start block",
        ),
        (
            PART_BLOCKS_TABLE + PART_LNX * 4,
            GENERIC_LNX_BLOCKS,
            CAMERA_LNX_BLOCKS,
            "LNX block count",
        ),
    )
    for address, generic, actual, label in entries:
        current_raw = probe.read_memory(dev, ep_out, ep_in, address, 4, label)
        current = struct.unpack("<I", current_raw)[0]
        if current not in (generic, actual):
            raise RuntimeError(
                "%s has unexpected value 0x%X at 0x%08X"
                % (label, current, address)
            )
        if current != actual:
            probe.upload_blob(
                dev,
                ep_out,
                ep_in,
                address,
                struct.pack("<I", actual),
                label,
            )
        verified = struct.unpack(
            "<I", probe.read_memory(dev, ep_out, ep_in, address, 4, label + " verify")
        )[0]
        if verified != actual:
            raise RuntimeError("%s RAM patch did not persist" % label)


def build_header(payload, old):
    header = bytearray(256)
    struct.pack_into(
        "<7I",
        header,
        0,
        binascii.crc32(payload) & 0xFFFFFFFF,
        old["ver_num"],
        old["ver_date"],
        len(payload),
        old["mem_addr"],
        old["flag"],
        PARTHD_MAGIC,
    )
    return bytes(header)


def build_wrapper(image_length):
    words = [
        0xE59F0024,
        0xE59F1024,
        0xE59F2024,
        0xE59F3024,
        0xE12FFF33,
        0xE59F1020,
        0xE5810000,
        0xE59F101C,
        0xE59F201C,
        0xE5812000,
        0xE59FF018,
        PART_LNX,
        IMAGE_ADDRESS,
        image_length,
        probe.WRITER_ENTRY,
        RESULT_ADDRESS,
        DISPATCH_ENTRY,
        tool.UNKNOWN_COMMAND_HANDLER,
        probe.ORIGINAL_INQUIRY_HANDLER,
    ]
    return struct.pack("<%dI" % len(words), *words)


def read_nand_chunk(dev, ep_out, ep_in, offset, length):
    transport._write(
        dev,
        ep_out,
        transport._command(
            transport.UCMD_MAGIC,
            transport.CMD_READY_TO_SEND,
            transport.FLAG_COMMAND,
            1,
            offset,
            length,
        ),
        "GET NAND @%08X" % offset,
    )
    actual_length, expected_crc = transport._response(
        dev, ep_in, "GET NAND metadata @%08X" % offset
    )
    if actual_length != length:
        raise RuntimeError(
            "NAND read length mismatch at 0x%08X: %d/%d"
            % (offset, actual_length, length)
        )
    transport._write(
        dev,
        ep_out,
        transport._command(
            transport.UCMD_MAGIC,
            transport.CMD_SEND_DATA,
            transport.FLAG_COMMAND,
            1,
        ),
        "SEND NAND @%08X" % offset,
    )
    data = transport._read_exact(dev, ep_in, length, "NAND @%08X" % offset)
    transport._response(dev, ep_in, "NAND completion @%08X" % offset)
    if (binascii.crc32(data) & 0xFFFFFFFF) != expected_crc:
        raise RuntimeError("NAND transfer CRC mismatch at 0x%08X" % offset)
    return data


def verify_nand(dev, ep_out, ep_in, payload):
    stock = probe.read_memory(
        dev,
        ep_out,
        ep_in,
        RAW_GET_PATCH_OFFSET,
        len(RAW_GET_STOCK_WORDS) * 4,
        "raw GET patch preflight",
    )
    expected_stock = struct.pack("<7I", *RAW_GET_STOCK_WORDS)
    expected_patch = struct.pack("<7I", *RAW_GET_PATCH_WORDS)
    if stock not in (expected_stock, expected_patch):
        raise RuntimeError("Unexpected instructions at raw NAND GET patch site")
    if stock != expected_patch:
        probe.upload_blob(
            dev,
            ep_out,
            ep_in,
            RAW_GET_PATCH_OFFSET,
            expected_patch,
            "raw NAND GET patch",
        )
    actual_patch = probe.read_memory(
        dev,
        ep_out,
        ep_in,
        RAW_GET_PATCH_OFFSET,
        len(expected_patch),
        "raw GET patch verify",
    )
    if actual_patch != expected_patch:
        raise RuntimeError("Raw NAND GET patch readback mismatch")

    expected = hashlib.sha256(payload).hexdigest()
    digest = hashlib.sha256()
    done = 0
    while done < len(payload):
        count = min(RAW_READ_CHUNK, len(payload) - done)
        digest.update(
            read_nand_chunk(
                dev, ep_out, ep_in, PARTITION_OFFSET + done, count
            )
        )
        done += count
        print(
            "\rReading rootfs back: %6.2f%%" % (100.0 * done / len(payload)),
            end="",
            flush=True,
        )
    print()
    actual = digest.hexdigest()
    if actual != expected:
        raise RuntimeError(
            "NAND readback SHA256 mismatch: expected %s got %s" % (expected, actual)
        )
    return actual


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    parser.add_argument("--expected-sha256")
    parser.add_argument("--backup-dir", type=Path, default=Path("."))
    parser.add_argument(
        "--resume-ddr",
        action="store_true",
        help="reuse a previously uploaded package after its completion response was consumed",
    )
    args = parser.parse_args()

    full, payload = prepare_payload(args.image)
    program_payload = prepare_program_payload(payload)
    full_sha = hashlib.sha256(full).hexdigest()
    payload_sha = hashlib.sha256(payload).hexdigest()
    if args.expected_sha256 and full_sha.lower() != args.expected_sha256.lower():
        raise SystemExit(
            "Artifact SHA256 mismatch: expected %s got %s"
            % (args.expected_sha256, full_sha)
        )

    print("Ambarella S2Lm rootfs-only partition writer")
    print("============================================")
    print("Artifact:       %s" % args.image)
    print("Artifact size:  0x%X" % len(full))
    print("Artifact SHA:   %s" % full_sha)
    print("Payload size:   0x%X (%d eraseblocks)" % (len(payload), len(payload) // ERASEBLOCK))
    print("Payload SHA:    %s" % payload_sha)
    print(
        "Program size:   0x%X (%d contiguous UBI eraseblocks)"
        % (len(program_payload), len(payload) // ERASEBLOCK)
    )
    print("Program SHA:    %s" % hashlib.sha256(program_payload).hexdigest())
    print("Reserved tail:  0x%X" % (PARTITION_SIZE - len(program_payload)))

    dev = tool.find_device(tool.PID_BLD)
    if dev is None:
        raise SystemExit("Running BLD 4255:0001 not found")
    tool.show_device(dev)
    ep_out, ep_in = transport._bulk_endpoints(dev)

    fingerprint = probe.read_memory(
        dev,
        ep_out,
        ep_in,
        probe.WRITER_ENTRY,
        len(probe.WRITER_FINGERPRINT),
        "writer fingerprint",
    )
    if fingerprint != probe.WRITER_FINGERPRINT:
        raise RuntimeError("Partition-writer fingerprint mismatch")

    install_camera_lnx_layout(dev, ep_out, ep_in)
    print(
        "RAM LNX layout: block %d, %d blocks (0x%08X..0x%08X): OK"
        % (
            CAMERA_LNX_START_BLOCK,
            CAMERA_LNX_BLOCKS,
            PARTITION_OFFSET,
            PARTITION_OFFSET + PARTITION_SIZE,
        )
    )

    ptb_before = get_ptb(dev, ep_out, ep_in)
    old = lnx_metadata(ptb_before)
    if old["magic"] != FLPART_MAGIC:
        raise RuntimeError("Current LNX PTB magic is invalid")
    args.backup_dir.mkdir(parents=True, exist_ok=True)
    ptb_path = args.backup_dir / "ptb_before_rootfs_write_v17.bin"
    ptb_path.write_bytes(ptb_before)
    print("PTB backup:     %s" % ptb_path)
    print(
        "Current LNX:    len=0x%X ver=0x%08X date=0x%08X flag=0x%08X"
        % (old["img_len"], old["ver_num"], old["ver_date"], old["flag"])
    )

    package = build_header(program_payload, old) + program_payload
    package_sha = hashlib.sha256(package).hexdigest()
    wrapper = build_wrapper(len(package))
    print("Package size:   0x%X" % len(package))
    print("Package SHA:    %s" % package_sha)
    print("\nPre-write phase: DDR only; NAND is not touched yet.")

    if args.resume_ddr:
        print("Reusing the already uploaded DDR package.")
    else:
        upload_stream(
            dev,
            ep_out,
            ep_in,
            TRANSFER_ADDRESS,
            b"\xFF" * TRANSFER_PREFIX + package,
            "rootfs package with sacrificial prefix",
        )
    dev, ep_out, ep_in = reopen_bld(dev)
    ddr_sha = read_memory_chunked(
        dev, ep_out, ep_in, IMAGE_ADDRESS, len(package), "rootfs package in DDR"
    )
    if ddr_sha != package_sha:
        raise RuntimeError(
            "DDR package SHA mismatch: expected %s got %s" % (package_sha, ddr_sha)
        )
    print("DDR package SHA256: OK")

    probe.upload_blob(dev, ep_out, ep_in, WRAPPER_ADDRESS, wrapper, "ARM wrapper")
    probe.upload_blob(
        dev,
        ep_out,
        ep_in,
        RESULT_ADDRESS,
        struct.pack("<I", RESULT_SENTINEL),
        "result sentinel",
    )
    if probe.read_memory(
        dev, ep_out, ep_in, WRAPPER_ADDRESS, len(wrapper), "wrapper verify"
    ) != wrapper:
        raise RuntimeError("Wrapper DDR readback mismatch")

    answer = input(
        "\nFINAL CHECK: this will erase/program ONLY PART_LNX/rootfs.\n"
        "Type exactly WRITE ROOTFS to continue: "
    )
    if answer != "WRITE ROOTFS":
        raise SystemExit("Cancelled before NAND write")

    probe.upload_blob(
        dev,
        ep_out,
        ep_in,
        DISPATCH_ENTRY,
        struct.pack("<I", WRAPPER_ADDRESS),
        "temporary cmd 4 dispatcher",
    )
    print("Programming rootfs through stock bad-block-aware writer ...", flush=True)
    transport._write(
        dev,
        ep_out,
        transport._command(transport.UCMD_MAGIC, CMD_WRITE, 1),
        "rootfs writer dispatch",
    )
    response_long(dev, ep_in, "rootfs writer dispatch")
    result_raw = probe.read_memory(
        dev, ep_out, ep_in, RESULT_ADDRESS, 4, "writer result"
    )
    unsigned = struct.unpack("<I", result_raw)[0]
    result = unsigned if unsigned < 0x80000000 else unsigned - (1 << 32)
    print("Writer returned: %d (%s)" % (result, ERRORS.get(result, "unknown")))
    if result != 0:
        raise RuntimeError("Rootfs writer failed with %d" % result)

    ptb_after = get_ptb(dev, ep_out, ep_in)
    after = lnx_metadata(ptb_after)
    ptb_after_path = args.backup_dir / "ptb_after_rootfs_write_v17.bin"
    ptb_after_path.write_bytes(ptb_after)
    expected_crc = binascii.crc32(program_payload) & 0xFFFFFFFF
    if after["img_len"] != len(program_payload) or after["crc32"] != expected_crc:
        raise RuntimeError(
            "PTB LNX metadata mismatch after write: len=0x%X crc=0x%08X"
            % (after["img_len"], after["crc32"])
        )
    print("PTB LNX metadata: OK")

    report = {
        "format": "s2lm-rootfs-write-v17",
        "artifact": str(args.image),
        "artifact_size": len(full),
        "artifact_sha256": full_sha,
        "payload_size": len(payload),
        "payload_sha256": payload_sha,
        "program_payload_size": len(program_payload),
        "program_payload_sha256": hashlib.sha256(program_payload).hexdigest(),
        "partition": "PART_LNX",
        "partition_offset": PARTITION_OFFSET,
        "partition_size": PARTITION_SIZE,
        "ram_bld_layout_patched": True,
        "writer_result": result,
        "verification_pending_cold_loader_restart": True,
        "ptb_before_sha256": hashlib.sha256(ptb_before).hexdigest(),
        "ptb_after_sha256": hashlib.sha256(ptb_after).hexdigest(),
    }
    report_path = args.backup_dir / "rootfs_write_v17_report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("Report: %s" % report_path)
    print("ROOTFS_WRITE_OK")
    print(
        "The stock writer uses low DDR as scratch space. Cold-start a fresh "
        "RAM loader and run s2lm_libusb0_rootfs_verify_v18.py for readback."
    )

    try:
        usb.util.dispose_resources(dev)
    except Exception:
        pass


if __name__ == "__main__":
    main()
