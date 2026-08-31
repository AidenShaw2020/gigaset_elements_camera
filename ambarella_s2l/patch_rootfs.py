#!/usr/bin/env python3
"""Apply the source-only Gigaset S2L local gateway overlay to an extracted rootfs."""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path


START_MARKER = "# gigaset-elements-camera local gateway"
CEC_ANCHOR = """\tfi

\tif [ -e /dev/adc/OTA_upgrade ]
"""
CEC_REPLACEMENT = """\tfi

\t# gigaset-elements-camera local gateway
\t/usr/local/bin/gigaset_local_gateway.sh monitor &

\tif [ -e /dev/adc/OTA_upgrade ]
"""

REWRITE_ANCHOR = 'url.rewrite-once = ( "[Ff]+[Oo]+[Rr]+[Mm]+/(.*)" => "/Form/$1" )'
REWRITE_REPLACEMENT = """url.rewrite-once = (
  "[Ff]+[Oo]+[Rr]+[Mm]+/(.*)" => "/Form/$1",
  "^/(generate_204|gen_204|hotspot-detect.html|connecttest.txt|ncsi.txt)$" => "/setup/index.html",
  "^/library/test/success.html$" => "/setup/index.html"
)"""

CGI_ANCHOR = '#cgi.assign = (".cgi" => "",".py" => "/usr/bin/python")'
CGI_REPLACEMENT = 'cgi.assign = (".cgi" => "")'

AUTH_ANCHOR = '$HTTP["url"] =~ "/" {'
AUTH_REPLACEMENT = (
    '$HTTP["url"] !~ "^/(setup/|cgi-bin/wifi_setup\\.cgi|generate_204|gen_204|'
    'hotspot-detect\\.html|library/test/success\\.html|connecttest\\.txt|ncsi\\.txt)" {'
)


def replace_once(text: str, old: str, new: str, description: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(
            f"expected exactly one {description} anchor, found {count}; "
            "this firmware revision is not supported safely"
        )
    return text.replace(old, new, 1)


def copy_overlay(overlay: Path, rootfs: Path) -> None:
    for source in overlay.rglob("*"):
        relative = source.relative_to(overlay)
        destination = rootfs / relative
        if source.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination, follow_symlinks=False)


def patch_rootfs(rootfs: Path, overlay: Path | None = None) -> None:
    if os.name == "nt" and overlay is None:
        raise RuntimeError("run this patcher inside Linux so rootfs modes and symlinks are preserved")
    rootfs = rootfs.resolve()
    required = [
        rootfs / "usr/local/bin/cec_init.sh",
        rootfs / "etc/lighttpd/lighttpd.conf",
        rootfs / "usr/local/bin/wifi_setup.sh",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError("not an extracted supported S2L rootfs; missing: " + ", ".join(missing))

    overlay = overlay or Path(__file__).with_name("rootfs_overlay")
    copy_overlay(overlay, rootfs)

    cec_path = rootfs / "usr/local/bin/cec_init.sh"
    cec = cec_path.read_text(encoding="utf-8")
    if START_MARKER not in cec:
        cec = replace_once(cec, CEC_ANCHOR, CEC_REPLACEMENT, "cec_init")
        cec_path.write_text(cec, encoding="utf-8", newline="\n")

    lighttpd_path = rootfs / "etc/lighttpd/lighttpd.conf"
    lighttpd = lighttpd_path.read_text(encoding="utf-8")
    if "cgi-bin/wifi_setup\\.cgi" not in lighttpd:
        lighttpd = replace_once(lighttpd, REWRITE_ANCHOR, REWRITE_REPLACEMENT, "URL rewrite")
        lighttpd = replace_once(lighttpd, CGI_ANCHOR, CGI_REPLACEMENT, "CGI assignment")
        lighttpd = replace_once(lighttpd, AUTH_ANCHOR, AUTH_REPLACEMENT, "authentication")
        lighttpd_path.write_text(lighttpd, encoding="utf-8", newline="\n")

    executable = [
        rootfs / "usr/local/bin/gigaset_local_gateway.sh",
        rootfs / "webSvr/web/cgi-bin/wifi_setup.cgi",
    ]
    for path in executable:
        path.chmod(0o755)
    setup_page = rootfs / "webSvr/web/setup/index.html"
    if setup_page.is_file():
        setup_page.chmod(0o644)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rootfs", type=Path, help="root of the Linux filesystem extracted by ubi-reader")
    parser.add_argument("--overlay", type=Path, help="override the packaged overlay (for testing)")
    args = parser.parse_args()
    patch_rootfs(args.rootfs, args.overlay)
    print(f"Patched S2L rootfs: {args.rootfs.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
