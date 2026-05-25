"""Skill mode: progressive tool revelation for context savings."""

from __future__ import annotations

import json
import time
from typing import Any

from ..models.content import TextBlock, ToolResultBlock, ToolUseBlock
from ..models.task import TaskDefinition
from ..models.tool import ToolSpec
from ..models.trace import ToolDispatch


GET_TOOL_SCHEMA_SPEC = ToolSpec(
    name="get_tool_schema",
    description=(
        "Retrieve full definitions (description + JSON Schema) for tools by name. "
        "Call this before using any tool."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "tool_names": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of tool names to retrieve schemas for.",
            }
        },
        "required": ["tool_names"],
    },
)


def handle_get_tool_schema(
    tool_use: ToolUseBlock,
    trace_id: str,
    all_tools_by_name: dict[str, ToolSpec],
) -> tuple[ToolResultBlock, ToolDispatch, list[str]]:
    """Handle a get_tool_schema call locally.

    Returns:
        - ToolResultBlock with tool definitions as JSON
        - ToolDispatch trace event
        - List of successfully resolved tool names (for activation)
    """
    t0 = time.monotonic()
    requested = tool_use.input.get("tool_names", [])

    results: dict[str, Any] = {}
    activated: list[str] = []
    for name in requested:
        spec = all_tools_by_name.get(name)
        if spec is not None:
            results[name] = {
                "name": spec.name,
                "description": spec.description,
                "input_schema": spec.input_schema,
            }
            activated.append(name)
        else:
            results[name] = {"error": f"Unknown tool: {name}"}

    content_text = json.dumps(results, ensure_ascii=False, indent=2)
    latency_ms = (time.monotonic() - t0) * 1000

    result = ToolResultBlock(
        tool_use_id=tool_use.id,
        content=[TextBlock(text=content_text)],
        is_error=False,
    )
    dispatch_event = ToolDispatch(
        trace_id=trace_id,
        tool_use_id=tool_use.id,
        tool_name="get_tool_schema",
        endpoint_url="local://meta/get_tool_schema",
        request_body=tool_use.input,
        response_status=200,
        response_body=results,
        latency_ms=latency_ms,
    )
    return result, dispatch_event, activated


def render_skill_mode_tools(
    task: TaskDefinition, extra_tools: list[ToolSpec] | None = None
) -> str:
    """Render tool names with brief descriptions for system prompt in skill mode."""
    all_tools = list(task.tools) + (extra_tools or [])

    lines = [
        "## Available Tools",
        "You have access to the following tools. To use any tool, you must first",
        "call `get_tool_schema` with tool name(s) to retrieve the full definition.",
        "",
        "Tool names are case-sensitive. Available tools:",
    ]
    for tool in all_tools:
        lines.append(f"- {tool.name}: {tool.description}")
    lines.append("")
    lines.append(
        'Call get_tool_schema({"tool_names": ["tool_name_1", "tool_name_2"]}) '
        "to get tool definitions before using them."
    )
    return "\n".join(lines)
