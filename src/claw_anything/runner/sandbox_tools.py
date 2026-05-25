"""Sandbox tool definitions for containerised agent execution."""

from __future__ import annotations

from ..models.tool import ToolSpec

# ---------------------------------------------------------------------------
# Tool specifications
# ---------------------------------------------------------------------------

_SHELL_EXEC = ToolSpec(
    name="sandbox_shell_exec",
    description="Execute a shell command inside the sandbox and return stdout/stderr.",
    input_schema={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute.",
            },
            "timeout_seconds": {
                "type": "integer",
                "description": "Max seconds before the command is killed (default: 30).",
                "default": 30,
            },
        },
        "required": ["command"],
    },
)

_FILE_READ = ToolSpec(
    name="sandbox_file_read",
    description="Read the contents of a file at the given path.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute or relative path to the file.",
            },
        },
        "required": ["path"],
    },
)

_FILE_WRITE = ToolSpec(
    name="sandbox_file_write",
    description="Write content to a file at the given path (creates parent directories if needed).",
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute or relative path to the file.",
            },
            "content": {
                "type": "string",
                "description": "The content to write to the file.",
            },
        },
        "required": ["path", "content"],
    },
)

_BROWSER_SCREENSHOT = ToolSpec(
    name="sandbox_browser_screenshot",
    description="Take a screenshot of the given URL and return a text description of the page.",
    input_schema={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to navigate to and screenshot.",
            },
        },
        "required": ["url"],
    },
)

# Full list of all sandbox tools
SANDBOX_TOOLS: list[ToolSpec] = [
    _SHELL_EXEC,
    _FILE_READ,
    _FILE_WRITE,
    _BROWSER_SCREENSHOT,
]


def get_sandbox_tools(
    *,
    enable_shell: bool = True,
    enable_browser: bool = True,
    enable_file: bool = True,
) -> list[ToolSpec]:
    """Return a filtered list of sandbox tools based on capability flags."""
    tools: list[ToolSpec] = []
    if enable_shell:
        tools.append(_SHELL_EXEC)
    if enable_file:
        tools.append(_FILE_READ)
        tools.append(_FILE_WRITE)
    if enable_browser:
        tools.append(_BROWSER_SCREENSHOT)
    return tools


def get_sandbox_tool_specs(
    existing_tool_names: set[str] | None = None,
) -> list[ToolSpec]:
    """Return sandbox ToolSpecs, excluding any names in *existing_tool_names*."""
    if not existing_tool_names:
        return list(SANDBOX_TOOLS)
    return [t for t in SANDBOX_TOOLS if t.name not in existing_tool_names]
