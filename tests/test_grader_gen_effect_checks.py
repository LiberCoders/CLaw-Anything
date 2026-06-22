"""Regression tests for the generation-pipeline guards that force ACTION tasks
to grade world-state (expected_effects) instead of narration."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml

from claw_anything.gen.grader_generator import GraderGenerator
from claw_anything.gen.validator import TaskValidator, Severity

_NARRATION_GRADER = """
from claw_anything.graders.base import AbstractGrader
class G(AbstractGrader):
    FORBIDDEN_TOOLS = set()
    def grade(self, messages, dispatches, task, audit_data=None, judge=None):
        return None
"""

_STATE_GRADER = """
from claw_anything.graders.base import AbstractGrader
class G(AbstractGrader):
    FORBIDDEN_TOOLS = set()
    def grade(self, messages, dispatches, task, audit_data=None, judge=None):
        self.assert_effect(audit_data, "gmail", "drafts")
        return None
"""


# ---------------------------------------------- grader_generator._validate_code
def test_action_grader_without_state_check_rejected():
    import pytest
    with __import__("pytest").raises(ValueError):
        GraderGenerator._validate_code(_NARRATION_GRADER, allowed_tools=set(), require_effect_check=True)


def test_action_grader_with_state_check_accepted():
    GraderGenerator._validate_code(_STATE_GRADER, allowed_tools=set(), require_effect_check=True)


def test_advisory_grader_allows_narration():
    GraderGenerator._validate_code(_NARRATION_GRADER, allowed_tools=set(), require_effect_check=False)


# ---------------------------------------------- validator._check_effect_grounding
def _make_task(tmp: str, effects, grader_src: str):
    yaml.safe_dump({
        "task_id": "T", "task_name": "n", "services": [{"name": "gmail"}],
        "tools": [], "tool_endpoints": [], "prompt": {"text": "p"},
        "scoring_components": [], "safety_checks": [], "expected_effects": effects,
    }, open(os.path.join(tmp, "task.yaml"), "w"))
    open(os.path.join(tmp, "grader.py"), "w").write(grader_src)
    return Path(tmp)


def test_effect_grounding_rejects_unknown_audit_key():
    with tempfile.TemporaryDirectory() as tmp:
        d = _make_task(tmp, [{"service": "gmail", "action_key": "NOTAKEY"}], _STATE_GRADER)
        r = TaskValidator()._check_effect_grounding(d)
        assert not r.passed and r.severity == Severity.BLOCKING


def test_effect_grounding_passes_valid_key_and_state_grader():
    with tempfile.TemporaryDirectory() as tmp:
        d = _make_task(tmp, [{"service": "gmail", "action_key": "drafts"}], _STATE_GRADER)
        r = TaskValidator()._check_effect_grounding(d)
        assert r.passed


def test_effect_grounding_rejects_action_task_with_narration_grader():
    with tempfile.TemporaryDirectory() as tmp:
        d = _make_task(tmp, [{"service": "gmail", "action_key": "drafts"}], _NARRATION_GRADER)
        r = TaskValidator()._check_effect_grounding(d)
        assert not r.passed


def test_effect_grounding_advisory_task_is_na():
    with tempfile.TemporaryDirectory() as tmp:
        d = _make_task(tmp, [], _NARRATION_GRADER)
        r = TaskValidator()._check_effect_grounding(d)
        assert r.passed  # no expected_effects → N/A (advisory)
