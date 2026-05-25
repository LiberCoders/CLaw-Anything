"""Generate a per-task OpenHarness plugin that exposes claw-anything tools.

For each evaluation trial we materialise an isolated OH config dir with one
plugin (`clawanything`) whose ``tools/clawanything_tools.py`` defines a ``BaseTool``
subclass per ``ToolEndpoint`` declared in the task. OH's plugin loader picks
them up at startup; the generated tools forward calls to the mock-service HTTP
endpoints already running in the parent process and append a side-channel
record to ``$CLAW_ANYTHING_DISPATCH_LOG`` so the OpenHarness-family agents can
rebuild ``ToolDispatch`` events later.

Both ``OpenHarnessAgent`` (vanilla openharness-ai) and ``OpenHarnessExtAgent``
(OH-Ext fork) use this module. Each agent owns its own builtin-tool list (see
``builtin_tools_for_deny`` on each class) and passes it via the
``extra_denied_tools`` parameter on ``generate_plugin_files``. This module is
deliberately ignorant of which specific tools exist for which fork — that
knowledge lives next to the agent that needs it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..models.task import TaskDefinition


PLUGIN_NAME = "clawanything"


_PLUGIN_MANIFEST = {
    "name": PLUGIN_NAME,
    "version": "1.0.0",
    "description": "Auto-generated claw-anything task tools",
    "enabled_by_default": True,
    "tools_dir": "tools",
}


# Tools that are ALWAYS denied during a claw-anything trial, regardless of the
# ``--oh-disable-builtin-tools`` flag. These tools break the non-interactive batch
# eval contract — e.g. ``ask_user_question`` blocks the agent loop waiting for
# human input that will never arrive, so the trial just burns wall-clock until
# its deadline. Add new entries here when a builtin is found to be incompatible
# with unattended evaluation.
ALWAYS_DENIED_TOOLS = [
    "ask_user_question",
]


def _load_settings(path: Path | None) -> dict:
    """Load a user-provided OH settings.json as the baseline (or empty dict).

    Raises FileNotFoundError / ValueError with a helpful message if the path
    is set but unreadable or not valid JSON.
    """
    if path is None:
        return {}
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"--oh-settings file does not exist: {p}")
    try:
        text = p.read_text(encoding="utf-8")
        if not text.strip():
            return {}
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--oh-settings file is not valid JSON ({p}): {exc}") from exc


def _build_settings(
    *,
    extra_denied_tools: list[str] | None = None,
    base_settings: dict | None = None,
    apps: list[dict] | None = None,
    execution_date: str | None = None,
) -> dict:
    """Merge clawanything-required fields onto a (possibly user-provided) base settings dict.

    Merge rules:
      - ``enabled_plugins`` — additive: user's existing plugin flags kept,
        ``clawanything`` forced to True so OH loads our generated plugin.
      - ``allow_project_plugins`` — overridden to False (claw-anything owns the
        plugin set; we don't want OH scanning the cwd for stray plugins).
      - ``permission.mode`` — overridden to ``full_auto`` so OH never
        prompts (claw-anything batch is non-interactive).
      - ``permission.denied_tools`` — always unioned with
        ``ALWAYS_DENIED_TOOLS`` (tools that don't make sense under
        non-interactive eval). When the caller passes ``extra_denied_tools``
        (typically the agent's own builtin-tool list to suppress them), those
        names are added too. This module never looks up tool names itself —
        the caller (the active agent) owns the list and passes the right one.
      - ``prompt_meta`` — top-level dict carrying task-time metadata for OH:
        ``today`` (execution_date, all tasks) and ``apps`` (mobile_gui tasks
        only). OH forwards this to the GUI backend at runtime. apps is omitted
        when empty; today is omitted when None.
      - All other top-level fields from the base settings are preserved
        verbatim — that's the point of providing them.
    """
    settings: dict = json.loads(json.dumps(base_settings or {}))  # deep copy

    enabled = settings.get("enabled_plugins")
    if not isinstance(enabled, dict):
        enabled = {}
    enabled[PLUGIN_NAME] = True
    settings["enabled_plugins"] = enabled

    settings["allow_project_plugins"] = False

    perm = settings.get("permission")
    if not isinstance(perm, dict):
        perm = {}
    perm["mode"] = "full_auto"

    existing_denied = perm.get("denied_tools") or []
    if not isinstance(existing_denied, list):
        existing_denied = []
    extra_denied: list[str] = list(ALWAYS_DENIED_TOOLS)
    if extra_denied_tools:
        extra_denied.extend(extra_denied_tools)
    if existing_denied or extra_denied:
        perm["denied_tools"] = list(dict.fromkeys([*existing_denied, *extra_denied]))
    settings["permission"] = perm

    meta: dict = {}
    if apps:
        meta["apps"] = apps
    if execution_date:
        meta["today"] = execution_date
    if meta:
        existing_meta = settings.get("prompt_meta")
        if isinstance(existing_meta, dict):
            existing_meta.update(meta)
        else:
            settings["prompt_meta"] = meta

    return settings


_GENERATED_HEADER = '''"""Auto-generated by claw-anything. Do not edit."""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class _AnyArgs(BaseModel):
    """Permissive Pydantic model: accepts any tool input.

    The real schema is exposed to the LLM via ``to_api_schema`` below.
    """

    model_config = ConfigDict(extra="allow")


def _emit_dispatch(record: dict) -> None:
    path = os.environ.get("CLAW_ANYTHING_DISPATCH_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\\n")
    except Exception:
        pass


def _make_tool_class(
    *,
    tool_name: str,
    tool_description: str,
    endpoint_url: str,
    method: str,
    input_schema: dict,
) -> type[BaseTool]:
    class _GenTool(BaseTool):
        name = tool_name
        description = tool_description
        input_model = _AnyArgs

        def to_api_schema(self) -> dict:
            return {
                "name": tool_name,
                "description": tool_description,
                "input_schema": input_schema,
            }

        def is_read_only(self, arguments: BaseModel) -> bool:
            return False

        async def execute(self, arguments: BaseModel, context: ToolExecutionContext) -> ToolResult:
            payload = arguments.model_dump()
            t0 = time.monotonic()
            try:
                async with httpx.AsyncClient(timeout=30.0, trust_env=False) as cli:
                    resp = await cli.request(method=method, url=endpoint_url, json=payload)
                    status = resp.status_code
                    try:
                        body: Any = resp.json()
                    except Exception:
                        body = {"raw": resp.text}
                latency_ms = (time.monotonic() - t0) * 1000.0
                _emit_dispatch({
                    "tool_name": tool_name,
                    "endpoint_url": endpoint_url,
                    "request_body": payload,
                    "response_status": status,
                    "response_body": body,
                    "latency_ms": latency_ms,
                    "timestamp": time.time(),
                })
                return ToolResult(
                    output=json.dumps(body, ensure_ascii=False),
                    is_error=status >= 400,
                )
            except Exception as exc:
                _emit_dispatch({
                    "tool_name": tool_name,
                    "endpoint_url": endpoint_url,
                    "request_body": payload,
                    "response_status": 0,
                    "response_body": {"error": str(exc)},
                    "latency_ms": (time.monotonic() - t0) * 1000.0,
                    "timestamp": time.time(),
                })
                return ToolResult(output=f"Error: {exc}", is_error=True)

    safe = re.sub(r"\\W", "_", tool_name) or "tool"
    _GenTool.__name__ = f"_GenTool_{safe}"
    _GenTool.__qualname__ = _GenTool.__name__
    return _GenTool


# --- Per-task tool registrations ---
'''


def _safe_attr_name(tool_name: str) -> str:
    """Sanitise a tool name into a valid Python identifier for module-level
    attribute assignment. The name is what OH's plugin loader sees, so we
    keep it close to the original tool name without decorative prefixes."""
    safe = re.sub(r"\W", "_", tool_name) or "tool"
    if safe[0].isdigit():
        safe = "_" + safe
    return safe


def _render_tool_registration(
    *, tool_name: str, description: str, endpoint_url: str, method: str, input_schema: dict
) -> str:
    """Render one module-level assignment that the OH plugin loader will discover."""
    return (
        f"{_safe_attr_name(tool_name)} = _make_tool_class(\n"
        f"    tool_name={json.dumps(tool_name, ensure_ascii=False)},\n"
        f"    tool_description={json.dumps(description, ensure_ascii=False)},\n"
        f"    endpoint_url={json.dumps(endpoint_url)},\n"
        f"    method={json.dumps(method)},\n"
        f"    input_schema={json.dumps(input_schema, ensure_ascii=False)},\n"
        f")\n"
    )


_SKILL_MODE_GET_TOOL_SCHEMA_DESCRIPTION = (
    "Retrieve full definitions (description + JSON Schema) for tools by name. "
    "Some tools in this session are listed by name only — their description and "
    "input schema are intentionally omitted to save context. "
    "Call this before using any tool to get its complete definition."
)

_SKILL_MODE_GET_TOOL_SCHEMA_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "tool_names": {
            "type": "array",
            "items": {"type": "string"},
            "description": "List of tool names to retrieve schemas for.",
        }
    },
    "required": ["tool_names"],
}


def _render_skill_mode_get_tool_schema(task: TaskDefinition) -> str:
    """Render a self-contained get_tool_schema BaseTool with all task schemas baked in.

    The full tool definitions are embedded as a Python dict literal so that the
    generated plugin has no runtime dependency on the parent claw-anything
    process.  OH discovers and instantiates the class like any other plugin
    tool; ``execute()`` handles the lookup locally and writes a dispatch record
    (with ``endpoint_url="local://meta/get_tool_schema"``) so the trace
    reconstructor can account for it.
    """
    all_schemas = {
        spec.name: {
            "name": spec.name,
            "description": spec.description,
            "input_schema": spec.input_schema,
        }
        for spec in task.tools
    }
    desc_src = json.dumps(_SKILL_MODE_GET_TOOL_SCHEMA_DESCRIPTION, ensure_ascii=False)
    api_schema_src = json.dumps(
        {
            "name": "get_tool_schema",
            "description": _SKILL_MODE_GET_TOOL_SCHEMA_DESCRIPTION,
            "input_schema": _SKILL_MODE_GET_TOOL_SCHEMA_INPUT_SCHEMA,
        },
        ensure_ascii=False,
    )
    schemas_src = json.dumps(all_schemas, ensure_ascii=False, indent=4)
    return (
        f"\n_SKILL_MODE_SCHEMAS = {schemas_src}\n"
        "\n\n"
        "class _GetToolSchemaTool(BaseTool):\n"
        f'    name = "get_tool_schema"\n'
        f"    description = {desc_src}\n"
        "    input_model = _AnyArgs\n"
        "\n"
        "    def to_api_schema(self) -> dict:\n"
        f"        return {api_schema_src}\n"
        "\n"
        "    def is_read_only(self, arguments: BaseModel) -> bool:\n"
        "        return True\n"
        "\n"
        "    async def execute(self, arguments: BaseModel, context: ToolExecutionContext) -> ToolResult:\n"
        '        t0 = time.monotonic()\n'
        '        names = arguments.model_dump().get("tool_names", [])\n'
        '        results = {}\n'
        '        for n in names:\n'
        '            if n in _SKILL_MODE_SCHEMAS:\n'
        '                results[n] = _SKILL_MODE_SCHEMAS[n]\n'
        '            else:\n'
        '                results[n] = {"error": "Unknown tool: " + n}\n'
        '        latency_ms = (time.monotonic() - t0) * 1000.0\n'
        '        _emit_dispatch({\n'
        '            "tool_name": "get_tool_schema",\n'
        '            "endpoint_url": "local://meta/get_tool_schema",\n'
        '            "request_body": {"tool_names": names},\n'
        '            "response_status": 200,\n'
        '            "response_body": results,\n'
        '            "latency_ms": latency_ms,\n'
        '            "timestamp": time.time(),\n'
        '        })\n'
        '        return ToolResult(output=json.dumps(results, ensure_ascii=False), is_error=False)\n'
        "\n"
        "\n"
        "get_tool_schema = _GetToolSchemaTool\n"
    )


def _render_generated_tools(task: TaskDefinition, skill_mode: bool = False) -> str:
    """Render the full tools/clawanything_tools.py source for a task."""
    endpoints = task.get_endpoint_map()
    lines = [_GENERATED_HEADER]
    if skill_mode:
        lines.append(_render_skill_mode_get_tool_schema(task))
    for spec in task.tools:
        ep = endpoints.get(spec.name)
        if ep is None:
            # No HTTP endpoint declared — skip silently. Such tools (rare)
            # would need a different backend; the LLM will see them missing.
            continue
        if skill_mode:
            # Expose only the tool name; schema is available via get_tool_schema.
            # Empty input_schema ({}) makes OH send "parameters": {} instead of
            # the full {"type": "object", "properties": {}}, saving a few tokens
            # per tool in the API request.
            lines.append(
                _render_tool_registration(
                    tool_name=spec.name,
                    description="",
                    endpoint_url=ep.url,
                    method=ep.method,
                    input_schema={},
                )
            )
        else:
            lines.append(
                _render_tool_registration(
                    tool_name=spec.name,
                    description=spec.description,
                    endpoint_url=ep.url,
                    method=ep.method,
                    input_schema=spec.input_schema,
                )
            )
    return "".join(lines)


def generate_plugin_files(
    task: TaskDefinition,
    plugin_dir: Path,
    *,
    settings_root: Path,
    extra_denied_tools: list[str] | None = None,
    settings_path: Path | None = None,
    skill_mode: bool = False,
) -> None:
    """Materialise plugin.json, tools/clawanything_tools.py and settings.json on disk.

    Layout produced::

        <settings_root>/
            settings.json
            plugins/clawanything/
                plugin.json
                tools/__init__.py
                tools/clawanything_tools.py

    ``settings_path`` lets the caller pass a pre-existing OH
    settings.json (with model/provider/etc.) as the baseline; clawanything-
    required fields are merged on top — see ``_build_settings`` for the
    merge rules. If omitted, a minimal clawanything-only settings.json is
    written, which is usually NOT enough for OH to actually run.

    ``extra_denied_tools`` is the agent-specific builtin tool list to add to
    ``permission.denied_tools`` (typically passed when the caller wants to
    suppress OH's native tools so the model only sees the per-task clawanything
    tools). Each OH-family agent owns its own list (see
    ``OpenHarnessAgent.builtin_tools_for_deny`` and the OH-Ext override) and
    passes the appropriate union.

    ``skill_mode`` switches the generated plugin to progressive-revelation mode:
    task tools expose only their name (empty description, empty input_schema)
    and an extra ``get_tool_schema`` BaseTool is injected with all full schemas
    baked in.  The LLM is guided (via the get_tool_schema description) to call
    it before using any tool.  Works for both vanilla OH and OH-Ext.
    """
    plugin_dir = Path(plugin_dir)
    settings_root = Path(settings_root)
    tools_dir = plugin_dir / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    settings_root.mkdir(parents=True, exist_ok=True)

    base_settings = _load_settings(settings_path)
    merged = _build_settings(
        extra_denied_tools=extra_denied_tools,
        base_settings=base_settings,
        apps=task.apps or None,
        execution_date=task.execution_date or None,
    )

    (plugin_dir / "plugin.json").write_text(
        json.dumps(_PLUGIN_MANIFEST, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (tools_dir / "__init__.py").write_text("", encoding="utf-8")
    (tools_dir / "clawanything_tools.py").write_text(_render_generated_tools(task, skill_mode=skill_mode), encoding="utf-8")
    (settings_root / "settings.json").write_text(
        json.dumps(merged, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
