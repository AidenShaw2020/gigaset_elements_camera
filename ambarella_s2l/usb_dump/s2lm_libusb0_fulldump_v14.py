"""Resume-capable logical NAND dump through the RAM-only v14 BLD.

Only the BLD GET and SEND commands are issued. The BLD dispatcher used with
this script keeps receive/program/erase/PTB-update commands disabled.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import time
import zlib

import s2lm_libusb0_stock_readonly_v11


tool = s2lm_libusb0_stock_readonly_v11.tool
tool.USB_TIMEOUT_MS = 120000
MAX_CHUNK = 8 * 1024 * 1024
DEFAULT_SIZE = 0x08000000
DEFAULT_CHUNK = 0x00400000


def read_chunk(dev, ep_out, ep_in, offset, requested):
    subtype = 1
    tool.bulk_write(
        dev,
        ep_out,
        tool.build_ucmd(tool.CMD_GET, subtype, offset, requested),
        "GET raw NAND command",
    )
    response = tool.bulk_read_exact(dev, ep_in, 16, "GET raw NAND response")
    status, length, expected_crc = tool.parse_ursp(
        response, "GET raw NAND response"
    )
    if status != 0 or length != requested:
        raise RuntimeError(
            "GET failed at 0x%08X: status=0x%08X length=%d expected=%d"
            % (offset, status, length, requested)
        )

    tool.bulk_write(
        dev,
        ep_out,
        tool.build_ucmd(tool.CMD_SEND, subtype),
        "SEND raw NAND command",
    )
    data = tool.bulk_read_exact(dev, ep_in, length, "raw NAND data")
    final = tool.bulk_read_exact(dev, ep_in, 16, "final response")
    final_status, final_length, final_crc = tool.parse_ursp(
        final, "final response"
    )
    if (final_status, final_length, final_crc) != (0, 0, 0):
        raise RuntimeError("Unexpected final response at 0x%08X" % offset)

    actual_crc = zlib.crc32(data) & 0xFFFFFFFF
    if actual_crc != expected_crc:
        raise RuntimeError(
            "CRC mismatch at 0x%08X: device=0x%08X local=0x%08X"
            % (offset, expected_crc, actual_crc)
        )
    return data, actual_crc


def save_manifest(path, values):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(values, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output")
    parser.add_argument("--size", type=lambda value: int(value, 0), default=DEFAULT_SIZE)
    parser.add_argument(
        "--chunk-size", type=lambda value: int(value, 0), default=DEFAULT_CHUNK
    )
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    if args.size <= 0 or args.size > 0x08000000:
        raise SystemExit("Dump size must be between 1 byte and 128 MiB")
    if args.chunk_size <= 0 or args.chunk_size > MAX_CHUNK:
        raise SystemExit("Chunk size must be between 1 byte and 8 MiB")

    output = Path(args.output)
    partial = Path(str(output) + ".part")
    manifest_path = Path(str(output) + ".json")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise SystemExit("Completed output already exists: %s" % output)

    if partial.exists():
        if not args.resume:
            raise SystemExit(
                "Partial dump exists; rerun with --resume: %s" % partial
            )
        offset = partial.stat().st_size
        if offset > args.size:
            raise SystemExit("Partial dump is larger than requested total size")
        if offset != args.size and offset % args.chunk_size:
            raise SystemExit("Partial dump size is not aligned to the chunk size")
    else:
        offset = 0

    dev = tool.find_device(tool.PID_BLD)
    if dev is None:
        raise SystemExit("4255:0001 is not accessible through native libusb0")
    _, ep_out, ep_in = tool.get_bulk_endpoints(dev)

    records = []
    if manifest_path.exists() and args.resume:
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
            records = list(previous.get("chunks", []))
        except (OSError, ValueError, TypeError):
            records = []

    started = time.monotonic()
    mode = "ab" if offset else "wb"
    print(
        "Logical NAND dump: offset=0x%08X total=0x%08X chunk=0x%X"
        % (offset, args.size, args.chunk_size)
    )
    print("Output in progress: %s" % partial)
    print("Only GET and SEND commands will be issued.")

    with partial.open(mode) as handle:
        while offset < args.size:
            length = min(args.chunk_size, args.size - offset)
            chunk_started = time.monotonic()
            data, crc = read_chunk(dev, ep_out, ep_in, offset, length)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            elapsed_chunk = max(time.monotonic() - chunk_started, 0.001)
            records.append(
                {
                    "offset": offset,
                    "length": length,
                    "crc32": "%08X" % crc,
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
            offset += length
            manifest = {
                "format": "s2lm-logical-nand-dump-v14",
                "complete": False,
                "total_size": args.size,
                "chunk_size": args.chunk_size,
                "completed_bytes": offset,
                "chunks": records,
            }
            save_manifest(manifest_path, manifest)
            print(
                "%6.2f%%  0x%08X/0x%08X  CRC32=%08X  %.2f MiB/s"
                % (
                    100.0 * offset / args.size,
                    offset,
                    args.size,
                    crc,
                    length / elapsed_chunk / (1024 * 1024),
                ),
                flush=True,
            )

    os.replace(partial, output)
    digest = hashlib.sha256()
    with output.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    manifest.update(
        {
            "complete": True,
            "completed_bytes": args.size,
            "sha256": digest.hexdigest(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    )
    save_manifest(manifest_path, manifest)
    print("Completed: %s" % output)
    print("SHA256: %s" % digest.hexdigest())
    print("No NAND write-capable command was sent.")


if __name__ == "__main__":
    main()
