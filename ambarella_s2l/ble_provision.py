"""Local BLE provisioning for the newer Gigaset Elements camera.

The protocol constants and command sequence were independently documented
from the discontinued Gigaset Elements Android client.  No vendor code is
included.  BLE provisioning itself is entirely local and does not contact the
former cloud service.
"""

from __future__ import annotations

import argparse
import asyncio
import re
from dataclasses import dataclass


CAMERA_NAME_PREFIX = "Gigaset-C-"
WIFI_COMMAND_CHARACTERISTIC = "00009616-0000-1000-8000-00805f9b34fb"
WIFI_LIST_CHARACTERISTIC = "00001d78-0000-1000-8000-00805f9b34fb"
WIFI_PASSWORD_CHARACTERISTIC = "0000f0a5-0000-1000-8000-00805f9b34fb"
WIFI_STATUS_CHARACTERISTIC = "00000220-0000-1000-8000-00805f9b34fb"
WIFI_SCAN_COMMAND = b"70"
WIFI_SELECT_COMMAND_PREFIX = "71"
REQUESTED_MTU = 517


@dataclass(frozen=True)
class CameraAdvertisement:
    name: str
    address: str
    rssi: int | None
    connectable: bool | None = None
    service_uuids: tuple[str, ...] = ()
    advertisement_type: str = "unknown"
    address_type: str = "unknown"


@dataclass(frozen=True)
class CameraWifiNetwork:
    index: int
    ssid: str
    security: str


async def scan_cameras(timeout: float = 12.0) -> list[CameraAdvertisement]:
    try:
        from bleak import BleakScanner
    except ImportError as error:  # pragma: no cover - depends on host setup
        raise RuntimeError("Bleak is required: python -m pip install bleak") from error

    discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
    cameras: list[CameraAdvertisement] = []
    for device, advertisement in discovered.values():
        name = advertisement.local_name or device.name or ""
        if name.startswith(CAMERA_NAME_PREFIX):
            raw_pair = advertisement.platform_data[1] if len(advertisement.platform_data) > 1 else None
            raw_adv = getattr(raw_pair, "adv", None)
            adv_type = str(getattr(raw_adv, "advertisement_type", "unknown"))
            address_type = str(getattr(raw_adv, "bluetooth_address_type", "unknown"))
            cameras.append(
                CameraAdvertisement(
                    name=name,
                    address=device.address,
                    rssi=getattr(advertisement, "rssi", None),
                    connectable=getattr(advertisement, "connectable", None),
                    service_uuids=tuple(advertisement.service_uuids or ()),
                    advertisement_type=adv_type,
                    address_type=address_type,
                )
            )
    return sorted(cameras, key=lambda item: item.name)


def parse_wifi_list(payload: bytes) -> list[CameraWifiNetwork]:
    """Parse the camera's delimiter-separated Wi-Fi scan response."""

    text = payload.decode("iso-8859-1").strip("\x00\r\n ")
    tokens = [token.strip() for token in re.split(r'[<>,"]+', text) if token.strip()]
    networks: list[CameraWifiNetwork] = []
    for index in range(0, len(tokens) - 1, 2):
        networks.append(
            CameraWifiNetwork(
                index=index // 2,
                ssid=tokens[index],
                security=tokens[index + 1],
            )
        )
    return networks


async def resolve_camera(address: str | None, timeout: float) -> CameraAdvertisement:
    if address:
        return CameraAdvertisement("selected camera", address, None)
    cameras = await scan_cameras(timeout)
    if not cameras:
        raise RuntimeError("No advertising Gigaset-C-* camera found")
    if len(cameras) > 1:
        names = ", ".join(f"{camera.name} ({camera.address})" for camera in cameras)
        raise RuntimeError(f"Several cameras found; select one with --address: {names}")
    return cameras[0]


async def read_wifi_networks(address: str, wait_seconds: float = 6.0) -> list[CameraWifiNetwork]:
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError as error:  # pragma: no cover - depends on host setup
        raise RuntimeError("Bleak is required: python -m pip install bleak") from error

    device = await BleakScanner.find_device_by_address(address, timeout=15.0)
    if device is None:
        raise RuntimeError(f"Camera {address} stopped advertising")
    try:
        # The camera programs a controller BD_ADDR at boot and BlueZ advertises
        # it as a public address.  Windows often fails to infer the address type
        # for advertisements which contain neither a service UUID nor flags.
        async with BleakClient(
            device,
            timeout=20.0,
            winrt={"address_type": "random", "use_cached_services": False},
        ) as client:
            command = client.services.get_characteristic(WIFI_COMMAND_CHARACTERISTIC)
            wifi_list = client.services.get_characteristic(WIFI_LIST_CHARACTERISTIC)
            if command is None or wifi_list is None:
                raise RuntimeError("Camera Wi-Fi GATT characteristics were not found")
            await client.write_gatt_char(command, WIFI_SCAN_COMMAND, response=True)
            await asyncio.sleep(wait_seconds)
            payload = await client.read_gatt_char(wifi_list)
    except OSError as error:
        raise RuntimeError(f"Windows BLE connection failed: {error}") from error
    networks = parse_wifi_list(bytes(payload))
    if not networks:
        raise RuntimeError("Camera returned an empty or unrecognized Wi-Fi list")
    return networks


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Provision newer Gigaset Elements cameras over local Bluetooth LE."
    )
    parser.add_argument("--scan", action="store_true", help="scan for Gigaset-C-* cameras")
    parser.add_argument(
        "--list-wifi",
        action="store_true",
        help="ask one advertising camera to scan nearby 2.4 GHz Wi-Fi networks",
    )
    parser.add_argument("--address", help="camera BLE address; auto-select if only one is found")
    parser.add_argument("--timeout", type=float, default=12.0, help="BLE scan time in seconds")
    return parser


async def _async_main(args: argparse.Namespace) -> int:
    try:
        if args.scan:
            cameras = await scan_cameras(args.timeout)
            if not cameras:
                print("No advertising Gigaset-C-* camera found.")
                return 1
            for camera in cameras:
                suffix = "" if camera.rssi is None else f"  RSSI {camera.rssi} dBm"
                print(
                    f"{camera.name}  {camera.address}{suffix}  "
                    f"type={camera.advertisement_type}/{camera.address_type}  "
                    f"connectable={camera.connectable}  "
                    f"services={','.join(camera.service_uuids) or '-'}"
                )
            return 0
        if args.list_wifi:
            camera = await resolve_camera(args.address, args.timeout)
            print(f"Connecting to {camera.name} ({camera.address}) ...")
            networks = await read_wifi_networks(camera.address)
            for network in networks:
                print(f"[{network.index}] {network.ssid}  ({network.security})")
            return 0
        raise SystemExit("Use --scan or --list-wifi.")
    except RuntimeError as error:
        print(f"Error: {error}")
        return 2


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
