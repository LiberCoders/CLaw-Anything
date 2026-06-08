"""Dynamic system prompt composer."""

from __future__ import annotations

import json
from pathlib import Path

from ..config import PromptConfig
from ..models.task import TaskDefinition

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

_LEGACY_SYSTEM_PROMPT = (
    "You are a helpful personal assistant. "
    "Use the provided tools to complete the user's request. "
    "Think step by step before acting."
)


def _resolve_prompt_path(path_str: str) -> Path:
    p = Path(path_str)
    if p.is_absolute():
        return p
    return (_REPO_ROOT / p).resolve()


def _load_file(path_str: str | None, *, strict: bool) -> tuple[str, str | None]:
    """Load markdown content; return (content, resolved_path_str)."""
    if not path_str:
        return "", None

    p = _resolve_prompt_path(path_str)
    if not p.exists():
        if strict:
            raise FileNotFoundError(f"Prompt file not found: {p}")
        return "", str(p)
    return p.read_text(encoding="utf-8"), str(p)


def _render_tool_definitions(task: TaskDefinition, extra_tools: list | None = None) -> str:
    all_tools = list(task.tools) + (extra_tools or [])
    if not all_tools:
        return "\n".join([
            "## Tooling",
            "Tool availability (filtered by policy):",
            "Tool names are case-sensitive. Call tools exactly as listed.",
            "- None",
        ])

    lines = [
        "## Tooling",
        "Tool availability (filtered by policy):",
        "Tool names are case-sensitive. Call tools exactly as listed.",
    ]
    for tool in all_tools:
        lines.append(f"- {tool.name}: {tool.description}")
    lines.append("When a first-class tool exists for an action, use the tool directly.")
    return "\n".join(lines)


def _render_agent_guidelines(task: TaskDefinition | None = None) -> str:
    """Render universal agent behavior guidelines injected into every prompt."""
    if task is not None:
        seen: list[str] = []
        for t in task.tools:
            prefix = t.name.split("_")[0]
            if prefix not in seen:
                seen.append(prefix)
        tool_examples = ", ".join(f"{p}_*" for p in seen[:5])
        if len(seen) > 5:
            tool_examples += ", etc."
    else:
        tool_examples = "gmail_*, calendar_*, todo_*, notes_*, finance_*, etc."

    return "\n".join([
        "## Agent Guidelines",
        "1. **Concise final answer**: deliver a clear, focused response. "
        "Do not pad the reply with step-by-step summaries or restate information the user already provided.",
        f"2. **Use HTTP API tools for app data**: retrieve the user's app "
        f"information (emails, calendar events, tasks, notes, …) exclusively "
        f"through the provided HTTP API tools ({tool_examples}). "
        "Do NOT use native file-system or shell tools (bash, read_file, "
        "write_file, grep, etc.) as a substitute — those tools are for "
        "code/file operations only.",
    ])


def _render_behavior_rules(cfg: PromptConfig) -> str:
    r = cfg.behavior_rules
    if cfg.text_tool_call_mode:
        tool_call_protocol = "\n".join([
            "Tool-call protocol: the model API does NOT support native function calls.",
            "Emit every tool call as XML markup inside <tool_call> tags using this exact format:",
            "<tool_call>",
            "<function=tool_name>",
            "<parameter=param_name>value</parameter>",
            "</tool_call>",
            "One <tool_call> block per tool invocation. Multiple calls: emit multiple consecutive blocks.",
            "Never use native API function call blocks.",
        ])
    else:
        tool_call_protocol = "\n".join([
            "Tool-call protocol is strict: use native API tool/function calls only.",
            "Never emit tool calls as plain text markup (for example: <tool_call>, <function=...>, <parameter=...>).",
            "If a tool is needed, issue a real tool call block instead of describing or simulating it in text.",
        ])
    return "\n".join([
        "## Tool Call Style",
        "Default: do not narrate routine, low-risk tool calls (just call the tool).",
        "Narrate only when it helps: multi-step work, complex tasks, or sensitive actions.",
        "Keep narration brief and value-dense.",
        tool_call_protocol,
        "",
        "## Safety",
        f"- Safety: {r.safety}",
        f"- Tool Call Style: {r.tool_call_style}",
        f"- Reply Tags: {r.reply_tags}",
        f"- Silent Reply: {r.silent_reply}",
        f"- Heartbeat: {r.heartbeat}",
    ])


def _render_skills(cfg: PromptConfig) -> str:
    skills = cfg.skills.default
    lines = [
        "## Skills (mandatory)",
        "Before replying: scan <available_skills> entries.",
        "- If exactly one skill clearly applies: read its SKILL.md using the configured read tool, then follow it.",
        "- If multiple skills could apply: choose the most specific one, then read and follow it.",
        "- If none clearly apply: do not read any SKILL.md.",
        "Constraints: never read more than one skill up front; only read after selecting.",
    ]
    if cfg.skills.load_via_tool_call:
        lines.append(
            f"The full SKILL.md content must be loaded dynamically via tool call (`{cfg.skills.read_tool_name}`) using the skill path."
        )
    if not skills:
        lines.append("<available_skills>\n</available_skills>")
        return "\n".join(lines)

    lines.append("The following skills provide specialized instructions for specific tasks.")
    lines.append("<available_skills>")
    for s in skills:
        lines.append("  <skill>")
        lines.append(f"    <name>{s.name}</name>")
        lines.append(f"    <description>{s.description}</description>")
        lines.append(f"    <location>{s.path}</location>")
        lines.append("  </skill>")
    lines.append("</available_skills>")
    return "\n".join(lines)


