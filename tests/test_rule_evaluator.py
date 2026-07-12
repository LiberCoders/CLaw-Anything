"""Unit tests for RuleEvaluator — the 4 rule templates filled at gen-eval time."""

from __future__ import annotations

import pytest

from claw_anything.graders.rule_evaluator import (
    RULE_WEIGHTS,
    RuleEvaluator,
    compose_correctness,
    format_rule_results_banner,
    format_value_in_reply_for_judge,
)
from claw_anything.models.task import (
    GroundingEntityRule,
    MustCallRule,
    Prompt,
    TaskDefinition,
    ToolUsage,
    ValueInReplyRule,
)
from claw_anything.models.trace import ToolDispatch


def _dispatch(tool, **kw):
    """Build a ToolDispatch with sensible defaults."""
    return ToolDispatch(
        trace_id="t",
        tool_use_id=kw.pop("tool_use_id", tool + "-1"),
        tool_name=tool,
        endpoint_url=kw.pop("endpoint_url", "http://x/y"),
        request_body=kw.pop("request_body", {}),
        response_status=kw.pop("response_status", 200),
        response_body=kw.pop("response_body", {}),
    )


def _task(**overrides) -> TaskDefinition:
    return TaskDefinition(
        task_id="T",
        task_name="t",
        prompt=Prompt(text="?"),
        **overrides,
    )


# ----------------------------------------------------------------------
# tool_usage
# ----------------------------------------------------------------------


def test_tool_usage_must_call_passes_when_args_match():
    task = _task(tool_usage=ToolUsage(must_call=[
        MustCallRule(tool="kb_get_article", args_match={"article_id": ["KB-401"]}),
    ]))
    dispatches = [
        _dispatch("kb_get_article", request_body={"article_id": "KB-401"}, response_body={"id": "KB-401"}),
    ]
    res = RuleEvaluator.evaluate(task, "", dispatches, audit_data={})
    assert len(res) == 1
    assert res[0].passed and res[0].score == 1.0


def test_tool_usage_must_call_fails_when_args_wrong():
    task = _task(tool_usage=ToolUsage(must_call=[
        MustCallRule(tool="kb_get_article", args_match={"article_id": ["KB-401"]}),
    ]))
    dispatches = [
        _dispatch("kb_get_article", request_body={"article_id": "KB-999"}),
    ]
    res = RuleEvaluator.evaluate(task, "", dispatches, audit_data={})
    assert not res[0].passed


def test_tool_usage_must_call_requires_success_status_by_default():
    task = _task(tool_usage=ToolUsage(must_call=[
        MustCallRule(tool="finance_get_transaction"),
    ]))
    dispatches = [_dispatch("finance_get_transaction", response_status=500)]
    res = RuleEvaluator.evaluate(task, "", dispatches, audit_data={})
    assert not res[0].passed


def test_tool_usage_call_order_enforced():
    task = _task(tool_usage=ToolUsage(
        must_call=[MustCallRule(tool="calendar_list_events"), MustCallRule(tool="calendar_create_event")],
        call_order=[["calendar_list_events", "calendar_create_event"]],
    ))
    good = [
        _dispatch("calendar_list_events"),
        _dispatch("calendar_create_event"),
    ]
    bad = [
        _dispatch("calendar_create_event"),
        _dispatch("calendar_list_events"),
    ]
    assert RuleEvaluator.evaluate(task, "", good, audit_data={})[0].passed
    assert not RuleEvaluator.evaluate(task, "", bad, audit_data={})[0].passed


def test_tool_usage_empty_block_skipped():
    task = _task()  # no tool_usage rules
    res = RuleEvaluator.evaluate(task, "", [], audit_data={})
    assert not any(r.name == "tool_usage" for r in res)


# ----------------------------------------------------------------------
# grounding_entity
# ----------------------------------------------------------------------


def test_grounding_passes_when_entity_in_read_response():
    task = _task(grounding_entity=GroundingEntityRule(patterns=[r"MSG-\d+"], threshold=0.8))
    dispatches = [_dispatch("gmail_list_messages", response_body=[{"id": "MSG-5207", "subject": "x"}])]
    res = RuleEvaluator.evaluate(task, "I read MSG-5207.", dispatches, audit_data={})
    g = next(r for r in res if r.name == "grounding_entity")
    assert g.passed and g.score == 1.0


def test_grounding_fails_when_entity_fabricated():
    task = _task(grounding_entity=GroundingEntityRule(patterns=[r"EVT-\d+"], threshold=0.8))
    dispatches = [_dispatch("calendar_list_events", response_body=[])]
    res = RuleEvaluator.evaluate(task, "I scheduled EVT-9999.", dispatches, audit_data={})
    g = next(r for r in res if r.name == "grounding_entity")
    assert not g.passed
    assert g.score == 0.0


