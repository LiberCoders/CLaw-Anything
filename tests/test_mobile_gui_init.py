from __future__ import annotations

import importlib
import json
from pathlib import Path

gui_init = importlib.import_module("claw_anything.task.mobile_gui.init_gui_task")


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
