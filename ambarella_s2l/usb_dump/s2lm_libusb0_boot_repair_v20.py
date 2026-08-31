"""Restore S30851-H2531 boot partitions from its verified private NAND dump.

The generic Ambarella RAM BLD describes a reference-board partition layout.
This tool fingerprints and patches only its live DDR layout entries, then uses
the stock bad-block-aware partition writer.  The reference dump is never
modified.  PART_LNX/rootfs is intentionally handled separately by the
corrected rootfs writer.
"""

import argparse
import binascii
import hashlib
from pathlib import Path
import struct

import usb.util

import s2lm_libusb0_partwriter_probe_v16 as probe
import s2lm_libusb0_rootfs_writer_v17 as writer


ERASEBLOCK = 0x20000
REFERENCE_SIZE = 0x08000000
REFERENCE_PTB_OFFSET = 0x00160040

PART_BST = 0
PART_BLD = 1
PART_PBA = 4
PART_PRI = 5

# id: (name, physical offset, start block, block count, generic start, generic count)
PARTITIONS = {
    PART_BST: ("BST", 0x00000000, 0, 1, 0, 1),
    PART_BLD: ("BLD", 0x00020000, 1, 10, 1, 10),
    PART_PBA: ("PBA", 0x002A0000, 21, 64, 0, 0),
    PART_PRI: ("PRI", 0x00AA0000, 85, 64, 53, 128),
}
RESTORE_ORDER = (PART_PBA, PART_PRI, PART_BLD, PART_BST)

# Generic fill_partition_sizes() stores zero as PBA's maximum at 0x74DC.
# Changing the source register from r3 (zero) to r2 (64 MiB) only relaxes the
# input validation gate in the RAM copy.  The live table still limits PBA to
# the camera's real 64 eraseblocks.
PBA_MAX_PATCH_ADDRESS = 0x000074DC
PBA_MAX_STOCK_WORD = 0xE5803010
PBA_MAX_PATCH_WORD = 0xE5802010


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def metadata(ptb, part_id):
    keys = ("crc32", "ver_num", "ver_date", "img_len", "mem_addr", "flag", "magic")
    return dict(zip(keys, struct.unpack_from("<7I", ptb, part_id * 28)))


def patch_word(dev, ep_out, ep_in, address, expected, replacement, label):
    current = struct.unpack(
        "<I", probe.read_memory(dev, ep_out, ep_in, address, 4, label)
    )[0]
    if current not in (expected, replacement):
        raise RuntimeError(
            "%s has unexpected value 0x%08X at 0x%08X"
            % (label, current, address)
        )
    if current != replacement:
        probe.upload_blob(
            dev, ep_out, ep_in, address, struct.pack("<I", replacement), label
        )
    verified = struct.unpack(
        "<I", probe.read_memory(dev, ep_out, ep_in, address, 4, label + " verify")
    )[0]
    if verified != replacement:
        raise RuntimeError("%s RAM patch did not persist" % label)


def install_partition_layout(dev, ep_out, ep_in):
    for part_id, values in PARTITIONS.items():
        name, _offset, start, blocks, generic_start, generic_blocks = values
        patch_word(
            dev,
            ep_out,
            ep_in,
            writer.PART_START_TABLE + part_id * 4,
            generic_start,
            start,
            "%s start block" % name,
        )
        patch_word(
            dev,
            ep_out,
            ep_in,
            writer.PART_BLOCKS_TABLE + part_id * 4,
            generic_blocks,
            blocks,
            "%s block count" % name,
        )
    patch_word(
        dev,
        ep_out,
        ep_in,
        PBA_MAX_PATCH_ADDRESS,
        PBA_MAX_STOCK_WORD,
        PBA_MAX_PATCH_WORD,
        "PBA validation maximum",
    )


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
        writer.PARTHD_MAGIC,
    )
    return bytes(header)


def build_wrapper(part_id, package_length):
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
        part_id,
        writer.IMAGE_ADDRESS,
        package_length,
        probe.WRITER_ENTRY,
        writer.RESULT_ADDRESS,
        writer.DISPATCH_ENTRY,
        writer.tool.UNKNOWN_COMMAND_HANDLER,
        probe.ORIGINAL_INQUIRY_HANDLER,
    ]
    return struct.pack("<%dI" % len(words), *words)


