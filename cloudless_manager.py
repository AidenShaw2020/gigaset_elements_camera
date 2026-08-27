#!/usr/bin/env python3
"""Local manager for discontinued Gigaset/Y-cam Gen1 cameras.

It uses the camera's native authenticated Webs form to disable the retired
cloud client. It does not upload firmware or execute commands on the camera.
"""

from __future__ import annotations

import argparse
import base64
import html
import io
import json
import re
import secrets
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


MAX_RESPONSE = 2 * 1024 * 1024


def normalize_mac(mac: str) -> str:
    value = re.sub(r"[^0-9A-Fa-f]", "", mac).upper()
    if len(value) != 12:
        raise ValueError("MAC address must contain exactly 12 hexadecimal digits")
    return value


def derive_admin_password(mac: str) -> str:
    value = normalize_mac(mac)
    material = "LUCKOTVF" + value[::-1] + "YCAMVF"
    return base64.b64encode(material.encode("ascii")).decode("ascii")


def normalize_camera(value: str) -> str:
    value = value.strip()
    if "://" not in value:
        value = "http://" + value
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "http" or not parsed.hostname:
        raise ValueError("camera must be an HTTP host or IPv4 address")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("camera address must not contain credentials or a query")
    if parsed.path not in ("", "/"):
        raise ValueError("camera address must not contain a path")
    host = parsed.hostname
    if parsed.port:
        host += ":%d" % parsed.port
    return "http://" + host


def parse_cloud_state(document: str) -> dict[str, object]:
    enabled = None
    for tag in re.findall(r"<input\b[^>]*>", document, flags=re.IGNORECASE):
        if not re.search(r"\bname\s*=\s*['\"]?ENABLE\b", tag, flags=re.IGNORECASE):
            continue
        value = re.search(
            r"\bvalue\s*=\s*['\"]?([^\s'\">]+)", tag, flags=re.IGNORECASE
        )
        if value and re.search(r"\bchecked\b", tag, flags=re.IGNORECASE):
            enabled = value.group(1).lower() == "enable"
            break

    def input_value(name: str) -> str:
        pattern = (
            r"<input\b(?=[^>]*\bname\s*=\s*['\"]?"
            + re.escape(name)
            + r"\b)[^>]*\bvalue\s*=\s*['\"]([^'\"]*)['\"][^>]*>"
        )
        match = re.search(pattern, document, flags=re.IGNORECASE)
        return html.unescape(match.group(1)) if match else ""

    return {
        "enabled": enabled,
        "server": input_value("SERVER"),
        "port": input_value("PORT"),
    }


def _set_ini_value(document: str, section: str, key: str, value: str) -> str:
    section_match = re.search(
        r"(?ms)^\[" + re.escape(section) + r"\]\s*$.*?(?=^\[|\Z)", document
    )
    if not section_match:
        raise RuntimeError("camera backup lacks [%s]" % section)
    block = section_match.group(0)
    pattern = re.compile(r"(?m)^(" + re.escape(key) + r")=.*$")
    changed, count = pattern.subn(r"\1=" + value, block, count=1)
    if count != 1:
        raise RuntimeError("camera backup lacks [%s] %s" % (section, key))
    return document[: section_match.start()] + changed + document[section_match.end() :]


