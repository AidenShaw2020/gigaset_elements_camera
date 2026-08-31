"""Program S30851-H2531 rootfs one verified NAND eraseblock at a time.

This recovery writer bypasses ``flprog_write_partition`` (whose generic-board
initialisation path can return success after erase/PTB update without durable
image data).  It calls the stock NAND bad-block, erase and page-program
routines directly.  Every 128 KiB semantic UBI PEB is verified in DDR before
programming and read back immediately afterwards.
"""

import argparse
import hashlib
import json
from pathlib import Path
import struct

import usb.util

import s2lm_libusb0_partwriter_probe_v16 as probe
import s2lm_libusb0_rootfs_writer_v17 as writer


NAND_IS_BAD_BLOCK = 0x0000B87C
NAND_ERASE_BLOCK = 0x0000C308
NAND_PROG_PAGES = 0x0000B060
FUNCTION_FINGERPRINTS = {
    NAND_IS_BAD_BLOCK: bytes.fromhex("10402de901db4de21f408de20010a0e3"),
    NAND_ERASE_BLOCK: bytes.fromhex("00219fe5013aa0e300304ee30215a0e3"),
    NAND_PROG_PAGES: bytes.fromhex("f04f2de903a0a0e1a8429fe504d04de2"),
}

ROOTFS_START_BLOCK = 149
ROOTFS_BLOCKS = 528
PAGES_PER_BLOCK = 64
NAND_PAGE_SIZE = 0x800
# The NAND controller DMA is proven coherent with the BLD huge buffer at
# 0x0C000000 (the stock NAND read/program paths use this region).  Generic DDR
# at 0x02000000 is CPU-readable but produced erased data through NAND DMA.
# Keep a sacrificial command prefix, then place one complete eraseblock here.
BLOCK_TRANSFER_ADDRESS = 0x0C000000
BLOCK_TRANSFER_PREFIX = 0x1000
BLOCK_BUFFER_ADDRESS = BLOCK_TRANSFER_ADDRESS + BLOCK_TRANSFER_PREFIX
BAD_BLOCK_FLAGS = 0x7


def build_wrapper(block, page_start=0, page_count=PAGES_PER_BLOCK, erase=True):
    # Calls nand_is_bad_block(block).  A flagged block is returned untouched.
    # Otherwise optionally calls nand_erase_block(block), followed on success
    # by nand_prog_pages(block, page_start, page_count, source, NULL). The result is
    # stored and command 4 is restored before returning through the normal USB
    # inquiry handler.
    if not 0 <= page_start < PAGES_PER_BLOCK:
        raise ValueError("page_start outside NAND eraseblock")
    if not 1 <= page_count <= PAGES_PER_BLOCK - page_start:
        raise ValueError("page_count outside NAND eraseblock")
    source_address = BLOCK_BUFFER_ADDRESS + page_start * NAND_PAGE_SIZE
    if erase:
        erase_words = [
            0xE59F0054,  # ldr r0, block
            0xE59FC060,  # ldr r12, nand_erase_block
            0xE12FFF3C,  # blx r12
            0xE3500000,  # cmp r0, #0
            0x1A00000B,  # bne store_result
        ]
    else:
        erase_words = [0xE1A00000] * 5  # nop; retain fixed literal/branch layout
    words = [
        0xE59F0068,  # ldr r0, block
        0xE59FC070,  # ldr r12, nand_is_bad_block
        0xE12FFF3C,  # blx r12
        0xE3100007,  # tst r0, #BAD_BLOCK_FLAGS
        0x1A000010,  # bne store_result
        *erase_words,
        0xE59F0040,  # ldr r0, block
        0xE3A01000 | page_start,  # mov r1, #page_start (0..63)
        0xE59F203C,  # ldr r2, pages
        0xE59F303C,  # ldr r3, source
        0xE24DD008,  # sub sp, sp, #8 (AAPCS stack argument/alignment)
        0xE3A0C000,  # mov r12, #0
        0xE58DC000,  # str r12, [sp] (spare pointer = NULL)
        0xE3A0C001,  # mov r12, #1
        0xE58DC004,  # str r12, [sp,#4] (ECC/program mode = 1)
        0xE59FC030,  # ldr r12, nand_prog_pages
        0xE12FFF3C,  # blx r12
        0xE28DD008,  # add sp, sp, #8
        0xE59F1028,  # store_result: ldr r1, result address
        0xE5810000,  # str r0, [r1]
        0xE59F1024,  # ldr r1, dispatcher entry
        0xE59F2024,  # ldr r2, unknown-command handler
        0xE5812000,  # str r2, [r1]
        0xE59FF020,  # ldr pc, stock inquiry handler
        block,
        page_count,
        source_address,
        NAND_IS_BAD_BLOCK,
        NAND_ERASE_BLOCK,
        NAND_PROG_PAGES,
        writer.RESULT_ADDRESS,
        writer.DISPATCH_ENTRY,
        writer.tool.UNKNOWN_COMMAND_HANDLER,
        probe.ORIGINAL_INQUIRY_HANDLER,
    ]
    return struct.pack("<%dI" % len(words), *words)


