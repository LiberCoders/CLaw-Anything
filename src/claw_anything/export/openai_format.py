"""Convert claw-anything JSONL traces to OpenAI fine-tuning format with tool_calls."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..models.content import (
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from ..models.task import TaskDefinition
from ..models.trace import GradingResult, TraceMessage
from ..runner.providers.openai_compat import _tool_spec_to_openai
from ..trace.reader import load_trace, read_events


def _strip_thinking(text: str) -> str:
    """Remove <think>...</think> blocks from text."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _convert_message(trace_msg: TraceMessage, *, strip_think: bool = False) -> list[dict[str, Any]]:
    """Convert a single TraceMessage to one or more OpenAI format messages.

    A user message with ToolResultBlocks becomes multiple {"role": "tool"} messages.
    """
    msg = trace_msg.message
    role = msg.role

    if role == "assistant":
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []

        for block in msg.content:
            if isinstance(block, TextBlock):
                t = block.text
                if strip_think:
                    t = _strip_thinking(t)
                if t:
                    text_parts.append(t)
            elif isinstance(block, ToolUseBlock):
                tool_calls.append({
                    "id": block.id,
                    "type": "function",
                    "function": {
                        "name": block.name,
                        "arguments": json.dumps(block.input, ensure_ascii=False),
                    },
                })
            # Skip media blocks in assistant messages

        content = "\n".join(text_parts) if text_parts else None

        if tool_calls:
            return [{"role": "assistant", "content": content, "tool_calls": tool_calls}]
        else:
            return [{"role": "assistant", "content": content or ""}]

    elif role == "user":
        # Check if this is a tool-result message
        has_tool_results = any(isinstance(b, ToolResultBlock) for b in msg.content)

        if has_tool_results:
            results: list[dict[str, Any]] = []
            text_parts_user: list[str] = []

            for block in msg.content:
                if isinstance(block, ToolResultBlock):
                    # Join text content from the tool result
                    result_text = "\n".join(
                        tb.text for tb in block.content if isinstance(tb, TextBlock)
                    )
                    results.append({
                        "role": "tool",
                        "tool_call_id": block.tool_use_id,
                        "content": result_text,
                    })
                elif isinstance(block, TextBlock):
                    text_parts_user.append(block.text)

            # If there's also text alongside tool results, add as user message after
            if text_parts_user:
                results.append({"role": "user", "content": "\n".join(text_parts_user)})
            return results

        else:
            # Pure user text message
            text_parts_plain: list[str] = [
                block.text for block in msg.content if isinstance(block, TextBlock)
            ]
            return [{"role": "user", "content": "\n".join(text_parts_plain)}]

    elif role == "system":
        text = "\n".join(
            b.text for b in msg.content if isinstance(b, TextBlock)
        )
        return [{"role": "system", "content": text}]

    return []


def _get_grading_result(trace_path: str | Path) -> GradingResult | None:
    """Scan trace for GradingResult event."""
    for event in read_events(trace_path):
        if isinstance(event, GradingResult):
            return event
    return None


def convert_trace(
    trace_path: str | Path,
    task_def: TaskDefinition,
    *,
    system_prompt: str | None = None,
    strip_think: bool = False,
    passed_only: bool = False,
    min_score: float | None = None,
) -> dict[str, Any] | None:
    """Convert a single trace file to OpenAI fine-tuning format.

    Returns None if the trace is filtered out (by score or pass status).
    """
    # Check filter criteria
    if passed_only or min_score is not None:
        grading = _get_grading_result(trace_path)
        if grading is not None:
            if passed_only and not grading.passed:
                return None
            if min_score is not None and grading.task_score < min_score:
                return None

    start, messages, dispatches, end, audit_data = load_trace(trace_path)

    if not messages:
        return None

    # Convert messages
    openai_messages: list[dict[str, Any]] = []

    # Prepend system prompt if provided
    if system_prompt:
        openai_messages.append({"role": "system", "content": system_prompt})

    for trace_msg in messages:
        converted = _convert_message(trace_msg, strip_think=strip_think)
        openai_messages.extend(converted)

    # Build tools list
    tools = [_tool_spec_to_openai(spec) for spec in task_def.tools] if task_def.tools else []

    result: dict[str, Any] = {"messages": openai_messages}
    if tools:
        result["tools"] = tools

    return result


def convert_directory(
    trace_dir: str | Path,
    tasks_dir: str | Path,
    output_path: str | Path,
    *,
    system_prompt: str | None = None,
    strip_think: bool = False,
    passed_only: bool = False,
    min_score: float | None = None,
    config_path: str | Path | None = None,
) -> int:
    """Convert all traces in a directory to OpenAI format JSONL.

    Returns the number of successfully converted traces.
    """
    trace_dir = Path(trace_dir)
    tasks_dir = Path(tasks_dir)
    output_path = Path(output_path)

    # Cache loaded task definitions
    task_cache: dict[str, TaskDefinition] = {}
    converted_count = 0
    skipped_count = 0
    error_count = 0

    trace_files = sorted(trace_dir.glob("*.jsonl"))
    if not trace_files:
        print(f"No .jsonl files found in {trace_dir}")
        return 0

    print(f"Found {len(trace_files)} trace files in {trace_dir}")

    with open(output_path, "w", encoding="utf-8") as out:
        for trace_file in trace_files:
            try:
                # Extract task_id from trace
                start, _, _, _, _ = load_trace(trace_file)
                task_id = start.task_id

                # Load task definition (with cache)
                if task_id not in task_cache:
                    task_yaml = tasks_dir / task_id / "task.yaml"
                    if not task_yaml.exists():
                        print(f"  WARN: task.yaml not found for {task_id} at {task_yaml}, skipping")
                        error_count += 1
                        continue
                    task_cache[task_id] = TaskDefinition.from_yaml(task_yaml)

                task_def = task_cache[task_id]

                # Build system prompt if auto mode
                effective_prompt = system_prompt
                if effective_prompt == "__auto__":
                    effective_prompt = _build_auto_system_prompt(task_def, config_path)

                result = convert_trace(
                    trace_file,
                    task_def,
                    system_prompt=effective_prompt,
                    strip_think=strip_think,
                    passed_only=passed_only,
                    min_score=min_score,
                )

                if result is None:
                    skipped_count += 1
                    continue

                out.write(json.dumps(result, ensure_ascii=False) + "\n")
                converted_count += 1

            except Exception as e:
                print(f"  ERROR processing {trace_file.name}: {e}")
                error_count += 1

    print(f"\nDone: {converted_count} converted, {skipped_count} filtered, {error_count} errors")
    print(f"Output: {output_path}")
    return converted_count


def _build_auto_system_prompt(
    task_def: TaskDefinition,
    config_path: str | Path | None,
) -> str:
    """Build system prompt from task definition and config."""
    from ..config import load_config
    from ..runner.system_prompt import build_system_prompt

    cfg = load_config(config_path)
    return build_system_prompt(task_def, cfg.prompt)
