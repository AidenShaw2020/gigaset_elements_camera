#!/usr/bin/env python3
"""Run the read-only MyLoader dumper over user-space raw Ethernet.

The adapter is selected from its physical MAC, so Windows does not need an
extra 192.168.168.2 address or an interface-specific identifier in the tool.
"""

from __future__ import annotations

import argparse
import queue
import random
import struct
import sys
import time
from pathlib import Path

from scapy.all import (
    ARP, IP, UDP, AsyncSniffer, Ether, Raw, get_if_hwaddr, get_if_list, sendp
)


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import bootloader_dump


IFACE = ""
SOURCE_IP = "192.168.168.2"
SOURCE_MAC = ""
# MyLoader uses its own fixed factory MAC before Linux loads the camera MAC.
TARGET_MAC = "00:80:48:01:23:45"


def find_interface(mac: str) -> str:
    wanted = mac.lower().replace("-", ":")
    for interface in get_if_list():
        try:
            if get_if_hwaddr(interface).lower() == wanted:
                return interface
        except Exception:
            pass
    raise RuntimeError(f"Npcap interface with MAC {mac} was not found")


def tftp_packet(opcode: int, *parts: bytes) -> bytes:
    return struct.pack("!H", opcode) + b"\0".join(parts) + (b"\0" if parts else b"")


def raw_put(host: str, path: Path, *, port: int = 69, timeout: float = 3.0,
            retries: int = 8):
    data = path.read_bytes()
    source_port = random.randint(20000, 50000)
    target_port = port
    target_mac = TARGET_MAC
    received: queue.Queue = queue.Queue()

    def capture(packet):
        received.put(packet)

    sniffer = AsyncSniffer(iface=IFACE, prn=capture, store=False)
    sniffer.start()
    try:
        # Teach the loader our synthetic IP-to-MAC mapping before the WRQ.
        sendp(
            Ether(src=SOURCE_MAC, dst=target_mac)
            / ARP(op=2, hwsrc=SOURCE_MAC, psrc=SOURCE_IP,
                  hwdst=target_mac, pdst=host),
            iface=IFACE,
            verbose=False,
        )

        expected_ack = 0
        offset = 0
        outgoing = tftp_packet(2, path.name.encode("ascii"), b"octet")
        while True:
            response = None
            for _attempt in range(retries):
                frame = (
                    Ether(src=SOURCE_MAC, dst=target_mac)
                    / IP(src=SOURCE_IP, dst=host)
                    / UDP(sport=source_port, dport=target_port)
                    / Raw(outgoing)
                )
                sendp(frame, iface=IFACE, verbose=False)
                while True:
                    try:
                        candidate = received.get(timeout=timeout)
                    except queue.Empty:
                        break
                    if ARP in candidate and candidate[ARP].op == 1 and candidate[ARP].pdst == SOURCE_IP:
                        sendp(
                            Ether(src=SOURCE_MAC, dst=candidate[Ether].src)
                            / ARP(op=2, hwsrc=SOURCE_MAC, psrc=SOURCE_IP,
                                  hwdst=candidate[Ether].src,
                                  pdst=candidate[ARP].psrc),
                            iface=IFACE,
                            verbose=False,
                        )
                        continue
                    if IP not in candidate or UDP not in candidate:
                        continue
                    if candidate[IP].src != host or candidate[IP].dst != SOURCE_IP:
                        continue
                    if candidate[UDP].dport != source_port:
                        continue
                    payload = bytes(candidate[UDP].payload)
                    if len(payload) < 4:
                        continue
                    opcode, block = struct.unpack("!HH", payload[:4])
                    if opcode == 5:
                        message = payload[4:].rstrip(b"\0").decode("ascii", "replace")
                        raise RuntimeError(f"TFTP error {block}: {message}")
                    if opcode == 4 and block == expected_ack:
                        response = candidate
                        break
                if response is not None:
                    break
            if response is None:
                raise TimeoutError(f"no raw-Ethernet ACK for block {expected_ack}")

            target_mac = response[Ether].src
            target_port = response[UDP].sport
            if expected_ack and len(outgoing) < 4 + 512:
                break
            chunk = data[offset:offset + 512]
            offset += len(chunk)
            expected_ack = (expected_ack + 1) & 0xFFFF
            outgoing = struct.pack("!HH", 3, expected_ack) + chunk
    finally:
        sniffer.stop()

    return len(data), (host, target_port)


bootloader_dump.put = raw_put


def enter_main_menu_aggressively(ser, timeout: float) -> None:
    """Send ESC throughout reset so the loader's earliest window is caught."""
    deadline = time.monotonic() + timeout
    received = bytearray()
    normal_timeout = ser.timeout
    ser.timeout = 0
    try:
        while time.monotonic() < deadline:
            ser.write(b"\x1b")
            chunk = ser.read(4096)
            if chunk:
                received.extend(chunk)
                if b"Please select" in received:
                    time.sleep(0.5)
                    while ser.read(4096):
                        pass
                    return
                if len(received) > 256 * 1024:
                    del received[:128 * 1024]
            time.sleep(0.002)
    finally:
        ser.timeout = normal_timeout
    raise TimeoutError("continuous ESC did not open the MyLoader main menu")


bootloader_dump.enter_main_menu = enter_main_menu_aggressively

if __name__ == "__main__":
    wrapper = argparse.ArgumentParser(
        usage="%(prog)s --host-mac MAC --uart-port PORT --output FILE [options]",
        description=__doc__,
        epilog=(
            "Dumper options such as --uart-port, --output, --already-in-menu "
            "and --menu-timeout are forwarded to bootloader_dump.py."
        ),
    )
    wrapper.add_argument("--host-mac", required=True,
                         help="physical MAC of the PC Ethernet adapter")
    wrapper.add_argument("--host-ip", default="192.168.168.2",
                         help="synthetic PC address used only in raw frames")
    wrapper.add_argument("--loader-mac", default="00:80:48:01:23:45")
    wrapper.add_argument("--interface",
                         help="Npcap interface; auto-selected from --host-mac")
    options, remaining = wrapper.parse_known_args()
    SOURCE_MAC = options.host_mac.lower().replace("-", ":")
    SOURCE_IP = options.host_ip
    TARGET_MAC = options.loader_mac.lower().replace("-", ":")
    IFACE = options.interface or find_interface(SOURCE_MAC)
    sys.argv = [sys.argv[0]] + remaining
    raise SystemExit(bootloader_dump.main())