def patch_backup_cloudless(backup: bytes) -> bytes:
    """Patch only cloud startup keys in a camera-created config backup."""
    marker = backup.find(b"ustar")
    offset = marker - 257
    if marker < 257 or offset > 512:
        raise RuntimeError("camera backup does not contain a valid ustar archive")
    prefix = backup[:offset]
    with tarfile.open(fileobj=io.BytesIO(backup[offset:]), mode="r:*") as source:
        members: list[tuple[tarfile.TarInfo, bytes | None]] = []
        found = False
        for member in source.getmembers():
            if member.name.startswith("/") or ".." in member.name.split("/"):
                raise RuntimeError("unsafe path in camera backup")
            stream = source.extractfile(member) if member.isfile() else None
            payload = stream.read() if stream else None
            if member.name == "sys.conf":
                if payload is None or found:
                    raise RuntimeError("invalid sys.conf in camera backup")
                config = payload.decode("latin1")
                config = _set_ini_value(config, "startup", "otproxyc", "manual")
                config = _set_ini_value(config, "procspy", "psl_client", "disable")
                config = _set_ini_value(config, "otproxyc", "ENABLE", "disable")
                config = _set_ini_value(config, "otproxyc", "SERVER", "")
                payload = config.encode("latin1")
                found = True
            members.append((member, payload))
    if not found:
        raise RuntimeError("camera backup lacks sys.conf")

    rebuilt = io.BytesIO()
    with tarfile.open(fileobj=rebuilt, mode="w", format=tarfile.USTAR_FORMAT) as target:
        for member, payload in members:
            if payload is not None:
                member.size = len(payload)
                target.addfile(member, io.BytesIO(payload))
            else:
                target.addfile(member)
    return prefix + rebuilt.getvalue()


