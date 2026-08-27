#!/usr/bin/env python3
"""Assemble the GM8126 RAM-only flash dumper."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
VENDORED_KEYSTONE = ROOT.parent / "work" / "vendor" / "keystone"
if VENDORED_KEYSTONE.is_dir():
    sys.path.insert(0, str(VENDORED_KEYSTONE))

from keystone import KS_ARCH_ARM, KS_MODE_ARM, KS_MODE_LITTLE_ENDIAN, Ks


def strip_directives(source: str) -> str:
    return "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith(".")
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source", type=Path, default=ROOT / "payload" / "gm8126_flash_dump.s"
    )
    parser.add_argument(
        "--output", type=Path, default=ROOT / "payload" / "gm8126_flash_dump.bin"
    )
    parser.add_argument("--address", type=lambda value: int(value, 0), default=0x8000)
    args = parser.parse_args()

    source = strip_directives(args.source.read_text(encoding="utf-8"))
    assembler = Ks(KS_ARCH_ARM, KS_MODE_ARM | KS_MODE_LITTLE_ENDIAN)
    encoding, count = assembler.asm(source, addr=args.address, as_bytes=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(encoding)
    print(f"assembled={count} bytes={len(encoding)} address=0x{args.address:08x}")
    print(f"output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
