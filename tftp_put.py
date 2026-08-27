#!/usr/bin/env python3
"""Minimal TFTP octet-mode PUT client for the MyLoader mini TFTP server."""

from __future__ import annotations

import argparse
import socket
import struct
from pathlib import Path


def packet(opcode: int, *parts: bytes) -> bytes:
    return struct.pack("!H", opcode) + b"\0".join(parts) + (b"\0" if parts else b"")


def put(
    host: str,
    path: Path,
    *,
    port: int = 69,
    timeout: float = 3.0,
    retries: int = 8,
) -> tuple[int, tuple[str, int]]:
    data = path.read_bytes()
    remote = (host, port)
    request = packet(2, path.name.encode("ascii"), b"octet")

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        peer = remote
        outgoing = request
        expected_ack = 0
        offset = 0

        while True:
            for _attempt in range(retries):
                sock.sendto(outgoing, peer)
                try:
                    reply, sender = sock.recvfrom(2048)
                except TimeoutError:
                    continue
                if len(reply) < 4:
                    continue
                opcode, block = struct.unpack("!HH", reply[:4])
                if opcode == 5:
                    message = reply[4:].rstrip(b"\0").decode("ascii", "replace")
                    raise RuntimeError(f"TFTP error {block}: {message}")
                if opcode == 4 and block == expected_ack:
                    peer = sender
                    break
            else:
                raise TimeoutError(f"no ACK for block {expected_ack}")

            if expected_ack and len(outgoing) < 4 + 512:
                break

            chunk = data[offset : offset + 512]
            offset += len(chunk)
            expected_ack = (expected_ack + 1) & 0xFFFF
            outgoing = struct.pack("!HH", 3, expected_ack) + chunk

        return len(data), peer


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("host")
    parser.add_argument("path", type=Path)
    parser.add_argument("--port", type=int, default=69)
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--retries", type=int, default=8)
    args = parser.parse_args()

    size, peer = put(
        args.host,
        args.path,
        port=args.port,
        timeout=args.timeout,
        retries=args.retries,
    )
    print(f"sent={size} peer={peer[0]}:{peer[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
