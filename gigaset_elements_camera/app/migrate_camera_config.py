#!/usr/bin/env python3
"""Move legacy Supervisor camera options to the visual editor's data file."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def camera_mapping(value: dict) -> dict[str, str]:
    return {
        "name": str(value.get("name") or value.get("camera_name") or "Gigaset Camera"),
        "ip": str(value.get("ip") or value.get("camera_ip") or ""),
        "mac": str(value.get("mac") or value.get("camera_mac") or ""),
        "user": str(value.get("user") or value.get("camera_user") or "admin"),
        "password": str(value.get("password") or value.get("camera_password") or ""),
        "token": str(value.get("token") or value.get("http_token") or ""),
    }


def migrate(options_path: Path, cameras_path: Path) -> bool:
    if cameras_path.exists():
        return False
    data = json.loads(options_path.read_text(encoding="utf-8"))
    cameras = [camera_mapping(value) for value in data.get("cameras", [])]
    if not cameras and data.get("camera_ip") and data.get("camera_mac"):
        cameras = [camera_mapping(data)]
    cameras_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = cameras_path.with_suffix(cameras_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"cameras": cameras}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, cameras_path)
    return bool(cameras)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("options", type=Path)
    parser.add_argument("cameras", type=Path)
    args = parser.parse_args()
    migrated = migrate(args.options, args.cameras)
    print("Migrated existing camera configuration." if migrated else "Camera configuration ready.")


if __name__ == "__main__":
    main()
