"""Tests for the gen schema_enforcer backfill helper.

Covers:
  * Type-derived defaults match every type string used in
    ``template/fixture_schemas.yaml`` and ``fixture_schemas_gui.yaml``.
  * ``enforce_record`` backfills missing top-level fields without overwriting
    existing values (including explicit ``None``).
  * ``enforce_records`` logs per-record warnings and an error when ≥50% of
    records in a batch miss the same field.
  * The original failing case — a ``notes`` fixture without
    ``duration_minutes`` — is repaired to ``0``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from claw_anything.gen.schema_enforcer import (
    _UNKNOWN_TYPE,
    _default_for_type,
    enforce_record,
    enforce_records,
    field_defaults,
)

_REPO = Path(__file__).resolve().parents[1]


def _load_schemas() -> dict:
    schemas: dict = yaml.safe_load((_REPO / "template" / "fixture_schemas.yaml").read_text())
    gui = yaml.safe_load((_REPO / "template" / "fixture_schemas_gui.yaml").read_text())
    if gui:
        schemas.update(gui)
    return schemas


def test_default_for_type_primitives():
    assert _default_for_type("string") == ""
    assert _default_for_type("integer") == 0
    assert _default_for_type("number") == 0.0
    assert _default_for_type("boolean") is False
    assert _default_for_type("object") == {}
    assert _default_for_type("list[string]") == []
    assert _default_for_type("list[object]") == []


def test_default_for_type_unions_and_nullable():
    # "null" anywhere → None (always safe for a missing value)
    assert _default_for_type("string|null") is None
    assert _default_for_type("null") is None
    # Union without null → first matching primitive in priority order
    assert _default_for_type("boolean|string|integer") == ""
    assert _default_for_type("string|object") == {}
    assert _default_for_type("list[string|object]") == []


def test_default_for_type_unknown():
    # Unknown / empty type returns the sentinel so callers can fall back to example.
    assert _default_for_type("") is _UNKNOWN_TYPE
    assert _default_for_type("totally_unknown_type") is _UNKNOWN_TYPE


def test_field_defaults_notes_schema():
    schemas = _load_schemas()
    d = field_defaults(schemas["notes"])
    assert d["note_id"] == ""
    assert d["title"] == ""
    assert d["created_at"] == ""
    assert d["participants"] == []
    assert d["content"] == ""
    assert d["duration_minutes"] == 0


def test_field_defaults_claw_wechat_nullable():
    schemas = _load_schemas()
    d = field_defaults(schemas["claw_wechat"])
    # last_message is "string|null" — must default to None, not ""
    assert d["last_message"] is None
    assert d["unread_count"] == 0
    assert d["muted"] is False
    assert d["messages"] == []
    assert d["members"] == []


def test_enforce_record_backfills_missing_notes_field():
    """The original conduct2.log failure: notes fixture without duration_minutes."""
    schemas = _load_schemas()
    bad = {
        "note_id": "NOTE-101",
        "title": "Reminder",
        "created_at": "2026-04-01T09:00:00+08:00",
        "participants": ["alice"],
        "content": "Buy formula",
        # duration_minutes missing — what blew up notes/server.py
    }
    filled, missing = enforce_record("notes", bad, schemas)
    assert missing == ["duration_minutes"]
    assert filled["duration_minutes"] == 0
    # Other fields untouched
    assert filled["note_id"] == "NOTE-101"
    assert filled["title"] == "Reminder"


def test_enforce_record_preserves_existing_none():
    """LLM-emitted explicit None must not be overwritten with the default."""
    schemas = _load_schemas()
    rec = {
        "chat_id": "WCC-0001",
        "name": "Family",
        "type": "group",
        "members": ["a", "b"],
        "last_message": None,  # explicitly null
        "unread_count": 0,
        "muted": False,
        "messages": [],
        "last_updated": "2026-04-01T09:00:00+08:00",
    }
    filled, missing = enforce_record("claw_wechat", rec, schemas)
    assert missing == []
    assert filled["last_message"] is None


def test_enforce_record_unknown_service_passthrough():
    filled, missing = enforce_record("not_a_real_service", {"x": 1}, {})
    assert filled == {"x": 1}
    assert missing == []


def test_enforce_records_logs_warning_per_record(caplog):
    schemas = _load_schemas()
    bad = [
        {"note_id": "NOTE-101", "title": "x", "created_at": "t", "participants": [], "content": "c"},
    ]
    with caplog.at_level(logging.WARNING, logger="claw_anything.gen.schema_enforcer"):
        out = enforce_records("notes", bad, schemas, source="unit_test")
    assert out[0]["duration_minutes"] == 0
    msgs = [r.getMessage() for r in caplog.records]
    assert any("NOTE-101" in m and "duration_minutes" in m for m in msgs)


def test_enforce_records_escalates_when_half_or_more_miss_same_field(caplog):
    schemas = _load_schemas()
    # 3/3 records miss duration_minutes → ≥50% → error-level summary
    bad = [
        {"note_id": f"NOTE-{i}", "title": "x", "created_at": "t", "participants": [], "content": "c"}
        for i in range(3)
    ]
    with caplog.at_level(logging.ERROR, logger="claw_anything.gen.schema_enforcer"):
        enforce_records("notes", bad, schemas, source="unit_test")
    err_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("systematically missed" in m and "duration_minutes" in m for m in err_msgs)


def test_enforce_records_empty_input_is_noop():
    schemas = _load_schemas()
    assert enforce_records("notes", [], schemas, source="x") == []
