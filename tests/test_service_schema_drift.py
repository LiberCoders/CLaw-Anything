"""Regression tests for the service-wide schema-drift bug class.

Same root cause as the sheet fix (see test_sheet_fixture_normalization.py): a
mock service reads a fixture field — for lookups or returned data — that the
actual fixtures never populate, so reads silently return empty / not-found (or,
worse, 500). These tests pin the read-path aliases / load-time normalization
added to slide, claw_zotero, meeting, claw_wechat, claw_qq, claw_feishu,
claw_zhihu and scheduler.

Each service loads its fixture at import time from an env var, so we write a
crafted fixture to a tmp file, point the env var at it, and reload the module
before driving it with a FastAPI TestClient.
"""

from __future__ import annotations

import importlib
import json
import os

from fastapi.testclient import TestClient


def _reload(module_path: str, env: dict[str, str]):
    os.environ.update(env)
    os.environ.setdefault("EXECUTION_DATE", "2026-06-10")
    mod = importlib.import_module(module_path)
    return importlib.reload(mod)


def _write(tmp_path, name, data) -> str:
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return str(p)


# --- slide: content-based fixtures -> element-based read model ---------------


def test_slide_content_surfaces_as_element_and_field(tmp_path):
    f = _write(tmp_path, "decks.json", [{
        "deck_id": "DECK-1", "name": "D", "owner": "o", "last_modified": "2026-03-10",
        "slides": [{"slide_number": 1, "title": "T", "content": "Body text here", "notes": "n"}],
    }])
    S = _reload("mock_services.slide.server", {"SLIDE_FIXTURES": f})
    c = TestClient(S.app)
    decks = c.post("/slide/decks").json()["decks"]
    # last_modified bridged onto last_updated.
    assert decks[0]["last_updated"] == "2026-03-10"
    gs = c.post("/slide/get_slide", json={"deck_id": "DECK-1", "slide_index": 1}).json()
    assert gs["content"] == "Body text here"
    assert len(gs["elements"]) == 1
    assert gs["elements"][0]["content"] == "Body text here"
    assert gs["layout"] == "title_content"  # defaulted when absent


def test_slide_existing_elements_untouched(tmp_path):
    f = _write(tmp_path, "decks.json", [{
        "deck_id": "DECK-1", "name": "D", "owner": "o", "last_updated": "2026-01-01",
        "slides": [{"title": "T", "layout": "blank",
                    "elements": [{"element_id": "E005", "type": "text", "content": "kept"}]}],
    }])
    S = _reload("mock_services.slide.server", {"SLIDE_FIXTURES": f})
    c = TestClient(S.app)
    gs = c.post("/slide/get_slide", json={"deck_id": "DECK-1", "slide_index": 1}).json()
    assert [e["element_id"] for e in gs["elements"]] == ["E005"]
    assert gs["layout"] == "blank"


# --- claw_zotero: synthesize collections from item collection_ids ------------


def test_zotero_collections_synthesized_from_items(tmp_path):
    f = _write(tmp_path, "library.json", [
        {"item_id": "ZOT-0001", "item_type": "article", "title": "A",
         "collection_ids": ["COL-x", "COL-y"]},
        {"item_id": "ZOT-0002", "item_type": "book", "title": "B",
         "collection_ids": ["COL-x"]},
    ])
    Z = _reload("mock_services.claw_zotero.server", {"CLAW_ZOTERO_FIXTURES": f})
    c = TestClient(Z.app)
    cols = c.post("/claw_zotero/collections").json()
    ids = {x["collection_id"] for x in cols["collections"]}
    assert ids == {"COL-x", "COL-y"}
    counts = {x["collection_id"]: x["item_count"] for x in cols["collections"]}
    assert counts["COL-x"] == 2 and counts["COL-y"] == 1
    # add_to_collection resolves a synthesized collection instead of 404ing.
    r = c.post("/claw_zotero/items/add_to_collection",
               json={"item_id": "ZOT-0002", "collection_id": "COL-y"}).json()
    assert r["status"] == "added_to_collection"


# --- meeting: recordings date filter falls back across timestamp keys --------


