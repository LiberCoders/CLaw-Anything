from __future__ import annotations

import re
from pathlib import Path

import yaml


GUI_TOOL_RE = re.compile(r"\b([a-z][a-z0-9_]*_gui_[a-z0-9_]+)\b")


def test_gui_graders_and_task_text_use_exposed_tool_names() -> None:
    """Legacy GUI task text sometimes used ``*_gui_*`` tool names.

    The task tool declarations expose names without that infix, e.g.
    ``fossify_messages_send_message``. A grader looking for the old spelling
    silently misses real tool calls, so flag any old spelling whose exposed
    equivalent exists in the same task.
    """
    root = Path(__file__).resolve().parents[1] / "benchmark/gui"
    mismatches: list[str] = []

    for task_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        task_yaml = task_dir / "task.yaml"
        if not task_yaml.exists():
            continue

        data = yaml.safe_load(task_yaml.read_text(encoding="utf-8")) or {}
        exposed = {
            tool.get("name")
            for tool in data.get("tools", [])
            if isinstance(tool, dict)
        }
        exposed.update(
            endpoint.get("tool_name")
            for endpoint in data.get("tool_endpoints", [])
            if isinstance(endpoint, dict)
        )

        for path in (task_yaml, task_dir / "grader.py"):
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for old_name in sorted(set(GUI_TOOL_RE.findall(text))):
                exposed_name = old_name.replace("_gui_", "_", 1)
                if old_name not in exposed and exposed_name in exposed:
                    mismatches.append(
                        f"{path.relative_to(root.parents[0])}: "
                        f"{old_name} should be {exposed_name}"
                    )

    assert not mismatches, "\n".join(mismatches)
