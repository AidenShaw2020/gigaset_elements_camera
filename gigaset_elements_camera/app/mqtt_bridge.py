#!/usr/bin/env python3
"""Bridge one or more Gigaset Gen1 cameras to Home Assistant over MQTT."""

from __future__ import annotations

import argparse
import base64
import dataclasses
import datetime as dt
import hmac
import html
import json
import os
import re
import signal
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import paho.mqtt.client as mqtt


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or "camera"


def normalize_mac(mac: str) -> str:
    value = re.sub(r"[^0-9A-Fa-f]", "", mac).upper()
    if len(value) != 12:
        raise ValueError("MAC address must contain exactly 12 hexadecimal digits")
    return value


def derive_admin_password(mac: str) -> str:
    value = normalize_mac(mac)
    material = "LUCKOTVF" + value[::-1] + "YCAMVF"
    return base64.b64encode(material.encode("ascii")).decode("ascii")


def parse_duration(value: str) -> int | None:
    value = value.strip().lower()
    clock = re.search(r"(?:(\d+)\s*days?\s+)?(\d+):(\d+):(\d+)", value)
    if clock:
        days, hours, minutes, seconds = (int(part or 0) for part in clock.groups())
        return days * 86400 + hours * 3600 + minutes * 60 + seconds
    units = {"day": 86400, "hour": 3600, "min": 60, "sec": 1}
    total = 0
    matched = False
    for amount, unit in re.findall(
        r"(\d+)\s*(days?|hours?|mins?|minutes?|secs?|seconds?)", value
    ):
        matched = True
        total += int(amount) * next(
            seconds for name, seconds in units.items() if unit.startswith(name)
        )
    return total if matched else None


def parse_table_rows(page: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", page, re.I | re.S):
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.I | re.S)
        if len(cells) < 2:
            continue
        values = []
        for cell in cells[:2]:
            text = re.sub(r"<[^>]+>", " ", cell)
            values.append(" ".join(html.unescape(text).split()))
        key = values[0].rstrip(":").strip().lower()
        if key and values[1]:
            result[key] = values[1]
    return result


def parse_system_info(page: str) -> dict[str, object]:
    rows = parse_table_rows(page)
    telemetry: dict[str, object] = {}
    direct = {
        "model": "model",
        "bios/loader version": "loader_version",
        "firmware version": "firmware_version",
        "mac address": "reported_mac",
        "ip address": "ip_address",
        "subnet mask": "subnet_mask",
        "default gateway": "default_gateway",
        "mode": "network_mode",
        "primary dns ip address": "primary_dns",
        "secondary dns ip address": "secondary_dns",
        "current storage": "current_storage",
    }
    for source, target in direct.items():
        if source in rows:
            telemetry[target] = rows[source]
    if "system up time" in rows:
        telemetry["system_uptime_seconds"] = parse_duration(rows["system up time"])
        telemetry["system_uptime"] = rows["system up time"]
    if "total up time" in rows:
        telemetry["total_uptime_seconds"] = parse_duration(rows["total up time"])
        telemetry["total_uptime"] = rows["total up time"]

    statuses = []
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", page, re.I | re.S):
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.I | re.S)
        if len(cells) < 2:
            continue
        values = [
            " ".join(html.unescape(re.sub(r"<[^>]+>", " ", cell)).split())
            for cell in cells[:2]
        ]
        if values[0].rstrip(":").lower() == "status":
            statuses.append(values[1])
    status_names = (
        "wireless_status",
        "ethernet_status",
        "pppoe_status",
        "ddns_status",
        "upnp_status",
        "internet_status",
        "storage_status",
    )
    for key, value in zip(status_names, statuses):
        telemetry[key] = value
    return {key: value for key, value in telemetry.items() if value is not None}