class CameraClient:
    def __init__(
        self,
        camera: str,
        mac: str,
        username: str = "admin",
        password: str | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.base_url = normalize_camera(camera)
        self.mac = normalize_mac(mac)
        self.username = username
        self.password = password or derive_admin_password(self.mac)
        self.timeout = timeout

    def _authorization(self) -> str:
        token = base64.b64encode(
            (self.username + ":" + self.password).encode("utf-8")
        ).decode("ascii")
        return "Basic " + token

    def _request(
        self, path: str, fields: dict[str, str] | None = None
    ) -> tuple[int, bytes]:
        if not path.startswith("/"):
            raise ValueError("camera path must be absolute")
        data = None
        method = "GET"
        headers = {"User-Agent": "gigaset-camera-cloudless/0.1"}
        if fields is not None:
            data = urllib.parse.urlencode(fields).encode("ascii")
            method = "POST"
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["Authorization"] = self._authorization()
        request = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read(MAX_RESPONSE + 1)
                if len(body) > MAX_RESPONSE:
                    raise RuntimeError("camera returned an unexpectedly large response")
                return response.status, body
        except urllib.error.HTTPError as exc:
            detail = exc.read(512).decode("latin1", errors="replace")
            if exc.code == 401:
                raise RuntimeError("camera rejected the web credentials") from exc
            raise RuntimeError("camera HTTP %d: %s" % (exc.code, detail)) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError("camera is unreachable: %s" % exc.reason) from exc

    def cloud_state(self) -> dict[str, object]:
        status, body = self._request("/en/otproxyc.asp")
        if status != 200:
            raise RuntimeError("unexpected camera status %d" % status)
        state = parse_cloud_state(body.decode("latin1", errors="replace"))
        state["reachable"] = True
        return state

    def set_cloud(self, enabled: bool) -> dict[str, object]:
        if not enabled:
            return self.install_cloudless()
        fields = {
            "ENABLE": "enable",
            "SERVER": "cam-dx.gigaset-elements.com",
            "PORT": "8000",
        }
        self._request("/form/otproxycSetupApply", fields)
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            time.sleep(0.4)
            state = self.cloud_state()
            if state.get("enabled") is enabled:
                return state
        raise RuntimeError("camera accepted the request but verification failed")

    def backup_config(self) -> bytes:
        _, body = self._request("/form/setting", {"BACKUP": "Backup"})
        if len(body) < 1024 or b"ustar" not in body:
            raise RuntimeError("camera returned an invalid configuration backup")
        return body

    def restore_config(self, backup: bytes) -> None:
        boundary = "----GigasetCloudless" + secrets.token_hex(12)
        chunks = [
            "--%s\r\n" % boundary,
            'Content-Disposition: form-data; name="filename"; filename="config.cfg"\r\n',
            "Content-Type: application/octet-stream\r\n\r\n",
        ]
        body = "".join(chunks).encode("ascii") + backup
        body += (
            "\r\n--%s\r\n" % boundary
            + 'Content-Disposition: form-data; name="RESTORE"\r\n\r\n'
            + "Restore\r\n--%s--\r\n" % boundary
        ).encode("ascii")
        request = urllib.request.Request(
            self.base_url + "/form/restore",
            data=body,
            headers={
                "Authorization": self._authorization(),
                "Content-Type": "multipart/form-data; boundary=" + boundary,
                "Content-Length": str(len(body)),
                "User-Agent": "gigaset-camera-cloudless/0.1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=max(self.timeout, 15)) as response:
                response.read(MAX_RESPONSE)
        except urllib.error.URLError as exc:
            raise RuntimeError("configuration restore failed: %s" % exc.reason) from exc

    def reboot(self) -> None:
        try:
            self._request("/form/reboot", {})
        except (RuntimeError, TimeoutError):
            # The old Webs server often closes the connection during reboot.
            pass

    def install_cloudless(self) -> dict[str, object]:
        original = self.backup_config()
        patched = patch_backup_cloudless(original)
        self.restore_config(patched)
        self.reboot()
        return {
            "enabled": False,
            "server": "",
            "port": "8000",
            "reachable": False,
            "rebooting": True,
        }


def render_page(client: CameraClient, token: str) -> bytes:
    camera = html.escape(client.base_url)
    links = [
        ("Live view", "/en/main.asp"),
        ("Network", "/en/ethernet.asp"),
        ("Wi-Fi", "/en/wlan.asp"),
        ("Video and audio", "/en/camera.asp"),
        ("Streams", "/en/stream.asp"),
        ("Motion detection", "/en/motiondect.asp"),
        ("Recording", "/en/storage.asp"),
        ("Time", "/en/clock.asp"),
        ("System information", "/en/sysinfo.asp"),
        ("Original settings", "/en/setting.asp"),
    ]
    cards = "".join(
        '<a class="card" target="_blank" rel="noreferrer" href="%s%s">%s</a>'
        % (camera, path, html.escape(label))
        for label, path in links
    )
    source = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Gigaset Camera Cloudless</title>
<style>:root{color-scheme:dark;--bg:#0c1117;--panel:#151d27;--line:#2a394a;--text:#eaf1f8;--muted:#9fb0c2;--ok:#56d890;--bad:#ff7b72;--accent:#6cb6ff}*{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,#0c1117,#101c29);color:var(--text);font:16px system-ui,sans-serif}.wrap{max-width:980px;margin:auto;padding:32px 20px}h1{font-size:28px;margin:0 0 6px}.sub{color:var(--muted);margin:0 0 24px}.panel{background:rgba(21,29,39,.94);border:1px solid var(--line);border-radius:14px;padding:20px;margin:16px 0}.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.grow{flex:1}.status{font-weight:700}.unknown{color:var(--muted)}.ok{color:var(--ok)}.bad{color:var(--bad)}button{border:0;border-radius:9px;padding:11px 15px;font-weight:700;cursor:pointer;background:var(--accent);color:#07121e}button.secondary{background:#263648;color:var(--text)}button:disabled{opacity:.5;cursor:wait}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}.card{display:block;padding:14px;border:1px solid var(--line);border-radius:9px;color:var(--text);text-decoration:none;background:#101821}.card:hover{border-color:var(--accent)}code,input{background:#0b1219;color:var(--text);border:1px solid var(--line);padding:7px;border-radius:5px}input{width:min(330px,100%)}.note{color:var(--muted);font-size:14px}.error{white-space:pre-wrap;color:var(--bad)}</style></head><body><main class="wrap">
<h1>Gigaset Camera Cloudless</h1><p class="sub">Local control for a discontinued Gen1 camera. The manager only talks to <code>CAMERA</code>.</p>
<section class="panel"><div class="row"><div class="grow"><div>Retired cloud client</div><div id="status" class="status unknown">Checking…</div></div><button id="disable">Disable cloud &amp; reboot</button><button id="refresh" class="secondary">Refresh</button></div><p id="detail" class="note"></p><div id="error" class="error"></div></section>
<section class="panel"><h2>Camera settings</h2><p class="note">These links open the camera's original local pages. Use <code>admin</code> and the password shown below.</p><div class="grid">CARDS</div></section>
<section class="panel"><h2>Local web login</h2><div class="row"><span>User <code>admin</code></span><label for="password">Password</label><input id="password" type="password" readonly value="PASSWORD"><button id="reveal" class="secondary">Reveal</button><button id="copy" class="secondary">Copy password</button></div><p class="note">The password is derived locally from the camera MAC address and is not sent anywhere except to this camera.</p></section>
</main><script>const token="TOKEN";const s=document.querySelector('#status'),d=document.querySelector('#detail'),e=document.querySelector('#error'),p=document.querySelector('#password');async function call(a,m='GET'){e.textContent='';const r=await fetch(a,{method:m,headers:{'X-Cloudless-Token':token}}),j=await r.json();if(!r.ok)throw new Error(j.error||('HTTP '+r.status));return j}function render(x){const k=x.enabled===true||x.enabled===false;s.textContent=x.rebooting?'Cloudless configuration installed — camera is rebooting…':(!k?'State unavailable':(x.enabled?'ACTIVE — camera is contacting the retired service':'DISABLED — local-only mode'));s.className='status '+(!k?'unknown':(x.enabled?'bad':'ok'));d.textContent=x.rebooting?'Wait about 30 seconds, then press Refresh.':'Server: '+(x.server||'none')+(x.port?' · Port: '+x.port:'');document.querySelector('#disable').disabled=x.enabled===false}async function refresh(){try{render(await call('/api/status'))}catch(x){s.textContent='Camera unavailable';s.className='status bad';e.textContent=x.message}}document.querySelector('#refresh').onclick=refresh;document.querySelector('#disable').onclick=async x=>{if(!confirm('The manager will back up the current settings, disable only the retired cloud client, restore the configuration and reboot the camera. Continue?'))return;x.target.disabled=true;try{render(await call('/api/cloud/disable','POST'))}catch(y){e.textContent=y.message}finally{x.target.disabled=false}};document.querySelector('#reveal').onclick=x=>{p.type=p.type==='password'?'text':'password';x.target.textContent=p.type==='password'?'Reveal':'Hide'};document.querySelector('#copy').onclick=()=>navigator.clipboard.writeText(p.value);refresh();</script></body></html>"""
    return (
        source.replace("CAMERA", camera)
        .replace("CARDS", cards)
        .replace("PASSWORD", html.escape(client.password))
        .replace("TOKEN", token)
        .encode("utf-8")
    )


class ManagerHandler(BaseHTTPRequestHandler):
    server_version = "GigasetCloudless/0.1"

    @property
    def app(self) -> "ManagerServer":
        return self.server  # type: ignore[return-value]

    def log_message(self, fmt: str, *args: object) -> None:
        print("manager: " + (fmt % args))

    def _json(self, status: int, value: dict[str, object]) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        return secrets.compare_digest(
            self.headers.get("X-Cloudless-Token", ""), self.app.token
        )

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/":
            body = render_page(self.app.client, self.app.token)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/status":
            if not self._authorized():
                self._json(403, {"error": "invalid manager token"})
                return
            try:
                self._json(200, self.app.client.cloud_state())
            except Exception as exc:
                self._json(502, {"error": str(exc)})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if not self._authorized():
            self._json(403, {"error": "invalid manager token"})
        elif path != "/api/cloud/disable":
            self._json(404, {"error": "not found"})
        else:
            try:
                self._json(200, self.app.client.set_cloud(False))
            except Exception as exc:
                self._json(502, {"error": str(exc)})


class ManagerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], client: CameraClient):
        super().__init__(address, ManagerHandler)
        self.client = client
        self.token = secrets.token_urlsafe(24)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a local web manager for a Gigaset/Y-cam Gen1 camera"
    )
    parser.add_argument("--camera", required=True, help="camera IPv4 address")
    parser.add_argument("--mac", required=True, help="camera MAC address")
    parser.add_argument("--password", help="override the derived web password")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if args.bind not in ("127.0.0.1", "::1", "localhost"):
        raise ValueError("the manager intentionally binds to localhost only")
    client = CameraClient(args.camera, args.mac, password=args.password)
    server = ManagerServer((args.bind, args.port), client)
    url = "http://%s:%d/" % (args.bind, server.server_address[1])
    print("manager=" + url)
    print("camera=" + client.base_url)
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
