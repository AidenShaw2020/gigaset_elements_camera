#!/usr/bin/env python3
"""Dump a stock GM8126 camera through MyLoader without writing its flash."""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from pathlib import Path

import serial

from tftp_put import put


ROOT = Path(__file__).resolve().parent
START_MARKER = b"GDMP1\n"
END_MARKER = b"\nEND1\n"
FLASH_SIZE = 8 * 1024 * 1024


def read_until(ser: serial.Serial, needle: bytes, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    received = bytearray()
    while time.monotonic() < deadline:
        chunk = ser.read(4096)
        if chunk:
            received.extend(chunk)
            if needle in received:
                return bytes(received)
            if len(received) > 256 * 1024:
                del received[:128 * 1024]
    raise TimeoutError(f"UART did not produce {needle!r}")


def enter_main_menu(ser: serial.Serial, timeout: float) -> None:
    # MyLoader clears or consumes UART state during early initialization on
    # this board. Start sending ESC as soon as the first-stage `GM8126` banner
    # appears and continue through the short input window.
    read_until(ser, b"GM8126", timeout)
    deadline = time.monotonic() + 5
    received = bytearray()
    normal_timeout = ser.timeout
    ser.timeout = 0
    try:
        while time.monotonic() < deadline:
            ser.write(b"\x1b")
            interval_end = time.monotonic() + 0.01
            while time.monotonic() < interval_end:
                chunk = ser.read(4096)
                if chunk:
                    received.extend(chunk)
                    if b"Please select" in received:
                        time.sleep(1.0)
                        while ser.read(4096):
                            pass
                        return
                time.sleep(0.001)
    finally:
        ser.timeout = normal_timeout
    raise TimeoutError("ESC did not open the MyLoader main menu")


def select_binary_loader(ser: serial.Serial) -> None:
    ser.write(b"\x1b")
    read_until(ser, b"Please select", 5)
    ser.write(b"2\r")
    read_until(ser, b"Load Program (BIN)", 5)
    ser.write(b"3\r")
    read_until(ser, b"Mini TFTP Server", 8)


def receive_dump(ser: serial.Serial, output: Path, timeout: float) -> str:
    initial = read_until(ser, START_MARKER, timeout)
    marker_end = initial.index(START_MARKER) + len(START_MARKER)
    pending = bytearray(initial[marker_end:])
    digest = hashlib.sha256()
    remaining = FLASH_SIZE
    first = bytearray()
    output.parent.mkdir(parents=True, exist_ok=True)

    with output.open("wb") as handle:
        inactivity_deadline = time.monotonic() + timeout
        next_progress = 1024 * 1024
        while remaining:
            if pending:
                take = min(len(pending), remaining)
                chunk = bytes(pending[:take])
                del pending[:take]
            else:
                chunk = ser.read(min(65536, remaining))
            if not chunk:
                if time.monotonic() >= inactivity_deadline:
                    raise TimeoutError(f"UART stopped with {remaining} bytes remaining")
                continue
            inactivity_deadline = time.monotonic() + timeout
            if len(first) < 0x20004:
                first.extend(chunk[: 0x20004 - len(first)])
            handle.write(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
            received = FLASH_SIZE - remaining
            if received >= next_progress:
                print(f"received={received}/{FLASH_SIZE}", flush=True)
                while next_progress <= received:
                    next_progress += 1024 * 1024

    trailer = read_until(ser, END_MARKER, 5)
    if END_MARKER not in trailer:
        raise RuntimeError("payload trailer missing")
    if first[:6] != b"GM8126":
        raise RuntimeError(f"unexpected flash header: {bytes(first[:8])!r}")
    if first[0x20000:0x20004] != b"MEF\x7f":
        raise RuntimeError(
            f"unexpected firmware header: {bytes(first[0x20000:0x20004])!r}"
        )
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uart-port", required=True)
    parser.add_argument("--uart-baud", type=int, default=115200)
    parser.add_argument("--loader-ip", default="192.168.168.1")
    parser.add_argument(
        "--payload", type=Path, default=ROOT / "payload" / "gm8126_flash_dump.bin"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--already-in-menu", action="store_true")
    parser.add_argument(
        "--uart-reboot",
        action="store_true",
        help="for an already-rooted test camera: issue reboot at its UART shell",
    )
    parser.add_argument("--menu-timeout", type=float, default=90)
    parser.add_argument("--dump-timeout", type=float, default=30)
    args = parser.parse_args()

    if not args.payload.is_file():
        raise FileNotFoundError(f"payload missing: {args.payload}; run build_payload.py")

    with serial.Serial(args.uart_port, args.uart_baud, timeout=0.2) as ser:
        if not args.already_in_menu:
            if args.uart_reboot:
                ser.write(b"\r")
                read_until(ser, b"# ", 15)
                ser.write(b"reboot\r")
                print("UART reboot requested; waiting for MyLoader...", flush=True)
            else:
                print(
                    "Power-cycle or reset the camera now; waiting for MyLoader...",
                    flush=True,
                )
            enter_main_menu(ser, args.menu_timeout)
        else:
            ser.reset_input_buffer()

        print("MyLoader menu detected; selecting Load Program (BIN)...", flush=True)
        select_binary_loader(ser)
        size, peer = put(args.loader_ip, args.payload)
        print(f"payload_sent={size} peer={peer[0]}:{peer[1]}", flush=True)
        digest = receive_dump(ser, args.output, args.dump_timeout)

    print(f"received={FLASH_SIZE}")
    print(f"sha256={digest}")
    print("flash_header=b'GM8126'")
    print("mef_at_0x20000=b'MEF\\x7f'")
    print("status=ok")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error={exc}", file=sys.stderr)
        raise
