"""Replace only the MEF region in a verified 8 MiB camera flash image."""

import argparse
import hashlib
from pathlib import Path


FLASH_SIZE = 8 * 1024 * 1024
MEF_OFFSET = 0x20000
PARTITION1_END = 0x7E0000


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("flash", type=Path)
    parser.add_argument("mef", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--partition-output", type=Path)
    args = parser.parse_args()

    flash = bytearray(args.flash.read_bytes())
    mef = args.mef.read_bytes()
    if len(flash) != FLASH_SIZE or flash[:6] != b"GM8126":
        raise ValueError("input is not a verified 8 MiB GM8126 flash image")
    if flash[MEF_OFFSET : MEF_OFFSET + 4] != b"MEF\x7f" or mef[:4] != b"MEF\x7f":
        raise ValueError("MEF marker missing")
    end = MEF_OFFSET + len(mef)
    if end > 0x7E0000:
        raise ValueError("MEF overlaps persistent configuration partitions")

    original_prefix = bytes(flash[:MEF_OFFSET])
    original_suffix = bytes(flash[end:])
    flash[MEF_OFFSET:end] = mef
    if bytes(flash[:MEF_OFFSET]) != original_prefix or bytes(flash[end:]) != original_suffix:
        raise AssertionError("data outside MEF changed")
    args.output.write_bytes(flash)
    print(
        f"wrote {args.output} size={len(flash)} changed_range="
        f"0x{MEF_OFFSET:X}-0x{end:X} sha256={hashlib.sha256(flash).hexdigest()}"
    )
    if args.partition_output:
        partition = bytes(flash[MEF_OFFSET:PARTITION1_END])
        if len(partition) != PARTITION1_END - MEF_OFFSET:
            raise AssertionError("partition 1 has an unexpected size")
        args.partition_output.write_bytes(partition)
        print(
            f"wrote {args.partition_output} size={len(partition)} "
            f"range=0x{MEF_OFFSET:X}-0x{PARTITION1_END - 1:X} "
            f"sha256={hashlib.sha256(partition).hexdigest()}"
        )


if __name__ == "__main__":
    main()