def test_meeting_recordings_date_filter_uses_fallback_keys(tmp_path):
    f = _write(tmp_path, "meetings.json", [{
        "meeting_id": "MTG-1", "topic": "Sync", "host": "h",
        "recordings": [
            {"recording_id": "REC-1", "uploaded_at": "2026-02-10T16:05:00+01:00"},
            {"recording_id": "REC-2", "created_at": "2026-03-01T09:00:00"},
        ],
    }])
    M = _reload("mock_services.meeting.server", {"MEETING_FIXTURES": f})
    c = TestClient(M.app)
    r = c.post("/meeting/recordings",
               json={"start_date": "2026-01-01", "end_date": "2026-12-31"}).json()
    assert r["total"] == 2  # both kept (were dropped before: no recorded_at)
    assert all(rec.get("recorded_at") for rec in r["recordings"])


# --- claw_wechat / claw_qq: chat list muted + last-activity aliases ----------


def _chat_fixture(tmp_path, name):
    return _write(tmp_path, name, [{
        "chat_id": "C1", "name": "G", "type": "group", "members": ["a"],
        "last_message": "hi", "unread_count": 0, "muted": True, "messages": [],
        "last_updated": "2026-05-01T10:00:00",
    }])


def test_wechat_chat_muted_and_last_activity_aliases(tmp_path):
    f = _chat_fixture(tmp_path, "chats.json")
    W = _reload("mock_services.claw_wechat.server",
                {"CLAW_WECHAT_CHAT_FIXTURES": f, "CLAW_WECHAT_MOMENTS_FIXTURES": f})
    c = TestClient(W.app)
    chat = c.post("/claw_wechat/chats/list").json()["chats"][0]
    assert chat["is_muted"] is True
    assert chat["last_message_at"] == "2026-05-01T10:00:00"


def test_qq_chat_muted_and_last_activity_aliases(tmp_path):
    f = _chat_fixture(tmp_path, "chat.json")
    Q = _reload("mock_services.claw_qq.server",
                {"CLAW_QQ_CHAT_FIXTURES": f, "CLAW_QQ_ZONE_FIXTURES": f})
    c = TestClient(Q.app)
    chat = c.post("/claw_qq/chats/list").json()["chats"][0]
    assert chat["is_muted"] is True
    assert chat["last_message_at"] == "2026-05-01T10:00:00"


# --- claw_feishu: message_id aliased from id ---------------------------------


def test_feishu_message_search_id_alias(tmp_path):
    f = _write(tmp_path, "workspace.json", [{
        "group_id": "G1", "name": "Grp", "members": ["a"], "owner": "a",
        "archived": False, "last_updated": "2026-01-01", "meetings": [], "docs": [],
        "messages": [{"id": "7", "sender": "a", "text": "roadmap review", "timestamp": "2026-01-01"}],
    }])
    F = _reload("mock_services.claw_feishu.server", {"CLAW_FEISHU_FIXTURES": f})
    c = TestClient(F.app)
    r = c.post("/claw_feishu/messages/search", json={"query": "roadmap"}).json()
    assert r["total"] == 1
    assert r["matches"][0]["message_id"] == "7"  # aliased from id, not null


# --- claw_zhihu: search_questions no longer 500s on contract-shaped data -----


def test_zhihu_search_questions_no_keyerror_on_follow_count_schema(tmp_path):
    f = _write(tmp_path, "questions.json", [{
        "question_id": "Q1", "title": "How to test", "topic": ["testing"],
        "created_at": "2026-01-01", "answer_count": 3, "follow_count": 12, "answers": [],
    }])
    ZH = _reload("mock_services.claw_zhihu.server", {"CLAW_ZHIHU_FIXTURES": f})
    c = TestClient(ZH.app)
    r = c.post("/claw_zhihu/search_questions", json={})
    assert r.status_code == 200  # was 500: q["followed"] KeyError
    q0 = r.json()["questions"][0]
    assert q0["followed"] is False
    assert q0["follow_count"] == 12
    assert c.post("/claw_zhihu/follow_question", json={"question_id": "Q1"}).status_code == 200


# --- scheduler: next_run computed from cron when fixtures omit it ------------