def parse_stream_info(page: str) -> dict[str, object]:
    result: dict[str, object] = {}
    fields = {
        "SIZE0": "primary_size_code",
        "FRAMERATE0": "primary_fps",
        "H264BITRATE0": "primary_h264_kbps",
        "SIZE1": "secondary_size_code",
        "FRAMERATE1": "secondary_fps",
        "H264BITRATE1": "secondary_h264_kbps",
    }
    for source, target in fields.items():
        match = re.search(
            rf"cf\.{source}\.options\[i\]\.value\s*==\s*\"([^\"]+)\"",
            page,
            re.I,
        )
        if match:
            value: object = match.group(1)
            if "size_code" not in target:
                value = int(value)
            result[target] = value
    sizes = {"fsize": "1280x720", "qsize": "640x352", "qqsize": "320x176"}
    for prefix in ("primary", "secondary"):
        code = result.get(prefix + "_size_code")
        if code in sizes:
            result[prefix + "_resolution"] = sizes[code]
    return result


@dataclasses.dataclass
class CameraOptions:
    camera: str
    mac: str
    name: str = "Gigaset Camera"
    camera_user: str = "admin"
    camera_password: str = ""
    http_token: str = ""

    def __post_init__(self):
        self.camera = self.camera.strip()
        self.name = self.name.strip()
        if not self.camera:
            raise ValueError("camera IP address or hostname cannot be empty")
        if not self.name:
            raise ValueError("camera name cannot be empty")
        compact = normalize_mac(self.mac)
        self.mac = ":".join(
            compact[index : index + 2] for index in range(0, 12, 2)
        )
        self.camera_password = self.camera_password or derive_admin_password(self.mac)

    @property
    def key(self) -> str:
        return slugify(self.name + "_" + normalize_mac(self.mac)[-6:])

    @property
    def uid(self) -> str:
        return slugify(self.name + "_" + normalize_mac(self.mac))


def _availability(base: str, bridge_availability: str) -> dict[str, object]:
    return {
        "availability": [
            {"topic": bridge_availability},
            {"topic": base + "/availability"},
        ],
        "availability_mode": "all",
    }


def discovery_messages(
    prefix: str,
    base: str,
    uid: str,
    name: str,
    bridge_availability: str = "gigaset/bridge/availability",
    configuration_url: str | None = None,
):
    device = {
        "identifiers": [uid],
        "manufacturer": "Gigaset",
        "model": "Gen1 Camera",
        "name": name,
    }
    if configuration_url:
        device["configuration_url"] = configuration_url
    available = _availability(base, bridge_availability)
    origin = {
        "name": "Gigaset elements camera local gateway",
        "sw_version": "0.2.1",
        "support_url": "https://github.com/AidenShaw2020/gigaset_elements_camera",
    }

    def entity(component: str, object_id: str, payload: dict):
        payload.update(available)
        payload["device"] = device
        payload["origin"] = origin
        return f"{prefix}/{component}/{uid}/{object_id}/config", payload

    messages = [
        entity(
            "binary_sensor",
            "motion",
            {
                "name": "Motion",
                "unique_id": uid + "_motion",
                "state_topic": base + "/motion",
                "payload_on": "ON",
                "payload_off": "OFF",
                "device_class": "motion",
            },
        ),
        entity(
            "camera",
            "snapshot",
            {
                "name": "Snapshot",
                "unique_id": uid + "_snapshot",
                "topic": base + "/snapshot",
            },
        ),
        entity(
            "button",
            "refresh_snapshot",
            {
                "name": "Refresh snapshot",
                "unique_id": uid + "_refresh_snapshot",
                "command_topic": base + "/snapshot/get",
                "payload_press": "PRESS",
                "entity_category": "config",
                "icon": "mdi:refresh",
            },
        ),
        entity(
            "sensor",
            "last_snapshot",
            {
                "name": "Last snapshot",
                "unique_id": uid + "_last_snapshot",
                "state_topic": base + "/snapshot/updated",
                "device_class": "timestamp",
                "entity_category": "diagnostic",
            },
        ),
    ]
    telemetry = [
        ("firmware", "Firmware", "firmware_version", None, None),
        ("loader", "Loader", "loader_version", None, None),
        ("system_uptime", "System uptime", "system_uptime_seconds", "duration", "s"),
        ("total_uptime", "Total uptime", "total_uptime_seconds", "duration", "s"),
        ("ip_address", "IP address", "ip_address", None, None),
        ("network_mode", "Network mode", "network_mode", None, None),
        ("ethernet", "Ethernet", "ethernet_status", None, None),
        ("wireless", "Wireless", "wireless_status", None, None),
        ("storage", "Storage", "storage_status", None, None),
        ("primary_resolution", "Primary resolution", "primary_resolution", None, None),
        ("primary_fps", "Primary frame rate", "primary_fps", None, "fps"),
        (
            "primary_bitrate",
            "Primary H.264 bitrate",
            "primary_h264_kbps",
            "data_rate",
            "kbit/s",
        ),
        ("response_time", "Response time", "response_time_ms", "duration", "ms"),
    ]
    for object_id, label, field, device_class, unit in telemetry:
        payload = {
            "name": label,
            "unique_id": uid + "_" + object_id,
            "state_topic": base + "/telemetry",
            "value_template": "{{ value_json.%s }}" % field,
            "entity_category": "diagnostic",
        }
        if device_class:
            payload["device_class"] = device_class
        if unit:
            payload["unit_of_measurement"] = unit
        messages.append(entity("sensor", object_id, payload))
    return messages


