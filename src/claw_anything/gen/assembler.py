"""Assembler: creates task directories with task.yaml, grader.py, and fixture symlinks."""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path

import yaml

import json

from .persona import GoldEnvironment, SERVICE_AUX_FIXTURES, SERVICE_FIXTURE_MAP

log = logging.getLogger(__name__)

# Path to the template directory (relative to this file)
_TEMPLATE_DIR = Path(__file__).resolve().parents[3] / "template"


def _sanitize_template_for_parse(content: str) -> str:
    """Replace <<<CUSTOMIZE: ...>>> markers with safe placeholder values so YAML can parse."""
    # Replace <<<CUSTOMIZE: xxx>>> with "PLACEHOLDER"
    return re.sub(r'<<<CUSTOMIZE[^>]*>>>', 'PLACEHOLDER', content)


class TaskAssembler:
    """Assembles a complete task directory from generated components."""

    def __init__(self, template_dir: Path | None = None, template_filename: str = "task_template.yaml"):
        self.template_dir = Path(template_dir) if template_dir else _TEMPLATE_DIR
        raw = (self.template_dir / template_filename).read_text(encoding="utf-8")

        # Pre-parse template to extract the fixed sections (services, tools, tool_endpoints)
        sanitized = _sanitize_template_for_parse(raw)
        # Replace GXXX_task_name with a safe placeholder for parsing
        sanitized = sanitized.replace("GXXX_task_name", "TEMPLATE_TASK")
        parsed = yaml.safe_load(sanitized)

        self._fixed_services = parsed["services"]
        self._fixed_tools = parsed["tools"]
        self._fixed_tool_endpoints = parsed["tool_endpoints"]

    def assemble(
        self,
        env: GoldEnvironment,
        task_id: str,
        task_yaml_content: str,
        grader_code: str,
        output_dir: Path,
        use_symlinks: bool = True,
    ) -> Path:
        """Create the complete task directory.

        Args:
            env: The gold base environment.
            task_id: Generated task ID (e.g., "E01_T01_expense_reconciliation").
            task_yaml_content: Complete task.yaml content (already has services/tools/endpoints).
            grader_code: Complete grader.py source code.
            output_dir: Parent directory for generated tasks (e.g., gen_tasks/).
            use_symlinks: If True, symlink fixtures; else copy them.

        Returns:
            Path to the created task directory.
        """
        task_dir = output_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)

        # Write task.yaml
        task_yaml_path = task_dir / "task.yaml"
        task_yaml_path.write_text(task_yaml_content, encoding="utf-8")
        log.info("Wrote %s", task_yaml_path)

        # Write grader.py
        grader_path = task_dir / "grader.py"
        grader_path.write_text(grader_code, encoding="utf-8")
        log.info("Wrote %s", grader_path)

        # Link or copy fixtures
        fixtures_link = task_dir / "fixtures"
        if fixtures_link.exists() or fixtures_link.is_symlink():
            if fixtures_link.is_symlink():
                fixtures_link.unlink()
            else:
                shutil.rmtree(fixtures_link)

        if use_symlinks:
            # Compute relative path from task_dir to env fixtures
            rel_path = os.path.relpath(env.fixtures_dir, task_dir)
            fixtures_link.symlink_to(rel_path)
            log.info("Symlinked fixtures -> %s", rel_path)
        else:
            shutil.copytree(env.fixtures_dir, fixtures_link)
            log.info("Copied fixtures to %s", fixtures_link)

        # Ensure auxiliary fixture files exist (e.g. claw_wechat/moments.json,
        # claw_qq/zone.json). Old gold envs built before these were added to
        # ALL_SERVICES will be missing them; create empty [] placeholders so the
        # mock service can start even when there is no Moments/QZone data.
        fixtures_dir = task_dir / "fixtures"
        if fixtures_dir.is_dir() and not fixtures_dir.is_symlink():
            for aux_files in SERVICE_AUX_FIXTURES.values():
                for aux_rel in aux_files:
                    aux_path = fixtures_dir / aux_rel
                    if not aux_path.exists():
                        aux_path.parent.mkdir(parents=True, exist_ok=True)
                        with open(aux_path, "w", encoding="utf-8") as f:
                            json.dump([], f)
                        log.info("Initialized missing aux fixture: %s", aux_path)

        # Copy logs/ directory if it exists in the gold environment
        env_logs_dir = env.env_dir / "logs"
        if env_logs_dir.exists() and env_logs_dir.is_dir():
            task_logs_dir = task_dir / "logs"
            if task_logs_dir.exists():
                shutil.rmtree(task_logs_dir)
            if use_symlinks:
                rel_path = os.path.relpath(env_logs_dir, task_dir)
                task_logs_dir.symlink_to(rel_path)
                log.info("Symlinked logs -> %s", rel_path)
            else:
                shutil.copytree(env_logs_dir, task_logs_dir)
                log.info("Copied logs to %s", task_logs_dir)

        return task_dir

    def build_task_yaml(
        self,
        task_id: str,
        task_name: str,
        category: str,
        difficulty: str,
        prompt_text: str,
        scoring_components: list[dict],
        safety_checks: list[dict],
        expected_actions: list[dict],
        judge_rubric: str,
        reference_solution: str,
        language: str = "en",
        timeout_seconds: int = 1200,
        max_turns: int = 100,
        output_dir: Path | None = None,
        execution_date: str | None = None,
        available_services: set[str] | None = None,
    ) -> str:
        """Build a complete task.yaml by combining generated content with the fixed template.

        The services/tools/tool_endpoints sections are taken from the pre-parsed template,
        with fixture paths updated to point to the task directory.

        If ``available_services`` is provided, services / tools / tool_endpoints /
        environment.fixtures / scoring_components / safety_checks are filtered so
        only the listed services and their tools survive — this persona does not
        "have" the other apps, and the agent should not see them.
        """
        import copy

        # Deep-copy the fixed sections and update fixture paths
        services = copy.deepcopy(self._fixed_services)
        tools = copy.deepcopy(self._fixed_tools)
        tool_endpoints = copy.deepcopy(self._fixed_tool_endpoints)
        scoring_components = copy.deepcopy(scoring_components)
        safety_checks = copy.deepcopy(safety_checks)

        # Apply persona-specific service filter.
        if available_services is not None:
            available = set(available_services)

            # 1. Drop service definitions not in the persona's app set.
            services = [s for s in services if s.get("name") in available]

            # 2. Drop tool_endpoints whose URL path segment names a dropped service,
            #    and remember the surviving tool names so we can mirror the filter
            #    onto the tool-schema list and scoring/safety entries.
            kept_tool_names: set[str] = set()
            filtered_endpoints: list[dict] = []
            for ep in tool_endpoints:
                m = re.match(r"https?://[^/]+/([^/]+)/", ep.get("url", ""))
                svc = m.group(1) if m else None
                if svc in available:
                    filtered_endpoints.append(ep)
                    kept_tool_names.add(ep.get("tool_name"))
            tool_endpoints = filtered_endpoints

            # 3. Drop tool JSON schemas for tools without a surviving endpoint.
            tools = [t for t in tools if t.get("name") in kept_tool_names]

            # 4. Drop scoring_components / safety_checks that reference a tool we removed.
            def _refs_dropped_tool(entry: dict) -> bool:
                # scoring_components: {"check": {"type": "tool_(not_)called", "tool_name": "..."}}
                check = entry.get("check") if isinstance(entry, dict) else None
                if isinstance(check, dict) and check.get("type") in {"tool_called", "tool_not_called"}:
                    tn = check.get("tool_name")
                    if tn and tn not in kept_tool_names:
                        return True
                # safety_checks: {"type": "tool_(not_)called", "tool_name": "..."}
                if isinstance(entry, dict) and entry.get("type") in {"tool_called", "tool_not_called"}:
                    tn = entry.get("tool_name")
                    if tn and tn not in kept_tool_names:
                        return True
                return False

            scoring_components = [e for e in scoring_components if not _refs_dropped_tool(e)]
            safety_checks = [e for e in safety_checks if not _refs_dropped_tool(e)]

        # Compute the actual task dir path relative to CWD (e.g. gen_tasks/E_li_ming_10/T01_T01_xxx)
        task_rel_path = str(Path(output_dir) / task_id) if output_dir else f"gen_tasks/{task_id}"
        for svc in services:
            env_dict = svc.get("env", {})
            for key, val in env_dict.items():
                if isinstance(val, str) and "TEMPLATE_TASK" in val:
                    env_dict[key] = val.replace("gen_tasks/TEMPLATE_TASK", task_rel_path)

        # Derive the fixtures list from the surviving services so new services
        # are picked up automatically and dropped ones are omitted. Include
        # any auxiliary fixture files for multi-fixture services (qq/wechat).
        fixtures_list: list[str] = []
        for svc in services:
            name = svc.get("name")
            if name in SERVICE_FIXTURE_MAP:
                fixtures_list.append(f"fixtures/{SERVICE_FIXTURE_MAP[name]}")
            for aux_rel in SERVICE_AUX_FIXTURES.get(name, []):
                fixtures_list.append(f"fixtures/{aux_rel}")

        # Build the complete data dict
        data = {
            "task_id": task_id,
            "task_name": task_name,
            "version": "1.0",
            "category": category,
            "difficulty": difficulty,
            "services": services,
            "tools": tools,
            "tool_endpoints": tool_endpoints,
            "environment": {
                "timeout_seconds": timeout_seconds,
                "max_turns": max_turns,
                "fixtures": fixtures_list,
            },
            "execution_date": execution_date,
            "prompt": {
                "text": prompt_text,
                "language": language,
            },
            "scoring_components": scoring_components,
            "safety_checks": safety_checks,
            "expected_actions": expected_actions,
            "judge_rubric": judge_rubric,
            "reference_solution": reference_solution,
            "primary_dimensions": ["completion", "robustness", "communication", "safety"],
        }

        return yaml.dump(
            data,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
            width=120,
        )
