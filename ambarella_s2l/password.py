#!/usr/bin/env python3
"""Derive the stock local password of the Ambarella S2L camera.

The camera runs ``ycam_password`` at boot with the Ethernet MAC printed by
BusyBox ``ifconfig``.  The generated value is installed for both the ``admin``
HTTP account and the ``root`` system account by the stock startup scripts.

This is an independent source implementation.  It does not contain or read
vendor firmware and does not contact the camera or any online service.
"""

from __future__ import annotations

import argparse
import hashlib
import re
from typing import Sequence


_ALPHABET = "zAyB1xCwD4vEuF3tGsH4rIqJ5pKoL6nMmN7lOkP8jQiR9hSgT0fUeVdWcXbYaZ"
_MAC_RE = re.compile(r"^[0-9A-Fa-f]{12}$")


def normalize_mac(value: str, *, letter_case: str = "upper") -> str:
    """Return a separator-free 12-digit MAC in the requested letter case."""

    compact = re.sub(r"[:-]", "", value.strip())
    if "." in compact:
        compact = compact.replace(".", "")
    if not _MAC_RE.fullmatch(compact):
        raise ValueError(
            "MAC must contain exactly 12 hexadecimal digits, for example "
            "00:11:22:33:44:55"
        )
    if letter_case == "upper":
        return compact.upper()
    if letter_case == "lower":
        return compact.lower()
    if letter_case == "keep":
        return compact
    raise ValueError("letter_case must be 'upper', 'lower' or 'keep'")


def derive_stock_password(mac: str, *, letter_case: str = "upper") -> str:
    """Return the 12-character stock password derived from *mac*.

    The default upper-case representation matches the output of BusyBox
    ``ifconfig`` used by the camera's stock ``Mac_Passwd.sh`` startup script.
    """

    key = normalize_mac(mac, letter_case=letter_case)
    digest = hashlib.sha1(key.encode("ascii")).digest()

    # This byte mixing is a direct, independent translation of the ARM Thumb-2
    # operations in ycam_password.  Each assignment in the original stores a
    # byte, so truncation happens before the modulo operation below.
    mixed = [
        digest[0] + 8 * digest[12],
        digest[1] + 8 * digest[11],
        digest[2] + 8 * digest[10],
        digest[3] + 8 * digest[9],
        digest[4] + 8 * digest[8],
        digest[5] + 8 * digest[7],
        9 * digest[6],
        8 * digest[5] + 65 * digest[7],
        8 * digest[4] + 65 * digest[8],
        8 * digest[3] + 65 * digest[9],
        8 * digest[2] + 65 * digest[10],
        8 * digest[1] + 65 * digest[11],
    ]
    return "".join(_ALPHABET[(value & 0xFF) % 62] for value in mixed)


def htdigest_value(
    password: str, *, username: str = "admin", realm: str = "ycam.com"
) -> str:
    """Return the MD5 HTTP-Digest value used in ``/etc/webpass.txt``."""

    material = f"{username}:{realm}:{password}".encode("utf-8")
    return hashlib.md5(material).hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Derive the stock local admin/root password for the newer "
            "Ambarella S2L Gigaset elements camera."
        )
    )
    parser.add_argument("mac", help="camera Ethernet MAC address")
    parser.add_argument(
        "--mac-case",
        choices=("upper", "lower", "keep"),
        default="upper",
        help="case passed to the stock algorithm (default: upper)",
    )
    parser.add_argument(
        "--digest",
        action="store_true",
        help="also print the admin:ycam.com HTTP-Digest value",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        normalized = normalize_mac(args.mac, letter_case=args.mac_case)
        password = derive_stock_password(normalized, letter_case="keep")
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    print(f"Camera MAC:    {normalized}")
    print("Web user:     admin")
    print(f"Web password: {password}")
    print("Root user:    root")
    print(f"Root password: {password}")
    if args.digest:
        print(f"HTTP digest:  {htdigest_value(password)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