class CameraBridge:
    def __init__(self, options: CameraOptions, settings, client: mqtt.Client):
        self.options = options
        self.settings = settings
        self.client = client
        self.base = settings.topic.rstrip("/") + "/" + options.key
        self.bridge_availability = settings.topic.rstrip("/") + "/bridge/availability"
        self.stop_event = threading.Event()
        self.motion_lock = threading.Lock()
        self.snapshot_lock = threading.Lock()
        self.motion_timer: threading.Timer | None = None
        token = base64.b64encode(
            f"{options.camera_user}:{options.camera_password}".encode("utf-8")
        ).decode("ascii")
        self.camera_headers = {"Authorization": "Basic " + token}

    def camera_url(self, path: str) -> str:
        return f"http://{self.options.camera}{path}"

    def request(self, path: str, timeout: float | None = None):
        request = urllib.request.Request(self.camera_url(path), headers=self.camera_headers)
        return urllib.request.urlopen(
            request,
            timeout=timeout if timeout is not None else self.settings.camera_timeout,
        )

    def publish_discovery(self):
        for topic, payload in discovery_messages(
            self.settings.discovery_prefix,
            self.base,
            self.options.uid,
            self.options.name,
            self.bridge_availability,
            self.camera_url("/en/main.asp"),
        ):
            self.client.publish(topic, json.dumps(payload), qos=1, retain=True)
        self.client.publish(self.base + "/motion", "OFF", qos=1, retain=True)

    def trigger_motion(self):
        with self.motion_lock:
            self.client.publish(self.base + "/motion", "ON", qos=1, retain=True)
            if self.motion_timer:
                self.motion_timer.cancel()
            self.motion_timer = threading.Timer(
                self.settings.motion_hold, self.clear_motion
            )
            self.motion_timer.daemon = True
            self.motion_timer.start()

    def clear_motion(self):
        with self.motion_lock:
            self.client.publish(self.base + "/motion", "OFF", qos=1, retain=True)
            self.motion_timer = None

    def fetch_snapshot(self) -> bytes:
        with self.request("/snapshot.jpg") as response:
            content = response.read()
        if not content.startswith(b"\xff\xd8") or not content.endswith(b"\xff\xd9"):
            raise ValueError("camera did not return a complete JPEG")
        return content

    def publish_snapshot(self) -> bool:
        if not self.snapshot_lock.acquire(blocking=False):
            return False
        started = time.monotonic()
        try:
            image = self.fetch_snapshot()
            now = dt.datetime.now(dt.timezone.utc).isoformat()
            self.client.publish(self.base + "/snapshot", image, qos=0, retain=False)
            self.client.publish(
                self.base + "/snapshot/updated", now, qos=1, retain=True
            )
            self.client.publish(self.base + "/snapshot/bytes", str(len(image)), qos=0)
            self.client.publish(
                self.base + "/snapshot/response_ms",
                str(round((time.monotonic() - started) * 1000)),
                qos=0,
            )
            self.client.publish(
                self.base + "/availability", "online", qos=1, retain=True
            )
            return True
        except (OSError, ValueError, urllib.error.URLError) as error:
            print(f"[{self.options.name}] snapshot error: {error}", flush=True)
            self.client.publish(
                self.base + "/availability", "offline", qos=1, retain=True
            )
            return False
        finally:
            self.snapshot_lock.release()

    def snapshot_loop(self):
        while not self.stop_event.is_set():
            self.publish_snapshot()
            self.stop_event.wait(self.settings.snapshot_interval)

    def fetch_text(self, path: str) -> str:
        with self.request(path) as response:
            return response.read().decode("windows-1252", errors="replace")

    def publish_telemetry(self):
        started = time.monotonic()
        try:
            values = parse_system_info(self.fetch_text("/en/sysinfo.asp"))
            values.update(parse_stream_info(self.fetch_text("/en/stream.asp")))
            values["response_time_ms"] = round((time.monotonic() - started) * 1000)
            values["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            self.client.publish(
                self.base + "/telemetry", json.dumps(values), qos=1, retain=True
            )
            self.client.publish(
                self.base + "/availability", "online", qos=1, retain=True
            )
        except (OSError, ValueError, urllib.error.URLError) as error:
            print(f"[{self.options.name}] telemetry error: {error}", flush=True)

    def telemetry_loop(self):
        while not self.stop_event.is_set():
            self.publish_telemetry()
            self.stop_event.wait(self.settings.telemetry_interval)

    def start(self):
        threading.Thread(target=self.snapshot_loop, daemon=True).start()
        threading.Thread(target=self.telemetry_loop, daemon=True).start()

    def stop(self):
        self.stop_event.set()
        with self.motion_lock:
            if self.motion_timer:
                self.motion_timer.cancel()
                self.motion_timer = None
        self.client.publish(self.base + "/availability", "offline", qos=1, retain=True)