def _render_workspace_blocks(cfg: PromptConfig) -> str:
    fcfg = cfg.files
    strict = cfg.strict_file_check
    agents, agents_path = _load_file(fcfg.agents_md, strict=strict)
    soul, soul_path = _load_file(fcfg.soul_md, strict=strict)
    user, user_path = _load_file(fcfg.user_md, strict=strict)
    tools, tools_path = _load_file(fcfg.tools_md, strict=strict)

    sections = ["## Workspace Files (injected)"]
    for title, content, p in [
        ("AGENTS.md", agents, agents_path),
        ("SOUL.md", soul, soul_path),
        ("USER.md", user, user_path),
        ("TOOLS.md", tools, tools_path),
    ]:
        if content:
            sections.append(f"### {title}")
            sections.append(f"Source: {p}")
            sections.append(content.strip())
        elif p:
            sections.append(f"### {title}")
            sections.append(f"Source: {p}")
            sections.append("[MISSING] Expected file not found or empty.")
    return "\n\n".join(sections)


def _render_text_tool_call_protocol() -> str:
    return "\n".join([
        "## Tool-Call Protocol",
        "The model API does NOT support native function calls. Emit EVERY tool call as XML markup using EXACTLY this format:",
        "<tool_call>",
        "<function=tool_name>",
        "<parameter=param_name>value</parameter>",
        "</tool_call>",
        "Rules:",
        "- One <tool_call> block per invocation; emit multiple consecutive blocks for parallel calls.",
        "- Use the exact tool name from the Tooling section.",
        "- Each parameter goes in its own <parameter=NAME>VALUE</parameter> tag.",
        "- Values may be plain text, numbers (123, 1.5), booleans (true/false), null, or JSON objects/arrays.",
        "- Never describe a tool call in prose without emitting the actual <tool_call> block.",
    ])


def _render_tool_schemas(task: TaskDefinition, extra_tools: list | None = None) -> str:
    all_tools = list(task.tools) + (extra_tools or [])
    if not all_tools:
        return ""
    lines = ["## Tool Schemas", "Complete JSON Schema for available tools:"]
    for tool in all_tools:
        schema_json = json.dumps(tool.input_schema, ensure_ascii=False, indent=2)
        lines.append(f"- {tool.name}")
        lines.append("```json")
        lines.append(schema_json)
        lines.append("```")
    return "\n".join(lines)


def _render_activity_logs_section(workspace_root: Path | None) -> str:
    """If ``workspace_root/logs`` exists, render an activity logs section.

    This tells the agent about the user's historical work logs WITHOUT
    mentioning them in the task prompt (which is written from the user's
    perspective). The host stages these logs under the agent's workspace
    (``_prepare_workspace`` in cli.py), so the advertised paths are absolute
    paths under ``workspace_root`` — inside a trial container that is
    ``/workspace/logs/...``; standalone it's the per-trial host workspace.
    The agent reads them with its native file tools (loop's sandbox
    read_file / OH's read_file), both of which honour absolute paths.
    """
    if workspace_root is None:
        return ""
    logs_dir = workspace_root / "logs"
    if not logs_dir.exists() or not logs_dir.is_dir():
        return ""

    # Check if there are any non-empty log files
    has_content = False
    for f in logs_dir.rglob("*.md"):
        if f.stat().st_size > 0:
            has_content = True
            break
    if not has_content:
        return ""

    base = f"{workspace_root}/logs"
    lines = [
        "## Activity Logs",
        "The user's work history logs are available at the following locations:",
    ]

    services_dir = logs_dir / "services"
    if services_dir.exists():
        service_logs = sorted(services_dir.glob("*_activity.md"))
        if service_logs:
            lines.append(f"- {base}/services/ — per-app activity logs:")
            for sl in service_logs:
                svc_name = sl.stem.replace("_activity", "")
                lines.append(f"  - {base}/services/{sl.name} ({svc_name})")

    weekly = sorted(logs_dir.glob("timeline_*.md"))
    weekly = [p for p in weekly if p.stat().st_size > 0]
    if weekly:
        lines.append(
            f"- {base}/timeline_YYYYMMDD_YYYYMMDD.md — merged activity timeline "
            "split into weekly files (Monday–Sunday ISO weeks; first/last file clipped "
            "to actual data range). Read only the week(s) you need:"
        )
        for p in weekly:
            rng = _parse_weekly_range(p.name)
            if rng is not None:
                start, end = rng
                lines.append(f"  - {base}/{p.name} ({start} to {end})")
            else:
                lines.append(f"  - {base}/{p.name}")
    else:
        legacy = logs_dir / "timeline.md"
        if legacy.exists() and legacy.stat().st_size > 0:
            lines.append(f"- {base}/timeline.md — System Log (legacy monolithic file)")

    lines.append("")
    lines.append("You can read these files to understand the user's prior work context and decisions.")
    lines.append("Use this history to inform your actions when relevant.")
    return "\n".join(lines)


