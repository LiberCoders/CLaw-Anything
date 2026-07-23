from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

gui_init = importlib.import_module("claw_anything.task.mobile_gui.init_gui_task")


def test_my_expenses_normalizes_hex_account_colors() -> None:
    fixture = (
        Path(__file__).resolve().parents[1]
        / "benchmark/gui/TGUI30_gmail_scheduler_expense_budget_verify"
        / "fixtures/gui/my_expenses_gui/data.json"
    )
    raw = json.loads(fixture.read_text(encoding="utf-8"))

    data = gui_init._normalize_my_expenses_data(raw)

    assert data["accounts"][0]["color"] == gui_init._hex_to_android_color("#1976D2", -3355444)
    assert all(isinstance(acc["color"], int) for acc in data["accounts"])


def test_my_expenses_normalizes_hex_colors_in_label_based_fixtures() -> None:
    raw = {
        "accounts": [
            {
                "label": "Wallet",
                "currency": "CNY",
                "type": "cash",
                "color": "#388E3C",
            }
        ],
        "categories": [{"label": "Meals", "type": "expense"}],
        "payees": ["Cafe"],
        "transactions": [
            {
                "account": "Wallet",
                "date": "2026-04-01",
                "amount": -1200,
                "category": "Meals",
                "payee": "Cafe",
            }
        ],
    }

    data = gui_init._normalize_my_expenses_data(raw)

    assert data["accounts"][0]["type"] == "CASH"
    assert data["accounts"][0]["color"] == gui_init._hex_to_android_color("#388E3C", -3355444)


def test_my_expenses_multi_container_duplicates_keep_latest_record() -> None:
    fixture = (
        Path(__file__).resolve().parents[1]
        / "benchmark/gui/TGUI51_incident_dinner_expense_calendar"
        / "fixtures/gui/my_expenses_gui/data.json"
    )
    raw = json.loads(fixture.read_text(encoding="utf-8"))

    data = gui_init._normalize_my_expenses_data(raw)
    txn = [
        tx for tx in data["transactions"]
        if tx.get("transaction_id") == "txn_001"
    ]

    assert len(txn) == 1
    assert txn[0]["payee"] == "Sichuan Restaurant - Xujiahui"
    assert txn[0]["account"] == "Personal Credit Card"
    assert txn[0]["category"] == "On-Call Incident Response"


def test_fossify_messages_normalizes_mixed_tgui46_fixture() -> None:
    fixture = (
        Path(__file__).resolve().parents[1]
        / "benchmark/gui/TGUI46_job_failure_triage_workmail_sms_notes"
        / "fixtures/gui/fossify_messages_gui/threads.json"
    )
    raw = json.loads(fixture.read_text(encoding="utf-8"))

    threads = gui_init._normalize_fossify_messages(raw)

    assert len(threads) >= 3
    assert all(thread["participants"] for thread in threads if thread["messages"])
    assert all(msg["text"] for thread in threads for msg in thread["messages"])
    assert any("JOB-" in msg["text"] for thread in threads for msg in thread["messages"])


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
