"""Tests for the Activity Logs system-prompt section path style.

``_render_activity_logs_section`` advertises the user's staged work-history
logs. The two agent families resolve file paths against different roots, so the
advertised path style must match the consumer:

  * loop  → ``relative=True``  → ``logs/...`` (resolved against workspace_root
    by the sandbox read tool; correct in local AND container, no host paths).
  * OH    → ``relative=False`` → ``{workspace_root}/logs/...`` absolute (OH's
    read_file resolves relative paths against its own empty cwd, so it can only
    reach the logs via an absolute ``/workspace/logs`` inside the container).

These tests pin that contract so loop can't silently regress back to absolute
paths (which double-join to "File not found" in local mode).
"""

from __future__ import annotations

from pathlib import Path

from claw_anything.runner.system_prompt import _render_activity_logs_section


def _make_logs(workspace_root: Path) -> None:
    """Stage a minimal non-empty logs/ tree under ``workspace_root``."""
    logs = workspace_root / "logs"
    services = logs / "services"
    services.mkdir(parents=True)
    (logs / "timeline_20260105_20260111.md").write_text("# Activity Timeline\n| t |\n")
    (services / "calendar_activity.md").write_text("# Calendar Activity Log\n| t |\n")


def test_relative_emits_workspace_relative_paths(tmp_path: Path):
    ws = tmp_path / "trial" / "workspace"
    _make_logs(ws)

    section = _render_activity_logs_section(ws, relative=True)

    # Advertises relative logs/ paths the loop sandbox tool can resolve.
    assert "logs/services/calendar_activity.md" in section
    assert "logs/timeline_20260105_20260111.md" in section
    # No absolute workspace prefix and — crucially — no machine-specific host
    # path leaked into the prompt.
    assert f"{ws}/logs" not in section
    assert str(ws) not in section


def test_absolute_is_default_and_prefixes_workspace_root(tmp_path: Path):
    ws = tmp_path / "trial" / "workspace"
    _make_logs(ws)

    default = _render_activity_logs_section(ws)
    explicit = _render_activity_logs_section(ws, relative=False)

    # Default must stay absolute so the OH path keeps working unchanged.
    assert default == explicit
    assert f"{ws}/logs/services/calendar_activity.md" in default
    assert f"{ws}/logs/timeline_20260105_20260111.md" in default
    # A bare relative entry would mean the OH (empty-cwd) path can't resolve it.
    assert "- logs/services/" not in default


def test_empty_or_missing_logs_render_nothing(tmp_path: Path):
    # No logs/ dir at all.
    assert _render_activity_logs_section(tmp_path / "workspace", relative=True) == ""
    assert _render_activity_logs_section(None, relative=True) == ""

    # logs/ exists but only empty files -> still nothing.
    ws = tmp_path / "ws2"
    (ws / "logs").mkdir(parents=True)
    (ws / "logs" / "timeline_20260105_20260111.md").write_text("")
    assert _render_activity_logs_section(ws, relative=True) == ""
