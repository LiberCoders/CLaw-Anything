"""Tests for OH/oh-ext capturing their final system prompt for trace export.

The build-time ``patch_emit_system_prompt`` patch makes OpenHarness emit a
``{"type": "system_prompt", "text": ...}`` stream-json event at session start.
``OpenHarnessAgent._capture_system_prompt`` records it on ``self._system_prompt``
so ``BaseAgent.run_task``'s ``convert_trace`` prepends a faithful ``system`` row
to the exported ``*.openai.json`` — OH never sets that attribute otherwise.

These pin the capture contract (set on the right event, ignore everything else,
inherited by oh-ext) and that a captured prompt actually surfaces as a system
message through ``convert_trace``.
"""

from __future__ import annotations

from pathlib import Path

from claw_anything.agents.openharness_agent import OpenHarnessAgent
from claw_anything.agents.openharness_ext_agent import OpenHarnessExtAgent
from claw_anything.export.openai_format import convert_trace
from claw_anything.models.content import TextBlock
from claw_anything.models.message import Message
from claw_anything.models.task import Prompt, TaskDefinition
from claw_anything.models.trace import TraceEnd, TraceMessage, TraceStart
from claw_anything.trace.writer import TraceWriter

SYSTEM_TEXT = "You are OpenHarness, an open-source general AI assistant CLI."


def test_capture_sets_system_prompt() -> None:
    agent = OpenHarnessAgent()
    agent._capture_system_prompt({"type": "system_prompt", "text": SYSTEM_TEXT})
    # base.run_task reads exactly this expression to feed convert_trace.
    assert getattr(agent, "_system_prompt", None) == SYSTEM_TEXT


def test_capture_ignores_other_event_types() -> None:
    agent = OpenHarnessAgent()
    agent._capture_system_prompt({"type": "assistant_delta", "text": "hello"})
    agent._capture_system_prompt({"type": "tool_started", "tool_name": "x"})
    assert getattr(agent, "_system_prompt", None) is None


def test_capture_ignores_empty_or_missing_text() -> None:
    agent = OpenHarnessAgent()
    agent._capture_system_prompt({"type": "system_prompt", "text": ""})
    agent._capture_system_prompt({"type": "system_prompt"})
    assert getattr(agent, "_system_prompt", None) is None


def test_capture_inherited_by_oh_ext() -> None:
    # oh-ext shares the base _execute / read loop, so the capture must apply
    # to it too — both images get the emit patch.
    agent = OpenHarnessExtAgent()
    agent._capture_system_prompt({"type": "system_prompt", "text": SYSTEM_TEXT})
    assert getattr(agent, "_system_prompt", None) == SYSTEM_TEXT


def _write_min_trace(path: Path, trace_id: str = "tid01234") -> None:
    with TraceWriter(path) as w:
        w.write_event(TraceStart(trace_id=trace_id, task_id="t", model="m"))
        w.write_event(
            TraceMessage(
                trace_id=trace_id,
                message=Message(role="user", content=[TextBlock(text="hi")]),
            )
        )
        w.write_event(TraceEnd(trace_id=trace_id))


def _min_task() -> TaskDefinition:
    return TaskDefinition(task_id="t", task_name="t", prompt=Prompt(text="hi"))


def test_captured_prompt_becomes_system_row(tmp_path: Path) -> None:
    agent = OpenHarnessAgent()
    agent._capture_system_prompt({"type": "system_prompt", "text": SYSTEM_TEXT})
    trace = tmp_path / "t_tid01234.jsonl"
    _write_min_trace(trace)
    data = convert_trace(
        trace, _min_task(), system_prompt=getattr(agent, "_system_prompt", None)
    )
    assert data is not None
    assert data["messages"][0] == {"role": "system", "content": SYSTEM_TEXT}


def test_no_system_row_when_nothing_captured(tmp_path: Path) -> None:
    agent = OpenHarnessAgent()  # no system_prompt event ever seen
    trace = tmp_path / "t_tid01234.jsonl"
    _write_min_trace(trace)
    data = convert_trace(
        trace, _min_task(), system_prompt=getattr(agent, "_system_prompt", None)
    )
    assert data is not None
    assert data["messages"][0]["role"] == "user"
