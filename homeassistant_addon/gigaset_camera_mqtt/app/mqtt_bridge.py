#!/usr/bin/env python3
"""Bridge a Gigaset Gen1 camera to Home Assistant over MQTT.

The camera reports motion to the bridge using its stock HTTP alarm action.  The
bridge publishes Home Assistant MQTT discovery, motion state, availability and
periodic JPEG snapshots.  Nothing is sent outside the local network.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import signal
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import paho.mqtt.client as mqtt

def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or "camera"


def derive_admin_password(mac: str) -> str:
    value = re.sub(r"[^0-9A-Fa-f]", "", mac).upper()
    if len(value) != 12:
        raise ValueError("MAC address must contain exactly 12 hexadecimal digits")
    material = "LUCKOTVF" + value[::-1] + "YCAMVF"
    return base64.b64encode(material.encode("ascii")).decode("ascii")


def discovery_messages(prefix: str, base: str, uid: str, name: str):
    device = {
        "identifiers": [uid],
        "manufacturer": "Gigaset",
        "model": "Gen1 Camera",
        "name": name,
    }
    availability = base + "/availability"
    motion = {
        "name": "Motion",
        "unique_id": uid + "_motion",
        "state_topic": base + "/motion",
        "availability_topic": availability,
        "payload_on": "ON",
        "payload_off": "OFF",
        "device_class": "motion",
        "device": device,
    }
    camera = {
        "name": "Snapshot",
        "unique_id": uid + "_snapshot",
        "topic": base + "/snapshot",
        "availability_topic": availability,
        "device": device,
    }
    return [
        (f"{prefix}/binary_sensor/{uid}/motion/config", motion),
        (f"{prefix}/camera/{uid}/snapshot/config", camera),
    ]


class Bridge:
    def __init__(self, args, client: mqtt.Client):
        self.args = args
        self.client = client
        self.base = args.topic.rstrip("/")
        self.uid = slugify(args.name + "_" + args.mac.replace(":", ""))
        self.stop_event = threading.Event()
        self.motion_lock = threading.Lock()
        self.motion_timer: threading.Timer | None = None
        token = base64.b64encode(
            f"{args.camera_user}:{args.camera_password}".encode("utf-8")
        ).decode("ascii")
        self.camera_headers = {"Authorization": "Basic " + token}

    def publish_discovery(self):
        for topic, payload in discovery_messages(
            self.args.discovery_prefix, self.base, self.uid, self.args.name
        ):
            self.client.publish(topic, json.dumps(payload), qos=1, retain=True)
        self.client.publish(self.base + "/motion", "OFF", qos=1, retain=True)
        self.client.publish(self.base + "/availability", "online", qos=1, retain=True)

    def trigger_motion(self):
        with self.motion_lock:
            self.client.publish(self.base + "/motion", "ON", qos=1, retain=True)
            if self.motion_timer:
                self.motion_timer.cancel()
            self.motion_timer = threading.Timer(self.args.motion_hold, self.clear_motion)
            self.motion_timer.daemon = True
            self.motion_timer.start()

    def clear_motion(self):
        with self.motion_lock:
            self.client.publish(self.base + "/motion", "OFF", qos=1, retain=True)
            self.motion_timer = None

    def fetch_snapshot(self) -> bytes:
        request = urllib.request.Request(
            f"http://{self.args.camera}/snapshot.jpg", headers=self.camera_headers
        )
        with urllib.request.urlopen(request, timeout=self.args.camera_timeout) as response:
            content = response.read()
        if not content.startswith(b"\xff\xd8") or not content.endswith(b"\xff\xd9"):
            raise ValueError("camera did not return a complete JPEG")
        return content

    def snapshot_loop(self):
        last_state = None
        while not self.stop_event.is_set():
            try:
                image = self.fetch_snapshot()
                self.client.publish(self.base + "/snapshot", image, qos=0, retain=False)
                state = "online"
            except (OSError, ValueError, urllib.error.URLError) as error:
                print(f"snapshot error: {error}", flush=True)
                state = "offline"
            if state != last_state:
                self.client.publish(
                    self.base + "/availability", state, qos=1, retain=True
                )
                last_state = state
            self.stop_event.wait(self.args.snapshot_interval)

    def stop(self):
        self.stop_event.set()
        with self.motion_lock:
            if self.motion_timer:
                self.motion_timer.cancel()
                self.motion_timer = None
        self.client.publish(self.base + "/availability", "offline", qos=1, retain=True)


def make_handler(bridge: Bridge):
    class Handler(BaseHTTPRequestHandler):
        server_version = "GigasetMQTT/1.0"

        def reply(self, status: int, payload: dict):
            content = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def authorized(self) -> bool:
            if not bridge.args.http_token:
                return True
            query = self.path.partition("?")[2]
            supplied = ""
            for field in query.split("&"):
                key, _, value = field.partition("=")
                if key == "token":
                    supplied = value
            return supplied == bridge.args.http_token

        def dispatch(self):
            path = self.path.partition("?")[0]
            if path == "/health":
                self.reply(200, {"ok": True})
            elif path == "/motion":
                if not self.authorized():
                    self.reply(403, {"ok": False, "error": "invalid token"})
                    return
                bridge.trigger_motion()
                self.reply(200, {"ok": True, "motion": "ON"})
            else:
                self.reply(404, {"ok": False, "error": "not found"})

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


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera", default=env("CAMERA_IP"), required=not env("CAMERA_IP"))
    parser.add_argument("--mac", default=env("CAMERA_MAC"), required=not env("CAMERA_MAC"))
    parser.add_argument("--name", default=env("CAMERA_NAME", "Gigaset Camera"))
    parser.add_argument("--camera-user", default=env("CAMERA_USER", "admin"))
    parser.add_argument("--camera-password", default=env("CAMERA_PASSWORD"))
    parser.add_argument("--broker", default=env("MQTT_BROKER"), required=not env("MQTT_BROKER"))
    parser.add_argument("--mqtt-port", type=int, default=int(env("MQTT_PORT", "1883")))
    parser.add_argument("--mqtt-user", default=env("MQTT_USER"))
    parser.add_argument("--mqtt-password", default=env("MQTT_PASSWORD"))
    parser.add_argument("--mqtt-tls", action="store_true", default=env("MQTT_TLS") == "1")
    parser.add_argument("--topic", default=env("MQTT_TOPIC", "gigaset/camera"))
    parser.add_argument("--discovery-prefix", default=env("HA_DISCOVERY_PREFIX", "homeassistant"))
    parser.add_argument("--listen", default=env("HTTP_LISTEN", "0.0.0.0"))
    parser.add_argument("--http-port", type=int, default=int(env("HTTP_PORT", "8766")))
    parser.add_argument("--http-token", default=env("HTTP_TOKEN"))
    parser.add_argument("--motion-hold", type=float, default=float(env("MOTION_HOLD", "15")))
    parser.add_argument("--snapshot-interval", type=float, default=float(env("SNAPSHOT_INTERVAL", "10")))
    parser.add_argument("--camera-timeout", type=float, default=5.0)
    args = parser.parse_args()
    args.camera_password = args.camera_password or derive_admin_password(args.mac)
    return args


def main():
    args = parse_args()
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="gigaset-" + slugify(args.name),
    )
    if args.mqtt_user:
        client.username_pw_set(args.mqtt_user, args.mqtt_password)
    if args.mqtt_tls:
        client.tls_set()
    client.will_set(args.topic.rstrip("/") + "/availability", "offline", 1, True)
    bridge = Bridge(args, client)

    def connected(_client, _userdata, _flags, reason_code, _properties):
        if reason_code == 0:
            print("MQTT connected", flush=True)
            bridge.publish_discovery()
        else:
            print(f"MQTT connection failed: {reason_code}", flush=True)

    client.on_connect = connected
    client.connect(args.broker, args.mqtt_port, keepalive=60)
    client.loop_start()
    server = ThreadingHTTPServer((args.listen, args.http_port), make_handler(bridge))
    snapshot_thread = threading.Thread(target=bridge.snapshot_loop, daemon=True)
    snapshot_thread.start()

    def shutdown(_signal=None, _frame=None):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    token = f"?token={args.http_token}" if args.http_token else ""
    print(f"Motion endpoint: http://<bridge-ip>:{args.http_port}/motion{token}", flush=True)
    print("Camera HTTP alarm: authorization No, URL motion" + token, flush=True)
    try:
        server.serve_forever()
    finally:
        bridge.stop()
        server.server_close()
        client.disconnect()
        client.loop_stop()


if __name__ == "__main__":
    main()