class Registry:
    def __init__(self, cameras: list[CameraBridge]):
        self.cameras = cameras
        self.by_key = {camera.options.key: camera for camera in cameras}

    def resolve(self, path: str):
        match = re.fullmatch(
            r"/camera/([^/]+)/(motion|snapshot\.jpg|stream\.mjpeg)", path
        )
        if match:
            return self.by_key.get(match.group(1)), match.group(2)
        if path == "/motion" and len(self.cameras) == 1:
            return self.cameras[0], "motion"
        return None, None


def make_handler(registry: Registry):
    class Handler(BaseHTTPRequestHandler):
        server_version = "GigasetMQTT/2.0"

        def reply_json(self, status: int, payload: dict):
            content = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def authorized(self, camera: CameraBridge) -> bool:
            expected = camera.options.http_token
            if not expected:
                return True
            supplied = urllib.parse.parse_qs(
                urllib.parse.urlsplit(self.path).query
            ).get("token", [""])[0]
            return hmac.compare_digest(supplied, expected)

        def send_snapshot(self, camera: CameraBridge):
            try:
                content = camera.fetch_snapshot()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            except (OSError, ValueError, urllib.error.URLError) as error:
                self.reply_json(502, {"ok": False, "error": str(error)})

        def send_stream(self, camera: CameraBridge):
            try:
                with camera.request("/stream.jpg", timeout=30) as upstream:
                    self.send_response(200)
                    self.send_header(
                        "Content-Type",
                        upstream.headers.get(
                            "Content-Type",
                            "multipart/x-mixed-replace;boundary=--videoboundary",
                        ),
                    )
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    while True:
                        chunk = upstream.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except (OSError, urllib.error.URLError) as error:
                print(
                    f"[{camera.options.name}] stream proxy error: {error}", flush=True
                )

        def dispatch(self):
            path = urllib.parse.urlsplit(self.path).path
            if path == "/health":
                self.reply_json(
                    200,
                    {
                        "ok": True,
                        "cameras": [camera.options.key for camera in registry.cameras],
                    },
                )
                return
            camera, action = registry.resolve(path)
            if not camera:
                self.reply_json(
                    404, {"ok": False, "error": "camera endpoint not found"}
                )
                return
            if not self.authorized(camera):
                self.reply_json(403, {"ok": False, "error": "invalid token"})
                return
            if action == "motion":
                camera.trigger_motion()
                self.reply_json(
                    200,
                    {"ok": True, "camera": camera.options.key, "motion": "ON"},
                )
            elif action == "snapshot.jpg":
                self.send_snapshot(camera)
            elif action == "stream.mjpeg":
                self.send_stream(camera)

        def do_GET(self):
            self.dispatch()

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            if length:
                self.rfile.read(length)
            self.dispatch()

        def log_message(self, format, *values):
            print(f"http {self.address_string()}: {format % values}", flush=True)

    return Handler


