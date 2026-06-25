from __future__ import annotations

import json
import importlib
from pathlib import Path

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
