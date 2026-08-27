#!/usr/bin/env python3
"""Send a file to the MyLoader mini TFTP server through raw Ethernet.

This avoids changing the host adapter's IPv4 configuration.  It requires
Scapy and Npcap and implements the standard 512-byte TFTP write sequence.
"""

from __future__ import annotations

import argparse
import os
import random
import struct
import sys
import time

from scapy.all import ARP, Ether, IP, Raw, UDP, conf, get_if_hwaddr, get_if_list, sendp, sniff, srp1


def find_interface(mac: str) -> str:
    wanted = mac.lower().replace("-", ":")
    for interface in get_if_list():
        try:
            if get_if_hwaddr(interface).lower() == wanted:
                return interface
        except Exception:
            pass
    raise RuntimeError(f"Npcap interface with MAC {mac} was not found")


def resolve_mac(interface: str, host_mac: str, host_ip: str, target_ip: str) -> str:
    request = (
        Ether(src=host_mac, dst="ff:ff:ff:ff:ff:ff")
        / ARP(op=1, hwsrc=host_mac, psrc=host_ip, pdst=target_ip)
    )
    answer = srp1(request, iface=interface, timeout=3, verbose=False)
    if answer is None or ARP not in answer:
        raise RuntimeError(f"No ARP response from {target_ip}")
    return answer[ARP].hwsrc.lower()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("target_ip")
    parser.add_argument("file")
    parser.add_argument("--host-ip", default="192.168.168.2")
    parser.add_argument(
        "--host-mac", required=True,
        help="physical MAC address of the PC Ethernet adapter",
    )
    parser.add_argument("--timeout", type=float, default=1.5)
    parser.add_argument("--retries", type=int, default=12)
    args = parser.parse_args()

    conf.verb = 0
    interface = find_interface(args.host_mac)
    target_mac = resolve_mac(interface, args.host_mac, args.host_ip, args.target_ip)
    print(f"Interface: {interface}")
    print(f"Target: {args.target_ip} ({target_mac})")

    with open(args.file, "rb") as handle:
        data = handle.read()
    filename = os.path.basename(args.file).encode("ascii", "replace")
    source_port = random.randint(40000, 60000)

    def exchange(payload: bytes, destination_port: int, expected_block: int | None):
        frame = (
            Ether(src=args.host_mac, dst=target_mac)
            / IP(src=args.host_ip, dst=args.target_ip)
            / UDP(sport=source_port, dport=destination_port)
            / Raw(payload)
        )

        def response(packet) -> bool:
            if IP not in packet or UDP not in packet:
                return False
            if packet[IP].src != args.target_ip or packet[UDP].dport != source_port:
                return False
            body = bytes(packet[UDP].payload)
            if len(body) < 2:
                return False
            opcode = struct.unpack("!H", body[:2])[0]
            if opcode == 5:
                return True
            return (
                opcode == 4
                and len(body) >= 4
                and expected_block is not None
                and struct.unpack("!H", body[2:4])[0] == expected_block
            )

        packets = sniff(
            iface=interface,
            count=1,
            timeout=args.timeout,
            lfilter=response,
            started_callback=lambda: sendp(frame, iface=interface, verbose=False),
        )
        if not packets:
            return None
        packet = packets[0]
        body = bytes(packet[UDP].payload)
        opcode = struct.unpack("!H", body[:2])[0]
        if opcode == 5:
            code = struct.unpack("!H", body[2:4])[0] if len(body) >= 4 else -1
            message = body[4:].split(b"\0", 1)[0].decode("latin1", "replace")
            raise RuntimeError(f"TFTP error {code}: {message}")
        return packet

    wrq = b"\x00\x02" + filename + b"\x00octet\x00"
    reply = None
    for attempt in range(1, args.retries + 1):
        reply = exchange(wrq, 69, 0)
        if reply is not None:
            break
        print(f"WRQ retry {attempt}/{args.retries}")
    if reply is None:
        raise RuntimeError("TFTP server did not acknowledge the write request")
    server_port = reply[UDP].sport
    print(f"TFTP transfer port: {server_port}")

    total_blocks = len(data) // 512 + 1
    started = time.monotonic()
    for index in range(total_blocks):
        block = (index + 1) & 0xFFFF
        chunk = data[index * 512 : (index + 1) * 512]
        packet = b"\x00\x03" + struct.pack("!H", block) + chunk
        acknowledged = False
        for attempt in range(1, args.retries + 1):
            if exchange(packet, server_port, block) is not None:
                acknowledged = True
                break
            if attempt == 1:
                print(f"Block {index + 1}: retrying", flush=True)
        if not acknowledged:
            raise RuntimeError(f"No ACK for block {index + 1} ({block:#06x})")
        if (index + 1) % 256 == 0 or index + 1 == total_blocks:
            sent = min((index + 1) * 512, len(data))
            elapsed = max(time.monotonic() - started, 0.001)
            print(
                f"{sent}/{len(data)} bytes ({sent * 100 / len(data):.1f}%), "
                f"{sent / elapsed / 1024:.1f} KiB/s",
                flush=True,
            )

    print("Transfer completed; waiting for the loader to verify/write the image.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