def env(name: str, fallback=None):
    return os.environ.get(name, fallback)


def camera_from_mapping(value: dict) -> CameraOptions:
    return CameraOptions(
        camera=str(value.get("ip") or value.get("camera_ip") or "").strip(),
        mac=str(value.get("mac") or value.get("camera_mac") or "").strip(),
        name=str(
            value.get("name") or value.get("camera_name") or "Gigaset Camera"
        ).strip(),
        camera_user=str(
            value.get("user") or value.get("camera_user") or "admin"
        ).strip(),
        camera_password=str(
            value.get("password") or value.get("camera_password") or ""
        ),
        http_token=str(value.get("token") or value.get("http_token") or ""),
    )


def load_camera_options(args) -> list[CameraOptions]:
    if args.config:
        data = json.loads(Path(args.config).read_text(encoding="utf-8"))
        args.motion_hold = float(data.get("motion_hold", args.motion_hold))
        args.snapshot_interval = float(
            data.get("snapshot_interval", args.snapshot_interval)
        )
        args.telemetry_interval = float(
            data.get("telemetry_interval", args.telemetry_interval)
        )
        args.topic = str(data.get("mqtt_topic", args.topic))
        configured = data.get("cameras") or []
        if configured:
            cameras = [camera_from_mapping(item) for item in configured]
        elif data.get("camera_ip") and data.get("camera_mac"):
            cameras = [camera_from_mapping(data)]
        else:
            raise ValueError("configure at least one camera in the cameras list")
    else:
        if not args.camera or not args.mac:
            raise ValueError("--camera and --mac are required without --config")
        cameras = [
            CameraOptions(
                camera=args.camera,
                mac=args.mac,
                name=args.name,
                camera_user=args.camera_user,
                camera_password=args.camera_password or "",
                http_token=args.http_token or "",
            )
        ]
    keys = [camera.key for camera in cameras]
    if len(keys) != len(set(keys)):
        raise ValueError("camera names and MAC addresses must produce unique IDs")
    return cameras


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="add-on options JSON containing a cameras list")
    parser.add_argument("--camera", default=env("CAMERA_IP"))
    parser.add_argument("--mac", default=env("CAMERA_MAC"))
    parser.add_argument("--name", default=env("CAMERA_NAME", "Gigaset Camera"))
    parser.add_argument("--camera-user", default=env("CAMERA_USER", "admin"))
    parser.add_argument("--camera-password", default=env("CAMERA_PASSWORD"))
    parser.add_argument(
        "--broker", default=env("MQTT_BROKER"), required=not env("MQTT_BROKER")
    )
    parser.add_argument("--mqtt-port", type=int, default=int(env("MQTT_PORT", "1883")))
    parser.add_argument("--mqtt-user", default=env("MQTT_USER"))
    parser.add_argument("--mqtt-password", default=env("MQTT_PASSWORD"))
    parser.add_argument(
        "--mqtt-tls", action="store_true", default=env("MQTT_TLS") == "1"
    )
    parser.add_argument("--topic", default=env("MQTT_TOPIC", "gigaset/camera"))
    parser.add_argument(
        "--discovery-prefix", default=env("HA_DISCOVERY_PREFIX", "homeassistant")
    )
    parser.add_argument("--listen", default=env("HTTP_LISTEN", "0.0.0.0"))
    parser.add_argument("--http-port", type=int, default=int(env("HTTP_PORT", "8766")))
    parser.add_argument("--http-token", default=env("HTTP_TOKEN"))
    parser.add_argument(
        "--motion-hold", type=float, default=float(env("MOTION_HOLD", "15"))
    )
    parser.add_argument(
        "--snapshot-interval",
        type=float,
        default=float(env("SNAPSHOT_INTERVAL", "10")),
    )
    parser.add_argument(
        "--telemetry-interval",
        type=float,
        default=float(env("TELEMETRY_INTERVAL", "60")),
    )
    parser.add_argument("--camera-timeout", type=float, default=5.0)
    args = parser.parse_args()
    args.cameras = load_camera_options(args)
    return args