def read_nand_retry(dev, ep_out, ep_in, offset, length):
    last = None
    for _attempt in range(2):
        try:
            return writer.read_nand_chunk(dev, ep_out, ep_in, offset, length)
        except RuntimeError as error:
            last = error
            if "CRC mismatch" not in str(error):
                raise
    raise last


def dispatch_page_batch(
    dev, ep_out, ep_in, physical_block, page_start, page_count, erase
):
    wrapper = build_wrapper(physical_block, page_start, page_count, erase)
    probe.upload_blob(
        dev, ep_out, ep_in, writer.WRAPPER_ADDRESS, wrapper, "raw NAND wrapper"
    )
    probe.upload_blob(
        dev,
        ep_out,
        ep_in,
        writer.RESULT_ADDRESS,
        struct.pack("<I", writer.RESULT_SENTINEL),
        "raw NAND result sentinel",
    )
    probe.upload_blob(
        dev,
        ep_out,
        ep_in,
        writer.DISPATCH_ENTRY,
        struct.pack("<I", writer.WRAPPER_ADDRESS),
        "temporary raw NAND dispatcher",
    )
    writer.transport._write(
        dev,
        ep_out,
        writer.transport._command(
            writer.transport.UCMD_MAGIC, writer.CMD_WRITE, 1
        ),
        "raw NAND page batch dispatch",
    )
    writer.response_long(dev, ep_in, "raw NAND page batch dispatch")
    result_raw = probe.read_memory(
        dev,
        ep_out,
        ep_in,
        writer.RESULT_ADDRESS,
        4,
        "raw NAND page batch result",
    )
    unsigned = struct.unpack("<I", result_raw)[0]
    signed = unsigned if unsigned < 0x80000000 else unsigned - (1 << 32)
    if unsigned & BAD_BLOCK_FLAGS and signed >= 0:
        return "bad", unsigned
    if signed != 0:
        raise RuntimeError(
            "raw NAND block %d pages %d..%d failed with %d"
            % (physical_block, page_start, page_start + page_count - 1, signed)
        )
    return "ok", 0


