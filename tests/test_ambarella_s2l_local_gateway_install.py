import json
from pathlib import Path

import pytest

from ambarella_s2l.local_gateway_install import (
    AUTORUN_NAME,
    FAILED_AUTORUN_NAME,
    PAYLOAD_DIR_NAME,
    build_autorun,
    prepare_install,
)


def test_autorun_validates_and_rolls_back() -> None:
    script = build_autorun()

    assert "/usr/bin/lua" in script
    assert "/usr/sbin/lighttpd -tt -f /etc/lighttpd/lighttpd.conf" in script
    assert "/bin/chmod 755 /usr/local/bin/cec_init.sh" in script
    assert "cec_init.sh.gigaset-stock" in script
    assert FAILED_AUTORUN_NAME in script
    assert f'/{AUTORUN_NAME}"' in script
    assert script.endswith("exit 1\n")
    assert "\r" not in script


def test_prepare_install_is_source_only_and_read_back_verified(tmp_path: Path) -> None:
    written = prepare_install(tmp_path)
    payload = tmp_path / PAYLOAD_DIR_NAME

    assert tmp_path / AUTORUN_NAME in written
    assert (payload / "install.lua").is_file()
    assert (payload / "gigaset_local_gateway.sh").is_file()
    assert (payload / "wifi_setup.cgi").is_file()
    assert (payload / "index.html").is_file()
    assert not any(path.suffix.lower() in {".bin", ".ubi"} for path in tmp_path.rglob("*"))

    manifest = json.loads((payload / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["format"] == "gigaset-elements-camera-local-gateway"
    assert set(manifest["files"]) == {
        "install.lua",
        "gigaset_local_gateway.sh",
        "wifi_setup.cgi",
        "index.html",
    }

    with pytest.raises(FileExistsError):
        prepare_install(tmp_path)


def test_prepare_install_replace_removes_only_its_own_files(tmp_path: Path) -> None:
    unrelated = tmp_path / "keep.txt"
    unrelated.write_text("keep", encoding="utf-8")
    prepare_install(tmp_path)
    prepare_install(tmp_path, replace=True)

    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert (tmp_path / AUTORUN_NAME).is_file()
