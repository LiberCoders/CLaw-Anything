from __future__ import annotations

import importlib
import json

import pytest

gui_init = importlib.import_module("claw_anything.task.mobile_gui.init_gui_task")


def test_shadow_app_state_falls_back_when_package_is_missing(tmp_path, monkeypatch) -> None:
    fixture = tmp_path / "products.json"
    fixture.write_text(
        json.dumps([{"product_id": "PROD-1", "name": "Gift"}]),
        encoding="utf-8",
    )

    shells: list[str] = []
    pushed: list[tuple[str, dict]] = []

    def fake_adb_shell(cmd: str, device=None, root_required: bool = False):
        shells.append(cmd)
        return True, ""

    def fake_adb_push(local: str, remote: str, device=None, root_required: bool = False) -> bool:
        pushed.append((remote, json.loads(open(local, encoding="utf-8").read())))
        return True

    monkeypatch.setattr(gui_init, "adb_shell", fake_adb_shell)
    monkeypatch.setattr(gui_init, "adb_push", fake_adb_push)
    monkeypatch.setattr(gui_init, "_package_installed", lambda package, device=None: (False, "missing"))
    monkeypatch.setattr(
        gui_init,
        "_start_package_launcher",
        lambda *args, **kwargs: pytest.fail("missing shadow app package should not be launched"),
    )

    ok = gui_init._inject_shadow_app_state(
        {"fixture": "products.json"},
        tmp_path,
        None,
        package="com.testmall.app",
        state_path="/sdcard/Android/data/com.testmall.app/files/state.json",
        state_key="products",
        default_fixture="fixtures/gui/testmall_gui/products.json",
        app_label="TestMall",
    )

    assert ok
    assert pushed == [
        (
            "/data/local/tmp/claw_gui_state/com.testmall.app/state.json",
            {"products": [{"product_id": "PROD-1", "name": "Gift"}]},
        )
    ]
    assert "rm -f /data/local/tmp/claw_gui_state/com.testmall.app/state.json" in shells
