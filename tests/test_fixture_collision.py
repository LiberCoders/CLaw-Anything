"""Validator's fixture-collision check: reject tasks whose expected_effects
gold state already exists in seed fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from claw_anything.gen.validator import TaskValidator, Severity


def _make_task(tmp_path: Path, expected_effects: list[dict],
               fixtures: dict[str, dict[str, list[dict]]]) -> Path:
    """Lay out a minimal task directory with the supplied expected_effects and
    per-service fixture files."""
    td = tmp_path / "TASK_X"
    td.mkdir()
    (td / "task.yaml").write_text(yaml.safe_dump({
        "task_id": "TASK_X",
        "task_name": "x",
        "services": [],
        "tools": [],
        "tool_endpoints": {},
        "prompt": {"text": "x"},
        "scoring_components": [],
        "safety_checks": [],
        "expected_effects": expected_effects,
    }))
    for svc, files in fixtures.items():
        d = td / "fixtures" / svc
        d.mkdir(parents=True)
        for fname, records in files.items():
            (d / fname).write_text(json.dumps(records))
    return td


def test_calendar_collision_is_blocking(tmp_path):
    td = _make_task(
        tmp_path,
        expected_effects=[{
            "service": "calendar",
            "action_key": "created_events",
            "match": {"title": "PWF Sign-Off Block", "start_time": "2026-06-18T15:00"},
            "required": True,
        }],
        fixtures={"calendar": {"events.json": [
            {"event_id": "EVT-1", "title": "PWF Sign-Off Block",
             "start_time": "2026-06-18T15:00:00-07:00",
             "end_time": "2026-06-18T17:00:00-07:00"},
        ]}},
    )
    r = TaskValidator()._check_fixture_collision(td)
    assert r.passed is False
    assert r.severity == Severity.BLOCKING
    assert "EVT-1" in r.message


def test_todo_collision_is_warning(tmp_path):
    td = _make_task(
        tmp_path,
        expected_effects=[{
            "service": "todo",
            "action_key": "created_tasks",
            "match": {"title": "Lock Field Day headcount", "priority": "high"},
            "required": True,
        }],
        fixtures={"todo": {"tasks.json": [
            {"task_id": "TODO-1", "title": "Lock Field Day headcount", "priority": "high",
             "due_date": "2026-06-20"},
        ]}},
    )
    r = TaskValidator()._check_fixture_collision(td)
    assert r.passed is False
    assert r.severity == Severity.WARNING


def test_no_collision_passes(tmp_path):
    td = _make_task(
        tmp_path,
        expected_effects=[{
            "service": "calendar",
            "action_key": "created_events",
            "match": {"title": "Brand-new sign-off"},
            "required": True,
        }],
        fixtures={"calendar": {"events.json": [
            {"event_id": "EVT-1", "title": "Unrelated event"},
        ]}},
    )
    r = TaskValidator()._check_fixture_collision(td)
    assert r.passed is True
    assert r.severity == Severity.BLOCKING


def test_no_expected_effects_is_warning_pass(tmp_path):
    td = _make_task(tmp_path, expected_effects=[], fixtures={})
    r = TaskValidator()._check_fixture_collision(td)
    assert r.passed is True
    assert r.severity == Severity.WARNING


def test_unregistered_service_skipped(tmp_path):
    # `helpdesk.updated_tickets` is intentionally NOT in _COLLISION_FIXTURES
    # because an update on a same-status ticket is still a valid landed write.
    td = _make_task(
        tmp_path,
        expected_effects=[{
            "service": "helpdesk", "action_key": "updated_tickets",
            "match": {"ticket_id": "TK-1"},
            "required": True,
        }],
        fixtures={"helpdesk": {"tickets.json": [
            {"ticket_id": "TK-1", "title": "existing"},
        ]}},
    )
    r = TaskValidator()._check_fixture_collision(td)
    assert r.passed is True
