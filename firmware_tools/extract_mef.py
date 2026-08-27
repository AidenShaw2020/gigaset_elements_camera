#!/usr/bin/env python3
"""Extract and validate the camera-owned MEF image from an 8 MiB flash dump."""

from __future__ import annotations

import argparse
import hashlib
import struct
import zlib
from pathlib import Path


FLASH_SIZE = 8 * 1024 * 1024
MEF_OFFSET = 0x20000
MAX_MEF_END = 0x7E0000


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("flash", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    flash = args.flash.read_bytes()
    if len(flash) != FLASH_SIZE or flash[:6] != b"GM8126":
        raise ValueError("input is not a verified 8 MiB GM8126 flash dump")
    if flash[MEF_OFFSET : MEF_OFFSET + 4] != b"MEF\x7f":
        raise ValueError("MEF marker missing at 0x20000")
    length = struct.unpack_from("<I", flash, MEF_OFFSET + 0x38)[0]
    if length < 0x1000 or MEF_OFFSET + length > MAX_MEF_END:
        raise ValueError(f"unsafe MEF length in header: 0x{length:X}")
    mef = bytearray(flash[MEF_OFFSET : MEF_OFFSET + length])
    expected = struct.unpack_from("<I", mef, 0x3C)[0]
    struct.pack_into("<I", mef, 0x3C, 0)
    actual = zlib.crc32(mef) & 0xFFFFFFFF
    if actual != expected:
        raise ValueError(
            f"MEF CRC mismatch: header=0x{expected:08X}, calculated=0x{actual:08X}"
        )
    struct.pack_into("<I", mef, 0x3C, expected)
    args.output.write_bytes(mef)
    print(f"output={args.output}")
    print(f"bytes={len(mef)}")
    print(f"sha256={hashlib.sha256(mef).hexdigest()}")
    print(f"crc32=0x{expected:08X}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
