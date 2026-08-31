from pathlib import Path

import pytest

from ambarella_s2l.wifi_provision import AUTORUN_NAME, build_autorun, write_autorun


def test_autorun_uses_stock_persistent_config_and_self_removes() -> None:
    script = build_autorun('Lab "2G"', r"safe\password")

    assert "CONFIG_FILE=/var/wifi/wifi.conf" in script
    assert 'ssid="Lab \\"2G\\""' in script
    assert 'psk="safe\\\\password"' in script
    assert f'/bin/rm -f "$SERVICE_DIR/{AUTORUN_NAME}"' in script
    assert script.endswith("/sbin/reboot -f\n")
    assert "\r" not in script


@pytest.mark.parametrize(
    ("ssid", "password", "message"),
    [
        ("", "12345678", "SSID must not be empty"),
        ("x" * 33, "12345678", "SSID is longer"),
        ("camera", "short", "at least 8"),
        ("camera\nother", "12345678", "control characters"),
    ],
)
def test_autorun_rejects_invalid_input(ssid: str, password: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build_autorun(ssid, password)


def test_write_autorun_verifies_and_refuses_overwrite(tmp_path: Path) -> None:
    destination = write_autorun(tmp_path, "CameraNet", "12345678")
    assert destination.name == AUTORUN_NAME
    assert destination.read_bytes().startswith(b"#!/bin/sh\n")

    with pytest.raises(FileExistsError):
        write_autorun(tmp_path, "CameraNet", "abcdefgh")

    write_autorun(tmp_path, "CameraNet", "abcdefgh", replace=True)
    assert 'psk="abcdefgh"' in destination.read_text(encoding="utf-8")