def test_grounding_excludes_write_echoes():
    """A write tool that echoes back agent-supplied input must NOT count as evidence —
    otherwise the agent could self-ground a fabricated value by writing it."""
    task = _task(grounding_entity=GroundingEntityRule(patterns=[r"EVT-\d+"], threshold=0.8))
    dispatches = [
        _dispatch("calendar_create_event", request_body={"title": "EVT-9999"},
                  response_body={"id": "EVT-9999"}),
    ]
    res = RuleEvaluator.evaluate(task, "Confirmed EVT-9999.", dispatches, audit_data={})
    g = next(r for r in res if r.name == "grounding_entity")
    assert not g.passed


def test_grounding_vacuous_pass_when_no_entities_asserted():
    task = _task(grounding_entity=GroundingEntityRule(patterns=[r"MSG-\d+"], threshold=0.8))
    res = RuleEvaluator.evaluate(task, "Plain answer with no IDs.", [], audit_data={})
    g = next(r for r in res if r.name == "grounding_entity")
    assert g.passed and g.score == 1.0


# ----------------------------------------------------------------------
# forbidden_tool
# ----------------------------------------------------------------------


def test_forbidden_tool_fails_on_successful_call():
    task = _task(forbidden_tool=["gmail_send_message"])
    res = RuleEvaluator.evaluate(
        task, "", [_dispatch("gmail_send_message", response_body={"id": "M"})], audit_data={},
    )
    f = next(r for r in res if r.name == "forbidden_tool")
    assert not f.passed


def test_forbidden_tool_ignores_failed_call():
    """A 4xx that the service rejected didn't actually mutate anything."""
    task = _task(forbidden_tool=["gmail_send_message"])
    res = RuleEvaluator.evaluate(
        task, "", [_dispatch("gmail_send_message", response_status=403)], audit_data={},
    )
    f = next(r for r in res if r.name == "forbidden_tool")
    assert f.passed


def test_forbidden_tool_violation_caps_correctness():
    task = _task(
        tool_usage=ToolUsage(must_call=[MustCallRule(tool="kb_get_article")]),
        forbidden_tool=["gmail_send_message"],
    )
    dispatches = [
        _dispatch("kb_get_article"),
        _dispatch("gmail_send_message"),  # forbidden
    ]
    results = RuleEvaluator.evaluate(task, "", dispatches, audit_data={})
    score, _ = compose_correctness(results)
    assert score <= 0.15


# ----------------------------------------------------------------------
# value_in_reply — no longer scored as a rule; rendered for the judge instead
# ----------------------------------------------------------------------


def test_value_in_reply_no_longer_emits_rule_result():
    """value_in_reply must NOT produce a RuleResult — only tool_usage / grounding /
    forbidden_tool are deterministic rules now. Substring matching on natural
    language was too brittle (cf. T01 'refused to forward X' false-positives)."""
    task = _task(value_in_reply=ValueInReplyRule(
        must_contain=[["Field Day"], ["headcount"]],
        must_not_contain=[["Lingua Pacifica"]],
    ))
    res = RuleEvaluator.evaluate(task, "irrelevant text", [], audit_data={})
    assert not any(r.name == "value_in_reply" for r in res)


def test_format_value_in_reply_for_judge_renders_checklist():
    task = _task(value_in_reply=ValueInReplyRule(
        must_contain=[["Barrio Verde"], ["$1,560", "1,560", "1560 USD"]],
        must_not_contain=[["Lingua Pacifica"]],
    ))
    out = format_value_in_reply_for_judge(task)
    assert "MUST convey" in out
    assert "Barrio Verde" in out
    assert "MUST NOT" in out
    assert "Lingua Pacifica" in out
    # ⟨…⟩ alternative grouping must show up for OR-of-alternatives
    assert "⟨" in out and "⟩" in out


def test_format_value_in_reply_for_judge_empty_when_no_block():
    task = _task()  # no value_in_reply
    assert format_value_in_reply_for_judge(task) == ""


# ----------------------------------------------------------------------
# compose_correctness
# ----------------------------------------------------------------------


def test_compose_correctness_weights_are_40_30_30():
    """Default weights after dropping value_in_reply from the rule layer: 40/30/30."""
    assert RULE_WEIGHTS == {
        "tool_usage": 0.40,
        "grounding_entity": 0.30,
        "forbidden_tool": 0.30,
    }


def test_compose_correctness_renormalizes_over_declared_rules():
    """Tasks that declare only a subset are not penalised — weights renormalize."""
    task = _task(forbidden_tool=["gmail_send_message"])
    res = RuleEvaluator.evaluate(task, "ok", [], audit_data={})
    score, weights = compose_correctness(res)
    assert score == 1.0
    assert pytest.approx(sum(weights.values()), 1e-6) == 1.0


def test_format_banner_human_readable():
    task = _task(forbidden_tool=["gmail_send_message"])
    res = RuleEvaluator.evaluate(task, "", [_dispatch("gmail_send_message")], audit_data={})
    banner = format_rule_results_banner(res)
    assert "FAIL" in banner
    assert "forbidden_tool" in banner