def program_physical_block(
    dev, ep_out, ep_in, physical_block, data, pages=PAGES_PER_BLOCK, batch_pages=16
):
    if len(data) != writer.ERASEBLOCK:
        raise ValueError("block payload must be exactly one eraseblock")

    writer.upload_stream(
        dev,
        ep_out,
        ep_in,
        BLOCK_TRANSFER_ADDRESS,
        b"\xFF" * BLOCK_TRANSFER_PREFIX + data,
        "semantic PEB data",
    )
    uploaded = writer.read_memory_retry_status1(
        dev,
        ep_out,
        ep_in,
        BLOCK_BUFFER_ADDRESS,
        len(data),
        "semantic PEB DDR verify",
    )
    if uploaded != data:
        raise RuntimeError("DDR eraseblock readback mismatch")

    final_source = writer.read_memory_retry_status1(
        dev,
        ep_out,
        ep_in,
        BLOCK_BUFFER_ADDRESS,
        len(data),
        "semantic PEB final pre-dispatch verify",
    )
    if final_source != data:
        raise RuntimeError("DDR source changed while installing the wrapper")
    if not 1 <= pages <= PAGES_PER_BLOCK:
        raise ValueError("pages must be in range 1..64")
    if not 1 <= batch_pages < 32:
        raise ValueError("batch_pages must be in range 1..31 to stay below 64 KiB")
    page_start = 0
    while page_start < pages:
        page_count = min(batch_pages, pages - page_start)
        state, detail = dispatch_page_batch(
            dev,
            ep_out,
            ep_in,
            physical_block,
            page_start,
            page_count,
            erase=(page_start == 0),
        )
        if state == "bad":
            return state, detail
        byte_start = page_start * NAND_PAGE_SIZE
        byte_length = page_count * NAND_PAGE_SIZE
        actual = read_nand_retry(
            dev,
            ep_out,
            ep_in,
            physical_block * writer.ERASEBLOCK + byte_start,
            byte_length,
        )
        expected = data[byte_start : byte_start + byte_length]
        if actual != expected:
            raise RuntimeError(
                "raw NAND readback mismatch for physical block %d pages %d..%d"
                % (physical_block, page_start, page_start + page_count - 1)
            )
        print(
            "RAW_PAGE_BATCH_OK physical=%d pages=%d..%d"
            % (physical_block, page_start, page_start + page_count - 1)
        )
        page_start += page_count
    return "ok", 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--start-semantic", type=int, default=0)
    parser.add_argument("--start-physical", type=int, default=ROOTFS_START_BLOCK)
    parser.add_argument("--blocks", type=int)
    parser.add_argument(
        "--test-pages",
        type=int,
        help="destructive diagnostic: write only this many pages of one block",
    )
    parser.add_argument("--batch-pages", type=int, default=16)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    full, payload = writer.prepare_payload(args.image)
    artifact_sha = hashlib.sha256(full).hexdigest()
    if artifact_sha.lower() != args.expected_sha256.lower():
        raise SystemExit("Artifact SHA256 mismatch: %s" % artifact_sha)
    if len(payload) % writer.ERASEBLOCK:
        raise SystemExit("Rootfs payload is not eraseblock aligned")
    semantic_total = len(payload) // writer.ERASEBLOCK
    if not 0 <= args.start_semantic < semantic_total:
        raise SystemExit("--start-semantic is outside the rootfs payload")
    if not ROOTFS_START_BLOCK <= args.start_physical < ROOTFS_START_BLOCK + ROOTFS_BLOCKS:
        raise SystemExit("--start-physical is outside PART_LNX")
    requested = semantic_total - args.start_semantic
    if args.blocks is not None:
        if args.blocks <= 0:
            raise SystemExit("--blocks must be positive")
        requested = min(requested, args.blocks)
    if args.test_pages is not None:
        if not 1 <= args.test_pages <= PAGES_PER_BLOCK:
            raise SystemExit("--test-pages must be in range 1..64")
        if requested != 1:
            raise SystemExit("--test-pages requires --blocks 1")
    if not 1 <= args.batch_pages < 32:
        raise SystemExit("--batch-pages must be in range 1..31")

    dev = writer.tool.find_device(writer.tool.PID_BLD)
    if dev is None:
        raise SystemExit("Fresh RAM BLD 4255:0001 not found")
    writer.tool.show_device(dev)
    ep_out, ep_in = writer.transport._bulk_endpoints(dev)
    for address, expected in FUNCTION_FINGERPRINTS.items():
        actual = probe.read_memory(
            dev, ep_out, ep_in, address, len(expected), "NAND function fingerprint"
        )
        if actual != expected:
            raise RuntimeError("NAND function mismatch at 0x%08X" % address)
    print("Stock NAND function fingerprints: OK")
    print(
        "Plan: semantic %d..%d, physical start %d, PART_LNX limit %d"
        % (
            args.start_semantic,
            args.start_semantic + requested - 1,
            args.start_physical,
            ROOTFS_START_BLOCK + ROOTFS_BLOCKS - 1,
        )
    )
    answer = input(
        "Type exactly WRITE ROOTFS RAW to erase/program these verified blocks: "
    )
    if answer != "WRITE ROOTFS RAW":
        raise SystemExit("Cancelled before NAND write")

    semantic = args.start_semantic
    physical = args.start_physical
    target = args.start_semantic + requested
    skipped = []
    while semantic < target:
        if physical >= ROOTFS_START_BLOCK + ROOTFS_BLOCKS:
            raise RuntimeError("PART_LNX exhausted before payload completed")
        data = payload[
            semantic * writer.ERASEBLOCK : (semantic + 1) * writer.ERASEBLOCK
        ]
        state, detail = program_physical_block(
            dev,
            ep_out,
            ep_in,
            physical,
            data,
            pages=args.test_pages or PAGES_PER_BLOCK,
            batch_pages=args.batch_pages,
        )
        if state == "bad":
            print(
                "Physical block %d is marked bad (flags 0x%X); skipped"
                % (physical, detail)
            )
            skipped.append(physical)
            physical += 1
            continue
        if args.test_pages is not None:
            print(
                "RAW_PAGE_TEST_OK semantic=%d physical=%d pages=%d"
                % (semantic, physical, args.test_pages)
            )
            return
        semantic += 1
        physical += 1
        print(
            "RAW_ROOTFS_BLOCK_OK semantic=%d physical=%d (%d/%d)"
            % (semantic - 1, physical - 1, semantic - args.start_semantic, requested)
        )

    report = {
        "format": "s2lm-rootfs-raw-writer-v21",
        "artifact": str(args.image),
        "artifact_sha256": artifact_sha,
        "start_semantic": args.start_semantic,
        "start_physical": args.start_physical,
        "completed_semantic": semantic,
        "next_physical": physical,
        "bad_blocks_skipped": skipped,
        "verified_each_block": True,
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print("Report: %s" % args.report)
    print("ROOTFS_RAW_WRITE_OK")

    try:
        usb.util.dispose_resources(dev)
    except Exception:
        pass


if __name__ == "__main__":
    main()
