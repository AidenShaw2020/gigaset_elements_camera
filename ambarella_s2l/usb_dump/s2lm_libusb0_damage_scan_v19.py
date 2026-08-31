"""Read-only comparison of camera NAND against a known-good full dump.

This is intended for recovery after a generic Ambarella RAM BLD used the
wrong, SDK-default partition layout.  The only device-side modification is a
temporary patch in DDR which exposes the stock NAND read routine.  NAND and
PTB are never written.
"""

import argparse
import hashlib
from pathlib import Path
import usb.util

import s2lm_libusb0_rootfs_writer_v17 as writer


ERASEBLOCK = 0x20000
SCAN_END = 0x08000000
READ_CHUNK = 4 * 1024 * 1024


def compact_ranges(blocks):
    if not blocks:
        return []
    result = []
    first = previous = blocks[0]
    for block in blocks[1:]:
        if block == previous + 1:
            previous = block
            continue
        result.append((first, previous))
        first = previous = block
    result.append((first, previous))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("reference_dump", type=Path)
    parser.add_argument(
        "--end",
        type=lambda value: int(value, 0),
        default=SCAN_END,
        help="exclusive NAND offset to compare (default: complete 128 MiB)",
    )
    args = parser.parse_args()

    if args.end <= 0 or args.end > SCAN_END or args.end % ERASEBLOCK:
        raise SystemExit("--end must be eraseblock aligned and <= 0x08000000")
    if args.reference_dump.stat().st_size < args.end:
        raise SystemExit("Reference dump is shorter than the requested scan")

    dev = writer.tool.find_device(writer.tool.PID_BLD)
    if dev is None:
        raise SystemExit("Fresh RAM BLD 4255:0001 not found")
    writer.tool.show_device(dev)
    ep_out, ep_in = writer.transport._bulk_endpoints(dev)
    changed = []
    reference_hash = hashlib.sha256()
    camera_hash = hashlib.sha256()
    with args.reference_dump.open("rb") as reference:
        offset = 0
        while offset < args.end:
            length = min(READ_CHUNK, args.end - offset)
            expected = reference.read(length)
            actual = writer.read_nand_chunk(dev, ep_out, ep_in, offset, length)
            reference_hash.update(expected)
            camera_hash.update(actual)
            for local in range(0, length, ERASEBLOCK):
                if actual[local : local + ERASEBLOCK] != expected[local : local + ERASEBLOCK]:
                    changed.append((offset + local) // ERASEBLOCK)
            offset += length
            print(
                "\rRead-only NAND comparison: %6.2f%%"
                % (100.0 * offset / args.end),
                end="",
                flush=True,
            )
    print()
    print("Reference SHA256: %s" % reference_hash.hexdigest())
    print("Camera SHA256:    %s" % camera_hash.hexdigest())
    print("Changed blocks:   %d" % len(changed))
    for first, last in compact_ranges(changed):
        print(
            "  blocks %d..%d  offsets 0x%08X..0x%08X"
            % (first, last, first * ERASEBLOCK, (last + 1) * ERASEBLOCK)
        )
    print("READ_ONLY_DAMAGE_SCAN_OK")

    try:
        usb.util.dispose_resources(dev)
    except Exception:
        pass


if __name__ == "__main__":
    main()