def test_scheduler_next_run_backfilled(tmp_path):
    f = _write(tmp_path, "jobs.json", [
        {"job_id": "JOB-1", "name": "daily", "cron_expression": "30 8 * * *",
         "enabled": True, "last_run": "2026-06-09T08:30:00"},
        {"job_id": "JOB-2", "name": "weekly-mon", "cron_expression": "0 2 * * 1", "enabled": True},
    ])
    SC = _reload("mock_services.scheduler.server", {"SCHEDULER_FIXTURES": f})
    c = TestClient(SC.app)
    jobs = {j["job_id"]: j for j in c.post("/scheduler/jobs").json()["jobs"]}
    # Anchored at 2026-06-10: next daily 08:30 is 06-11; next Monday 02:00 is 06-15.
    assert jobs["JOB-1"]["next_run"] == "2026-06-11T08:30:00"
    assert jobs["JOB-2"]["next_run"] == "2026-06-15T02:00:00"


def test_scheduler_cron_parser_units():
    SC = importlib.import_module("mock_services.scheduler.server")
    assert SC._parse_cron_field("*", 0, 59) == set(range(60))
    assert SC._parse_cron_field("*/15", 0, 59) == {0, 15, 30, 45}
    assert SC._parse_cron_field("1,3,5", 0, 6) == {1, 3, 5}
    assert SC._parse_cron_field("9-11", 0, 23) == {9, 10, 11}
    assert SC._parse_cron_field("garbage", 0, 59) is None
    assert SC._cron_next("not a cron", _dt("2026-06-10T00:00:00")) is None
    assert SC._cron_next("", _dt("2026-06-10T00:00:00")) is None


def _dt(s: str):
    from datetime import datetime
    return datetime.fromisoformat(s)


# --- todo: created_tasks must surface in /audit -----------------------------
# Same bug class as the sweep above. The audit endpoint historically returned
# only {calls, deleted, updated_tasks} so assert_effect(audit, "todo",
# "created_tasks") never landed, even when /tasks/create succeeded. This pins
# the missing bucket so the grader can score real creates.


def test_todo_audit_exposes_created_tasks(tmp_path):
    f = _write(tmp_path, "tasks.json", [
        {"task_id": "todo_001", "title": "seed", "status": "pending",
         "priority": "low", "due_date": None, "tags": []},
    ])
    TD = _reload("mock_services.todo.server", {"TODO_FIXTURES": f})
    c = TestClient(TD.app)
    c.post("/todo/reset")
    r = c.post("/todo/tasks/create", json={"title": "Lock Field Day headcount",
                                            "due_date": "2026-06-20",
                                            "priority": "high"})
    assert r.json()["status"] == "created"
    aud = c.get("/todo/audit").json()
    assert "created_tasks" in aud, "audit must expose created_tasks bucket"
    assert len(aud["created_tasks"]) == 1
    assert aud["created_tasks"][0]["title"] == "Lock Field Day headcount"
    assert aud["created_tasks"][0]["priority"] == "high"


def test_todo_audit_created_tasks_resets(tmp_path):
    f = _write(tmp_path, "tasks.json", [])
    TD = _reload("mock_services.todo.server", {"TODO_FIXTURES": f})
    c = TestClient(TD.app)
    c.post("/todo/tasks/create", json={"title": "x"})
    assert len(c.get("/todo/audit").json()["created_tasks"]) == 1
    c.post("/todo/reset")
    assert c.get("/todo/audit").json()["created_tasks"] == []


# --- inventory: created_orders alias matching ACTION_KEYS ------------------
# ACTION_KEYS["inventory"] = ["created_orders"] but /audit historically returned
# the bucket as "orders", so graders using assert_effect(audit, "inventory",
# "created_orders") could never land. We now expose both keys.


def test_inventory_audit_exposes_created_orders(tmp_path):
    f = _write(tmp_path, "inv.json", [
        {"product_id": "SKU-1", "name": "Widget", "in_stock": 0, "reorder_threshold": 5},
    ])
    INV = _reload("mock_services.inventory.server", {"INVENTORY_FIXTURES": f})
    c = TestClient(INV.app)
    c.post("/inventory/reset")
    r = c.post("/inventory/orders/create", json={
        "product_id": "SKU-1", "quantity": 20, "supplier": "Acme",
    })
    assert r.json()["status"] == "created"
    aud = c.get("/inventory/audit").json()
    assert "created_orders" in aud, "audit must expose canonical created_orders key"
    assert "orders" in aud, "legacy 'orders' alias must remain for back-compat"
    assert aud["created_orders"] == aud["orders"]
    assert len(aud["created_orders"]) == 1
