"""Prepare the stock S2L camera USB service disk for Wi-Fi provisioning.

The stock S30851-H2531-R101 firmware exposes a small FAT USB disk during a
normal boot.  If the disk contains ``ycam_autorun.sh``, the camera executes it
before starting wpa_supplicant.  This module creates a one-shot autorun which
writes the camera's normal ``/var/wifi/wifi.conf`` and then reboots.

No camera password or Wi-Fi password is sent over the network or stored in the
repository.  The generated service-disk script necessarily contains the Wi-Fi
password until the camera consumes and removes it on the next boot.
"""

from __future__ import annotations

import argparse
import getpass
import os
import shutil
import string
import sys
from pathlib import Path


AUTORUN_NAME = "ycam_autorun.sh"
STATUS_NAME = "wifi_provisioning.txt"


def _wpa_quote(value: str, *, field: str, max_bytes: int) -> str:
    if not value:
        raise ValueError(f"{field} must not be empty")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ValueError(f"{field} must not contain control characters")
    if len(value.encode("utf-8")) > max_bytes:
        raise ValueError(f"{field} is longer than {max_bytes} UTF-8 bytes")
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_autorun(ssid: str, password: str) -> str:
    """Return the LF-only one-shot shell script used by the stock firmware."""

    quoted_ssid = _wpa_quote(ssid, field="SSID", max_bytes=32)
    quoted_password = _wpa_quote(password, field="WPA2 password", max_bytes=63)
    if len(password.encode("utf-8")) < 8:
        raise ValueError("WPA2 password must be at least 8 UTF-8 bytes")

    return f"""#!/bin/sh
set -eu
umask 077

CONFIG_DIR=/var/wifi
CONFIG_FILE=/var/wifi/wifi.conf
CONFIG_TMP=/var/wifi/wifi.conf.new
SERVICE_DIR=/mnt/mass_storage_folder

/bin/mkdir -p \"$CONFIG_DIR\"
/bin/cat > \"$CONFIG_TMP\" <<'GIGASET_WIFI_EOF'
ctrl_interface=/var/wifi/wpa_supplicant
p2p_disabled=1
network={{
proto=WPA WPA2
ssid=\"{quoted_ssid}\"
key_mgmt=WPA-PSK
pairwise=CCMP TKIP
group=CCMP TKIP
psk=\"{quoted_password}\"
}}
GIGASET_WIFI_EOF

/bin/chmod 600 \"$CONFIG_TMP\"
/bin/mv -f \"$CONFIG_TMP\" \"$CONFIG_FILE\"
/bin/echo "Wi-Fi configuration saved; camera is rebooting." > \"$SERVICE_DIR/{STATUS_NAME}\"
/bin/rm -f \"$SERVICE_DIR/{AUTORUN_NAME}\"
/bin/sync
/bin/sleep 2
/sbin/reboot -f
"""


def normalize_drive(value: str) -> Path:
    value = value.strip().strip('"')
    if os.name == "nt" and len(value) == 2 and value[1] == ":":
        value += "\\"
    path = Path(value).resolve()
    if not path.is_dir():
        raise ValueError(f"service disk does not exist or is not mounted: {path}")
    return path


def small_removable_candidates() -> list[tuple[Path, int]]:
    """Return mounted 1-16 MiB volumes, matching the stock 3 MiB service disk."""

    candidates: list[tuple[Path, int]] = []
    if os.name != "nt":
        return candidates
    for letter in string.ascii_uppercase:
        root = Path(f"{letter}:\\")
        if not root.is_dir():
            continue
        try:
            size = shutil.disk_usage(root).total
        except OSError:
            continue
        if 1 * 1024 * 1024 <= size <= 16 * 1024 * 1024:
            candidates.append((root, size))
    return candidates


def write_autorun(drive: Path, ssid: str, password: str, *, replace: bool = False) -> Path:
    destination = drive / AUTORUN_NAME
    if destination.exists() and not replace:
        raise FileExistsError(
            f"{destination} already exists; inspect it or use --replace explicitly"
        )
    payload = build_autorun(ssid, password)
    destination.write_bytes(payload.encode("utf-8"))
    if destination.read_bytes() != payload.encode("utf-8"):
        raise OSError("service-disk read-back verification failed")
    return destination


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Configure Wi-Fi through the newer Gigaset camera's USB service disk."
    )
    parser.add_argument("--list", action="store_true", help="list likely camera service disks")
    parser.add_argument("--drive", help="camera service disk, for example E:")
    parser.add_argument("--ssid", help="2.4 GHz Wi-Fi SSID")
    parser.add_argument(
        "--password",
        help="WPA2 password (omit this option to enter it without terminal echo)",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="replace an existing ycam_autorun.sh on the selected disk",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.list:
        candidates = small_removable_candidates()
        if not candidates:
            print("No likely 1-16 MiB camera service disk is mounted.")
            return 1
        for path, size in candidates:
            print(f"{path}  {size / (1024 * 1024):.1f} MiB")
        return 0

    if not args.drive or not args.ssid:
        _parser().error("--drive and --ssid are required unless --list is used")

    password = args.password
    if password is None:
        password = getpass.getpass("WPA2 password: ")
        confirmation = getpass.getpass("Repeat WPA2 password: ")
        if password != confirmation:
            print("Passwords do not match.", file=sys.stderr)
            return 2

    try:
        drive = normalize_drive(args.drive)
        destination = write_autorun(drive, args.ssid, password, replace=args.replace)
    except (ValueError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2

    print(f"Prepared {destination}")
    print("Safely eject the service disk and power-cycle the camera normally.")
    print("The camera will save Wi-Fi, remove the autorun and reboot once more.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