def _parse_weekly_range(filename: str) -> tuple[str, str] | None:
    """Extract the human-readable (start, end) dates from ``timeline_YYYYMMDD_YYYYMMDD.md``."""
    stem = filename.removesuffix(".md")
    parts = stem.split("_")
    if len(parts) != 3 or parts[0] != "timeline":
        return None
    start_raw, end_raw = parts[1], parts[2]
    if len(start_raw) != 8 or len(end_raw) != 8 or not start_raw.isdigit() or not end_raw.isdigit():
        return None
    start = f"{start_raw[:4]}-{start_raw[4:6]}-{start_raw[6:]}"
    end = f"{end_raw[:4]}-{end_raw[4:6]}-{end_raw[6:]}"
    return start, end


def build_system_prompt(
    task: TaskDefinition,
    prompt_cfg: PromptConfig | None,
    *,
    extra_tools: list | None = None,
    current_date: str | None = None,
    workspace_root: Path | None = None,
) -> str:
    """Build a dynamic system prompt from runtime config + task tools.

    Args:
        extra_tools: Additional tool specs (e.g. sandbox tools) to include
            in the tool definitions and schema sections of the prompt.
        workspace_root: The agent's workspace dir. When it contains a
            ``logs/`` subtree (staged by ``_prepare_workspace``), an
            "Activity Logs" section advertising those files is injected.
    """
    # Resolve current date: explicit parameter > task.execution_date
    effective_date = current_date or getattr(task, "execution_date", None)

    if prompt_cfg is None or not prompt_cfg.enabled:
        if effective_date:
            return f"Current date: {effective_date}\n\n{_LEGACY_SYSTEM_PROMPT}"
        return _LEGACY_SYSTEM_PROMPT

    blocks: list[str] = [
        "You are a personal assistant running inside OpenClaw.",
    ]
    if effective_date:
        blocks.append(f"Current date: {effective_date}")
    if prompt_cfg.skill_mode:
        from .skill_mode import render_skill_mode_tools
        blocks.append(render_skill_mode_tools(task, extra_tools))
    else:
        blocks.append(_render_tool_definitions(task, extra_tools))
        if prompt_cfg.include_tool_schema:
            blocks.append(_render_tool_schemas(task, extra_tools))
    blocks.append(_render_behavior_rules(prompt_cfg))
    blocks.append(_render_skills(prompt_cfg))
    blocks.append(_render_workspace_blocks(prompt_cfg))

    # Inject activity logs section if present
    activity_logs = _render_activity_logs_section(workspace_root)
    if activity_logs:
        blocks.append(activity_logs)

    blocks.append(_render_agent_guidelines(task))

    return "\n\n".join(blocks).strip()


def build_task_tools_prompt(
    task: TaskDefinition,
    prompt_cfg: PromptConfig | None,
    *,
    extra_tools: list | None = None,
    current_date: str | None = None,
    task_dir: Path | None = None,
    include_date: bool = True,
) -> str:
    """Render ONLY the task-tool-related sections:

    - Current date (suppressible via ``include_date=False``)
    - Tool definitions (or skill_mode renderer)
    - Optional tool schemas
    - Activity logs section (if task_dir/logs exists)

    Used by host-style agents (e.g. OpenHarness) that have their own base
    prompt and only need claw-anything to inform them about the task surface,
    *without* claw-anything's own personal-assistant role declaration, behavior
    rules, skill system, or workspace files (which the OH base prompt and
    its own tool registry already cover).

    Set ``include_date=False`` when the host agent injects the task date
    elsewhere (e.g. via OH's ``EnvironmentInfo.date`` override) to avoid
    presenting two conflicting "current date" lines.
    """
    effective_date = current_date or getattr(task, "execution_date", None)
    blocks: list[str] = []
    if include_date and effective_date:
        blocks.append(f"Current date: {effective_date}")
    if prompt_cfg is not None and prompt_cfg.enabled and prompt_cfg.skill_mode:
        from .skill_mode import render_skill_mode_tools
        blocks.append(render_skill_mode_tools(task, extra_tools))
    else:
        blocks.append(_render_tool_definitions(task, extra_tools))
        if prompt_cfg is not None and prompt_cfg.enabled and prompt_cfg.include_tool_schema:
            blocks.append(_render_tool_schemas(task, extra_tools))
    if prompt_cfg is not None and prompt_cfg.enabled and prompt_cfg.text_tool_call_mode:
        blocks.append(_render_text_tool_call_protocol())
    activity_logs = _render_activity_logs_section(task_dir)
    if activity_logs:
        blocks.append(activity_logs)
    blocks.append(_render_agent_guidelines(task))
    return "\n\n".join(blocks).strip()

