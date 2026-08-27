#!/usr/bin/env python3
"""Prepare MyLoader partition 1 for TFTP, or verify and reboot after writing."""

from __future__ import annotations

import argparse
import time

import serial


def wait_for(uart, needle: bytes, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    received = bytearray()
    while time.monotonic() < deadline:
        received.extend(uart.read(4096))
        if needle in received:
            return bytes(received)
    raise TimeoutError(f"UART did not report {needle!r}")


def enter_menu(uart, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    received = bytearray()
    uart.timeout = 0
    while time.monotonic() < deadline:
        uart.write(b"\x1b")
        received.extend(uart.read(4096))
        if b"Please select" in received:
            uart.timeout = 0.2
            time.sleep(0.3)
            uart.read(16384)
            return
        if len(received) > 256 * 1024:
            del received[:128 * 1024]
        time.sleep(0.002)
    raise TimeoutError("MyLoader menu not detected; reset or power-cycle the camera")


def prepare(uart, already_in_menu: bool, timeout: float) -> None:
    if not already_in_menu:
        print("Reset or power-cycle the camera now; sending ESC...", flush=True)
        enter_menu(uart, timeout)
    for key, prompt in (
        (b"5\r", b"Update Flash (Binary Mode)"),
        (b"4\r", b"Please Select Partition"),
        (b"1\r", b"Mini TFTP Server"),
    ):
        uart.reset_input_buffer()
        uart.write(key)
        wait_for(uart, prompt, 5)
    print("partition1_tftp=ready")


def finish(uart, timeout: float, reboot: bool) -> None:
    deadline = time.monotonic() + timeout
    result = bytearray()
    while time.monotonic() < deadline:
        result.extend(uart.read(4096))
        lowered = bytes(result).lower()
        if b"bad firmware" in lowered or b"error" in lowered or b"failed" in lowered:
            raise RuntimeError(result.decode("latin1", "replace"))
        if b"done" in lowered:
            break
    else:
        raise TimeoutError("MyLoader did not report a write result")
    print("loader_write=done")
    if reboot:
        uart.write(b"\x1b")
        time.sleep(0.4)
        menu = uart.read(16384)
        if b"Main Menu" not in menu:
            uart.write(b"\x1b")
            time.sleep(0.4)
            menu += uart.read(16384)
        if b"Main Menu" not in menu:
            raise RuntimeError("could not return to the MyLoader main menu")
        uart.write(b"7\r")
        print("camera_reboot=requested")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "finish"))
    parser.add_argument("--port", required=True)
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--already-in-menu", action="store_true")
    parser.add_argument("--no-reboot", action="store_true")
    args = parser.parse_args()
    with serial.Serial(args.port, args.baud, timeout=0.2) as uart:
        if args.action == "prepare":
            prepare(uart, args.already_in_menu, args.timeout)
        else:
            finish(uart, args.timeout, not args.no_reboot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
