"""Prepare the newer camera's stock USB service disk for local-gateway install.

Only original source files from this repository are copied. The camera runs the
one-shot installer as root through its stock ``ycam_autorun.sh`` mechanism,
backs up the two modified configuration files, validates lighttpd, and reboots.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

try:
    from .wifi_provision import AUTORUN_NAME, normalize_drive, small_removable_candidates
except ImportError:  # Direct invocation: python ambarella_s2l/local_gateway_install.py
    from wifi_provision import AUTORUN_NAME, normalize_drive, small_removable_candidates


PAYLOAD_DIR_NAME = "gigaset_local_gateway"
STATUS_NAME = "gigaset_local_gateway_install.txt"
FAILED_AUTORUN_NAME = "ycam_autorun.failed"
INSTALLER_VERSION = 1


def build_autorun() -> str:
    return f'''#!/bin/sh
PATH=/sbin:/bin:/usr/sbin:/usr/bin:/usr/local/bin
SERVICE_DIR=/mnt/mass_storage_folder
PAYLOAD_DIR="$SERVICE_DIR/{PAYLOAD_DIR_NAME}"
STATUS_FILE="$SERVICE_DIR/{STATUS_NAME}"
FAILED_AUTORUN="$SERVICE_DIR/{FAILED_AUTORUN_NAME}"

/bin/echo "Installing Gigaset elements camera local gateway..." > "$STATUS_FILE"
/bin/mkdir -p /webSvr/web/cgi-bin /webSvr/web/setup >> "$STATUS_FILE" 2>&1

install_ok=0
if /usr/bin/lua "$PAYLOAD_DIR/install.lua" >> "$STATUS_FILE" 2>&1
then
        if /bin/chmod 755 /usr/local/bin/cec_init.sh /usr/local/bin/gigaset_local_gateway.sh /webSvr/web/cgi-bin/wifi_setup.cgi >> "$STATUS_FILE" 2>&1
        then
                if /bin/chmod 644 /webSvr/web/setup/index.html >> "$STATUS_FILE" 2>&1
                then
                        if /usr/sbin/lighttpd -tt -f /etc/lighttpd/lighttpd.conf >> "$STATUS_FILE" 2>&1
                        then
                                install_ok=1
                        fi
                fi
        fi
fi

if [ "$install_ok" = 1 ]
then
        /bin/echo "Installation verified. Camera is rebooting." >> "$STATUS_FILE"
        /bin/rm -f "$SERVICE_DIR/{AUTORUN_NAME}" "$FAILED_AUTORUN"
        /bin/rm -rf "$PAYLOAD_DIR"
        /bin/sync
        /bin/sleep 2
        /sbin/reboot -f
        exit 0
fi

/bin/echo "Installation failed; restoring stock configuration." >> "$STATUS_FILE"
if [ -f /usr/local/bin/cec_init.sh.gigaset-stock ]; then
        /bin/cp -f /usr/local/bin/cec_init.sh.gigaset-stock /usr/local/bin/cec_init.sh
fi
if [ -f /etc/lighttpd/lighttpd.conf.gigaset-stock ]; then
        /bin/cp -f /etc/lighttpd/lighttpd.conf.gigaset-stock /etc/lighttpd/lighttpd.conf
fi
/bin/chmod 755 /usr/local/bin/cec_init.sh
/bin/chmod 644 /etc/lighttpd/lighttpd.conf
/bin/rm -f /usr/local/bin/gigaset_local_gateway.sh /webSvr/web/cgi-bin/wifi_setup.cgi /webSvr/web/setup/index.html
/bin/rm -f "$FAILED_AUTORUN"
/bin/mv "$SERVICE_DIR/{AUTORUN_NAME}" "$FAILED_AUTORUN"
/bin/echo "Stock files restored. Inspect this log before retrying." >> "$STATUS_FILE"
/bin/sync
exit 1
'''


def _source_files(source_root: Path) -> dict[str, bytes]:
    overlay = source_root / "rootfs_overlay"
    paths = {
        "install.lua": source_root / "camera_install.lua",
        "gigaset_local_gateway.sh": overlay / "usr/local/bin/gigaset_local_gateway.sh",
        "wifi_setup.cgi": overlay / "webSvr/web/cgi-bin/wifi_setup.cgi",
        "index.html": overlay / "webSvr/web/setup/index.html",
    }
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError("missing installer source: " + ", ".join(missing))
    return {name: path.read_bytes() for name, path in paths.items()}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def prepare_install(
    drive: Path,
    *,
    replace: bool = False,
    source_root: Path | None = None,
) -> list[Path]:
    source_root = source_root or Path(__file__).resolve().parent
    autorun = drive / AUTORUN_NAME
    payload_dir = drive / PAYLOAD_DIR_NAME
    failed_autorun = drive / FAILED_AUTORUN_NAME
    occupied = [path for path in (autorun, payload_dir) if path.exists()]
    if occupied and not replace:
        raise FileExistsError(
            "installer files already exist: "
            + ", ".join(str(path) for path in occupied)
            + "; inspect them or use --replace explicitly"
        )

    if replace:
        if autorun.exists():
            autorun.unlink()
        if payload_dir.exists():
            shutil.rmtree(payload_dir)
        if failed_autorun.exists():
            failed_autorun.unlink()

    sources = _source_files(source_root)
    payload_dir.mkdir(parents=False, exist_ok=False)
    written: list[Path] = []
    for name, data in sources.items():
        destination = payload_dir / name
        destination.write_bytes(data)
        if destination.read_bytes() != data:
            raise OSError(f"service-disk read-back verification failed: {destination}")
        written.append(destination)

    manifest = {
        "format": "gigaset-elements-camera-local-gateway",
        "version": INSTALLER_VERSION,
        "files": {name: _sha256(data) for name, data in sorted(sources.items())},
    }
    manifest_data = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    manifest_path = payload_dir / "manifest.json"
    manifest_path.write_bytes(manifest_data)
    if manifest_path.read_bytes() != manifest_data:
        raise OSError(f"service-disk read-back verification failed: {manifest_path}")
    written.append(manifest_path)

    autorun_data = build_autorun().encode("utf-8")
    autorun.write_bytes(autorun_data)
    if autorun.read_bytes() != autorun_data:
        raise OSError(f"service-disk read-back verification failed: {autorun}")
    written.append(autorun)
    return written


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install the source-only local gateway through the S2L USB service disk."
    )
    parser.add_argument("--list", action="store_true", help="list likely camera service disks")
    parser.add_argument("--drive", help="camera service disk, for example E:")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="replace only an existing local-gateway installer on the selected disk",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.list:
        candidates = small_removable_candidates()
        if not candidates:
            print("No likely 1-16 MiB camera service disk is mounted.")
            return 1
        for path, size in candidates:
            print(f"{path}  {size / (1024 * 1024):.1f} MiB")
        return 0
    if not args.drive:
        parser.error("--drive is required unless --list is used")

    try:
        drive = normalize_drive(args.drive)
        files = prepare_install(drive, replace=args.replace)
    except (FileNotFoundError, FileExistsError, ValueError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2

    print(f"Prepared and read-back verified {len(files)} files on {drive}")
    print("Safely eject the service disk and power-cycle the camera normally.")
    print(f"After installation, reconnect USB and read {STATUS_NAME} for the result.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