def write_partition(dev, ep_out, ep_in, part_id, payload, old, resume_ddr=False):
    name = PARTITIONS[part_id][0]
    package = build_header(payload, old) + payload
    package_sha = hashlib.sha256(package).hexdigest()
    if resume_ddr:
        print("\n%s: reusing package uploaded by the previous process" % name)
    else:
        print("\n%s: uploading 0x%X bytes to DDR" % (name, len(package)))
        writer.upload_stream(
            dev,
            ep_out,
            ep_in,
            writer.TRANSFER_ADDRESS,
            b"\xFF" * writer.TRANSFER_PREFIX + package,
            "%s package with sacrificial prefix" % name,
        )
        dev, ep_out, ep_in = writer.reopen_bld(dev)
    actual_sha = writer.read_memory_chunked(
        dev,
        ep_out,
        ep_in,
        writer.IMAGE_ADDRESS,
        len(package),
        "%s package in DDR" % name,
    )
    if actual_sha != package_sha:
        raise RuntimeError("%s DDR package SHA mismatch" % name)

    wrapper = build_wrapper(part_id, len(package))
    probe.upload_blob(
        dev, ep_out, ep_in, writer.WRAPPER_ADDRESS, wrapper, "%s wrapper" % name
    )
    probe.upload_blob(
        dev,
        ep_out,
        ep_in,
        writer.RESULT_ADDRESS,
        struct.pack("<I", writer.RESULT_SENTINEL),
        "%s result sentinel" % name,
    )
    probe.upload_blob(
        dev,
        ep_out,
        ep_in,
        writer.DISPATCH_ENTRY,
        struct.pack("<I", writer.WRAPPER_ADDRESS),
        "%s temporary cmd 4 dispatcher" % name,
    )
    print("%s: programming through stock partition writer ..." % name, flush=True)
    writer.transport._write(
        dev,
        ep_out,
        writer.transport._command(writer.transport.UCMD_MAGIC, writer.CMD_WRITE, 1),
        "%s writer dispatch" % name,
    )
    writer.response_long(dev, ep_in, "%s writer dispatch" % name)
    result_raw = probe.read_memory(
        dev, ep_out, ep_in, writer.RESULT_ADDRESS, 4, "%s writer result" % name
    )
    unsigned = struct.unpack("<I", result_raw)[0]
    result = unsigned if unsigned < 0x80000000 else unsigned - (1 << 32)
    print("%s writer returned: %d (%s)" % (name, result, writer.ERRORS.get(result, "unknown")))
    if result != 0:
        raise RuntimeError("%s writer failed with %d" % (name, result))

    current_ptb = writer.get_ptb(dev, ep_out, ep_in)
    current = metadata(current_ptb, part_id)
    expected_crc = binascii.crc32(payload) & 0xFFFFFFFF
    if current["img_len"] != len(payload) or current["crc32"] != expected_crc:
        raise RuntimeError("%s PTB metadata mismatch after write" % name)
    print("%s PTB metadata: OK" % name)
    return dev, ep_out, ep_in


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("reference_dump", type=Path)
    parser.add_argument(
        "--expected-sha256",
        required=True,
        help="SHA-256 of this camera's independently verified private dump",
    )
    parser.add_argument("--backup-dir", type=Path, default=Path("."))
    parser.add_argument(
        "--partition", choices=("PBA", "PRI", "BLD", "BST"),
        help="operate on one partition (required for split USB-safe mode)",
    )
    parser.add_argument(
        "--upload-only", action="store_true",
        help="upload the selected package to DDR and exit before verification",
    )
    parser.add_argument(
        "--resume-ddr", action="store_true",
        help="verify the selected package left in DDR by --upload-only, then write",
    )
    args = parser.parse_args()

    if args.upload_only and args.resume_ddr:
        parser.error("--upload-only and --resume-ddr are mutually exclusive")
    if (args.upload_only or args.resume_ddr) and not args.partition:
        parser.error("--partition is required for split USB-safe mode")

    selected_order = RESTORE_ORDER
    if args.partition:
        selected_order = (
            next(
                part_id
                for part_id, values in PARTITIONS.items()
                if values[0] == args.partition
            ),
        )

    if args.reference_dump.stat().st_size != REFERENCE_SIZE:
        raise SystemExit("Reference dump must be exactly 128 MiB")
    digest = sha256_file(args.reference_dump)
    if digest.lower() != args.expected_sha256.lower():
        raise SystemExit("Reference dump SHA256 mismatch: %s" % digest)

    with args.reference_dump.open("rb") as reference:
        reference.seek(REFERENCE_PTB_OFFSET)
        reference_ptb = reference.read(4096)
        payloads = {}
        for part_id in selected_order:
            name, offset, _start, blocks, _generic_start, _generic_blocks = PARTITIONS[part_id]
            old = metadata(reference_ptb, part_id)
            if old["magic"] != writer.FLPART_MAGIC:
                raise RuntimeError("Reference %s PTB magic is invalid" % name)
            if old["img_len"] > blocks * ERASEBLOCK:
                raise RuntimeError("Reference %s image exceeds its partition" % name)
            reference.seek(offset)
            payload = reference.read(old["img_len"])
            if (binascii.crc32(payload) & 0xFFFFFFFF) != old["crc32"]:
                raise RuntimeError("Reference %s payload CRC mismatch" % name)
            payloads[part_id] = (payload, old)
            print(
                "%s reference: offset=0x%08X len=0x%X crc=0x%08X"
                % (name, offset, len(payload), old["crc32"])
            )

    dev = writer.tool.find_device(writer.tool.PID_BLD)
    if dev is None:
        raise SystemExit("Write-capable RAM BLD 4255:0001 not found")
    writer.tool.show_device(dev)
    ep_out, ep_in = writer.transport._bulk_endpoints(dev)
    fingerprint = writer.read_memory_retry_status1(
        dev,
        ep_out,
        ep_in,
        probe.WRITER_ENTRY,
        len(probe.WRITER_FINGERPRINT),
        "writer fingerprint",
    )
    if fingerprint != probe.WRITER_FINGERPRINT:
        raise RuntimeError("Partition-writer fingerprint mismatch")
    install_partition_layout(dev, ep_out, ep_in)
    print("Camera partition layout patched in RAM: OK")

    if args.upload_only:
        part_id = selected_order[0]
        payload, old = payloads[part_id]
        name = PARTITIONS[part_id][0]
        package = build_header(payload, old) + payload
        print("\n%s: uploading 0x%X bytes to DDR" % (name, len(package)))
        writer.upload_stream(
            dev,
            ep_out,
            ep_in,
            writer.TRANSFER_ADDRESS,
            b"\xFF" * writer.TRANSFER_PREFIX + package,
            "%s package with sacrificial prefix" % name,
        )
        print("%s_DDR_UPLOAD_OK" % name)
        print("Exit this process before starting --resume-ddr.")
        try:
            usb.util.dispose_resources(dev)
        except Exception:
            pass
        return

    args.backup_dir.mkdir(parents=True, exist_ok=True)
    ptb_before = writer.get_ptb(dev, ep_out, ep_in)
    (args.backup_dir / "ptb_before_boot_repair_v20.bin").write_bytes(ptb_before)

    selected_names = ", ".join(PARTITIONS[p][0] for p in selected_order)
    answer = input(
        "\nFINAL CHECK: restore %s from the verified private dump.\n"
        "PART_LNX/rootfs and PTB are not written by this command.\n"
        "Type exactly REPAIR BOOT to continue: " % selected_names
    )
    if answer != "REPAIR BOOT":
        raise SystemExit("Cancelled before NAND write")

    for part_id in selected_order:
        payload, old = payloads[part_id]
        dev, ep_out, ep_in = write_partition(
            dev, ep_out, ep_in, part_id, payload, old,
            resume_ddr=args.resume_ddr,
        )

    ptb_after = writer.get_ptb(dev, ep_out, ep_in)
    (args.backup_dir / "ptb_after_boot_repair_v20.bin").write_bytes(ptb_after)
    print("BOOT_PARTITIONS_REPAIR_OK")
    print("Cold-start a fresh RAM loader and verify before normal boot.")

    try:
        usb.util.dispose_resources(dev)
    except Exception:
        pass


if __name__ == "__main__":
    main()
