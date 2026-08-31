import os
from pathlib import Path

from ambarella_s2l.patch_rootfs import (
    AUTH_ANCHOR,
    CEC_ANCHOR,
    CGI_ANCHOR,
    REWRITE_ANCHOR,
    patch_rootfs,
)


def make_rootfs(tmp_path: Path) -> tuple[Path, Path]:
    rootfs = tmp_path / "rootfs"
    overlay = tmp_path / "overlay"
    (rootfs / "usr/local/bin").mkdir(parents=True)
    (rootfs / "etc/lighttpd").mkdir(parents=True)
    (overlay / "usr/local/bin").mkdir(parents=True)
    (overlay / "webSvr/web/cgi-bin").mkdir(parents=True)

    (rootfs / "usr/local/bin/cec_init.sh").write_text(
        "#!/bin/sh\n" + CEC_ANCHOR,
        encoding="utf-8",
    )
    (rootfs / "usr/local/bin/wifi_setup.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (rootfs / "etc/lighttpd/lighttpd.conf").write_text(
        "\n".join((REWRITE_ANCHOR, CGI_ANCHOR, AUTH_ANCHOR, "}")) + "\n",
        encoding="utf-8",
    )
    (overlay / "usr/local/bin/gigaset_local_gateway.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (overlay / "webSvr/web/cgi-bin/wifi_setup.cgi").write_text("#!/usr/bin/lua\n", encoding="utf-8")
    return rootfs, overlay


def test_patch_is_strict_and_idempotent(tmp_path: Path) -> None:
    rootfs, overlay = make_rootfs(tmp_path)
    patch_rootfs(rootfs, overlay)
    patch_rootfs(rootfs, overlay)

    cec = (rootfs / "usr/local/bin/cec_init.sh").read_text(encoding="utf-8")
    lighttpd = (rootfs / "etc/lighttpd/lighttpd.conf").read_text(encoding="utf-8")
    assert cec.count("gigaset-elements-camera local gateway") == 1
    assert "/usr/local/bin/gigaset_local_gateway.sh monitor &" in cec
    assert "cgi-bin/wifi_setup\\.cgi" in lighttpd
    assert 'cgi.assign = (".cgi" => "")' in lighttpd

    if os.name != "nt":
        assert (rootfs / "usr/local/bin/gigaset_local_gateway.sh").stat().st_mode & 0o111
        assert (rootfs / "webSvr/web/cgi-bin/wifi_setup.cgi").stat().st_mode & 0o111


def test_gateway_uses_camera_stock_ap_interface() -> None:
    gateway = (
        Path(__file__).parents[1]
        / "ambarella_s2l/rootfs_overlay/usr/local/bin/gigaset_local_gateway.sh"
    ).read_text(encoding="utf-8")

    assert "AP_IF=wlan0" in gateway
    assert '/usr/local/bin/wifi_switch.sh ap "$ssid" 6' in gateway
    assert "/var/run/wpa_supplicant /var/wifi/wpa_supplicant" in gateway
    assert "wifi_setup.sh ap" not in gateway
    assert "PERSISTENT_LOG=/dev/adc/gigaset-local-gateway.log" in gateway