def main():
    args = parse_args()
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="gigaset-camera-bridge",
    )
    if args.mqtt_user:
        client.username_pw_set(args.mqtt_user, args.mqtt_password)
    if args.mqtt_tls:
        client.tls_set()
    bridge_availability = args.topic.rstrip("/") + "/bridge/availability"
    client.will_set(bridge_availability, "offline", 1, True)
    cameras = [CameraBridge(options, args, client) for options in args.cameras]
    registry = Registry(cameras)

    def connected(_client, _userdata, _flags, reason_code, _properties):
        if reason_code == 0:
            print("MQTT connected", flush=True)
            client.publish(bridge_availability, "online", qos=1, retain=True)
            for camera in cameras:
                camera.publish_discovery()
                client.subscribe(camera.base + "/snapshot/get", qos=1)
        else:
            print(f"MQTT connection failed: {reason_code}", flush=True)

    def message(_client, _userdata, event):
        for camera in cameras:
            if event.topic == camera.base + "/snapshot/get":
                threading.Thread(target=camera.publish_snapshot, daemon=True).start()
                break

    client.on_connect = connected
    client.on_message = message
    client.connect(args.broker, args.mqtt_port, keepalive=60)
    client.loop_start()
    server = ThreadingHTTPServer((args.listen, args.http_port), make_handler(registry))
    for camera in cameras:
        camera.start()

    def shutdown(_signal=None, _frame=None):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    for camera in cameras:
        if not camera.options.http_token:
            print(
                f"[{camera.options.name}] warning: token is empty; local HTTP "
                "motion and proxy endpoints are unauthenticated",
                flush=True,
            )
        token = (
            "?token=" + urllib.parse.quote(camera.options.http_token)
            if camera.options.http_token
            else ""
        )
        root = (
            f"http://<home-assistant-ip>:{args.http_port}/camera/"
            f"{camera.options.key}"
        )
        print(f"[{camera.options.name}] motion: {root}/motion{token}", flush=True)
        print(
            f"[{camera.options.name}] snapshot: {root}/snapshot.jpg{token}",
            flush=True,
        )
        print(f"[{camera.options.name}] MJPEG: {root}/stream.mjpeg{token}", flush=True)
    try:
        server.serve_forever()
    finally:
        for camera in cameras:
            camera.stop()
        client.publish(bridge_availability, "offline", qos=1, retain=True)
        server.server_close()
        client.disconnect()
        client.loop_stop()


if __name__ == "__main__":
    main()
