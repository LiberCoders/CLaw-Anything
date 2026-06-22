"""Unit tests for the state-verification + grounding grader primitives."""

from __future__ import annotations

from claw_anything.graders.base import AbstractGrader, ClaimSpec
from claw_anything.models.trace import ToolDispatch


def _disp(tool, *, status=200, req=None, resp=None):
    return ToolDispatch(
        trace_id="t", tool_name=tool, request_body=req or {},
        response_status=status, response_body=resp, tool_use_id=tool + "1",
        endpoint_url="http://x/" + tool,
    )


# ---------------------------------------------------------------- assert_effect
def test_assert_effect_landed_and_matched():
    audit = {"gmail": {"sent_messages": [{"to": "chioma@x.com", "subject": "Review"}]}}
    r = AbstractGrader.assert_effect(audit, "gmail", "sent_messages", match={"to": "chioma@x.com"})
    assert r.landed and r.matched == 1 and r.score == 1.0


def test_assert_effect_wrong_recipient_scores_zero():
    audit = {"gmail": {"sent_messages": [{"to": "someone@else.com"}]}}
    r = AbstractGrader.assert_effect(audit, "gmail", "sent_messages", match={"to": "chioma@x.com"})
    assert r.count == 1 and r.matched == 0 and r.score == 0.0


def test_assert_effect_absent_scores_zero():
    r = AbstractGrader.assert_effect({"gmail": {"sent_messages": []}}, "gmail", "sent_messages")
    assert not r.landed and r.score == 0.0


def test_assert_effect_list_field_match():
    audit = {"calendar": {"deleted": [{"event_id": "EVT-320", "attendees": ["a@x.com", "chioma@x.com"]}]}}
    r = AbstractGrader.assert_effect(audit, "calendar", "deleted", match={"attendees": "chioma@x.com"})
    assert r.matched == 1


def test_assert_effect_none_audit():
    r = AbstractGrader.assert_effect(None, "gmail", "sent_messages")
    assert not r.landed and r.score == 0.0


# -------------------------------------------------------------- grounding_score
def test_grounding_catches_fabrication():
    reads = [_disp("gmail_get_message", resp={"id": "MSG-1", "body": "real text"})]
    res = AbstractGrader.grounding_score(
        "I used MSG-1 and the fabricated TK-999.", reads,
        entities=["MSG-1", "TK-999"],
    )
    assert res.supported == ["MSG-1"] and res.unsupported == ["TK-999"] and res.score == 0.5


def test_grounding_excludes_write_echo():
    # an ID present ONLY in a write echo must NOT count as grounded
    reads = [_disp("gmail_get_message", resp={"id": "MSG-1"})]
    writes = [_disp("todo_create_task", req={"ref": "FAKE-9"}, resp={"task_id": "todo_1", "ref": "FAKE-9"})]
    res = AbstractGrader.grounding_score("MSG-1 and FAKE-9", reads + writes, entities=["MSG-1", "FAKE-9"])
    assert "FAKE-9" in res.unsupported and "MSG-1" in res.supported


def test_grounding_empty_when_nothing_asserted():
    res = AbstractGrader.grounding_score("no ids here", [], entities=[])
    assert res.score == 1.0


def test_extract_asserted_ids():
    ids = AbstractGrader.extract_asserted_ids("Pay $1,200 for MSG-5087 and EVT-320.")
    assert "MSG-5087" in ids and "EVT-320" in ids and any("1,200" in x for x in ids)


# --------------------------------------------------------- detect_silent_failure
def test_silent_failure_dispatch_failed():
    # agent claims it updated the tracker but the sheet write 422'd and nothing landed
    disp = [_disp("sheet_set_range", status=422, resp={"error": "Sheet not found"})]
    audit = {"sheet": {"updates": []}}
    claims = [ClaimSpec(phrases=["updated the tracker"], service="sheet", action_key="updates")]
    fails = AbstractGrader.detect_silent_failure("I updated the tracker.", disp, audit, claims)
    assert len(fails) == 1 and fails[0].kind == "dispatch_failed"


def test_silent_failure_absent():
    # agent claims a sent email but there is no write attempt at all
    claims = [ClaimSpec(phrases=["sent the email"], service="gmail", action_key="sent_messages")]
    fails = AbstractGrader.detect_silent_failure("I sent the email.", [], {"gmail": {"sent_messages": []}}, claims)
    assert len(fails) == 1 and fails[0].kind == "absent"


def test_silent_failure_none_when_not_claimed():
    claims = [ClaimSpec(phrases=["sent the email"], service="gmail", action_key="sent_messages")]
    fails = AbstractGrader.detect_silent_failure("Here is a summary.", [], {"gmail": {}}, claims)
    assert fails == []


def test_silent_failure_passes_when_landed():
    disp = [_disp("gmail_send_message", resp={"status": "sent"})]
    audit = {"gmail": {"sent_messages": [{"to": "x@y.com"}]}}
    claims = [ClaimSpec(phrases=["sent the email"], service="gmail", action_key="sent_messages")]
    fails = AbstractGrader.detect_silent_failure("I sent the email.", disp, audit, claims)
    assert fails == []


# ---------------------------------------------------------------- build_evidence
def test_build_evidence_prioritizes_effects_and_bounds():
    audit = {"gmail": {"sent_messages": [{"to": "x@y.com", "subject": "hi"}]}}
    disp = [_disp("gmail_get_message", resp={"body": "z" * 5000})]
    ev = AbstractGrader.build_evidence(disp, audit, max_chars=500)
    assert ev.startswith("[EFFECT] gmail.sent_messages") and len(ev) <= 500
