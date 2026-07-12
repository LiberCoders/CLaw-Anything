"""Validator: quality control checks for generated tasks."""

from __future__ import annotations

import importlib.util
import inspect
import json
import logging
import re
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from .persona import GoldEnvironment, SERVICE_ID_FIELD, ALL_SERVICES

log = logging.getLogger(__name__)


class Severity(str, Enum):
    """Validation result severity. BLOCKING failures must reject the task;
    WARNING failures only get logged."""

    BLOCKING = "blocking"
    WARNING = "warning"


# Match the longest service prefix first so `claw_notion` wins over `notion`,
# `claw_wechat_moments` wins over `claw_wechat`, etc.
_SERVICES_BY_LENGTH = sorted(ALL_SERVICES, key=len, reverse=True)


def _extract_service(tool_name: str) -> str | None:
    """Map a tool name like `claw_notion_append_blocks` to its declaring
    service `claw_notion`. Returns None if no service in ALL_SERVICES is a
    prefix of the tool name.

    Multi-segment service names (`claw_notion`, `claw_wechat_moments`) are
    correctly resolved by matching the longest prefix first.
    """
    if not tool_name:
        return None
    for svc in _SERVICES_BY_LENGTH:
        if tool_name == svc or tool_name.startswith(svc + "_"):
            return svc
    return None


class ValidationResult:
    """Result of a single validation check."""

    def __init__(
        self,
        name: str,
        passed: bool,
        message: str = "",
        severity: Severity = Severity.BLOCKING,
    ):
        self.name = name
        self.passed = passed
        self.message = message
        self.severity = severity

    def __repr__(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        return f"[{status}] {self.name}: {self.message}"


class TaskValidator:
    """Validates generated task directories for correctness and quality."""

    def validate(
        self,
        task_dir: Path,
        env: GoldEnvironment,
    ) -> list[ValidationResult]:
        """Run all validation checks on a generated task.

        Args:
            task_dir: Path to the task directory (containing task.yaml, grader.py, fixtures/).
            env: The gold base environment used to generate the task.

        Returns:
            List of validation results.
        """
        results: list[ValidationResult] = []

        results.append(self._check_schema(task_dir))
        results.append(self._check_grader_compilation(task_dir))
        results.append(self._check_fixture_references(task_dir, env))
        results.append(self._check_service_coverage(task_dir))
        results.append(self._check_signal_density(task_dir, env))
        results.append(self._check_safety_consistency(task_dir))
        results.append(self._check_grader_tool_existence(task_dir))
        results.append(self._check_effect_grounding(task_dir))
        results.append(self._check_fixture_collision(task_dir))
        results.append(self._check_answer_sheet(task_dir))

        return results

    def _check_schema(self, task_dir: Path) -> ValidationResult:
        """Check that task.yaml can be parsed as valid YAML with required fields."""
        task_yaml = task_dir / "task.yaml"
        if not task_yaml.exists():
            return ValidationResult("schema", False, "task.yaml not found")

        try:
            with open(task_yaml, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            return ValidationResult("schema", False, f"YAML parse error: {e}")

        required_fields = [
            "task_id", "task_name", "services", "tools", "tool_endpoints",
            "prompt", "scoring_components", "safety_checks",
        ]
        missing = [f for f in required_fields if f not in data]
        if missing:
            return ValidationResult("schema", False, f"Missing fields: {missing}")

        # Check prompt has text
        prompt = data.get("prompt", {})
        if not prompt.get("text"):
            return ValidationResult("schema", False, "prompt.text is empty")

        # Check services count. The exact number depends on the task template
        # used (4 for temp_template, 28+ for task_template) and on which apps
        # the persona actually has data for, so we only enforce that at least
        # one service made it through.
        services = data.get("services", [])
        if not services:
            return ValidationResult("schema", False, "task.yaml has no services")

        return ValidationResult("schema", True, "task.yaml is valid")

    def _check_grader_compilation(self, task_dir: Path) -> ValidationResult:
        """Check that grader.py compiles and defines an AbstractGrader subclass."""
        grader_path = task_dir / "grader.py"
        if not grader_path.exists():
            return ValidationResult("grader_compile", False, "grader.py not found")

        code = grader_path.read_text(encoding="utf-8")

        # Syntax check
        try:
            compile(code, str(grader_path), "exec")
        except SyntaxError as e:
            return ValidationResult("grader_compile", False, f"Syntax error at line {e.lineno}: {e.msg}")

        # Try to import
        try:
            module_name = f"_validate_{task_dir.name}"
            spec = importlib.util.spec_from_file_location(module_name, grader_path)
            if spec is None or spec.loader is None:
                return ValidationResult("grader_compile", False, "Cannot create module spec")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as e:
            return ValidationResult("grader_compile", False, f"Import error: {e}")

        # Check for AbstractGrader subclass
        from claw_anything.graders.base import AbstractGrader
        found_grader = False
        for name, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, AbstractGrader) and obj is not AbstractGrader:
                found_grader = True
                break

        if not found_grader:
            return ValidationResult("grader_compile", False, "No AbstractGrader subclass found")

        # Check for FORBIDDEN_TOOLS
        if "FORBIDDEN_TOOLS" not in code:
            return ValidationResult("grader_compile", False, "FORBIDDEN_TOOLS not defined")

        return ValidationResult("grader_compile", True, "grader.py compiles and defines grader class")

    def _check_fixture_references(self, task_dir: Path, env: GoldEnvironment) -> ValidationResult:
        """Check that record IDs in reference_solution exist in fixture data."""
        task_yaml = task_dir / "task.yaml"
        with open(task_yaml, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        ref_solution = data.get("reference_solution", "")
        judge_rubric = data.get("judge_rubric", "")
        combined_text = ref_solution + "\n" + judge_rubric

        # Extract all record IDs from the text
        id_pattern = r'(MSG|TK|CUS|CON|KB|SKU|EVT|TODO|TXN|JOB|RSS|CFG|NOTE|INT)-\w+'
        found_ids = set(re.findall(id_pattern, combined_text))
        # Reconstruct full IDs
        full_ids = set(re.findall(r'(?:MSG|TK|CUS|CON|KB|SKU|EVT|TODO|TXN|JOB|RSS|CFG|NOTE|INT)-[A-Za-z0-9_]+', combined_text))

        if not full_ids:
            return ValidationResult("fixture_refs", True, "No specific record IDs in reference_solution (OK if using search-based approach)")

        # Check each ID exists in fixtures. Iterate only the services this
        # env actually has data for — scanning all 50 entries in ALL_SERVICES
        # (most of which the persona never used) used to emit a "Fixture not
        # found" warning per miss, drowning the gen-eval log.
        all_fixture_ids: set[str] = set()
        for svc in env.available_services:
            records = env.get_fixtures(svc)
            id_field = SERVICE_ID_FIELD.get(svc, "id")
            for r in records:
                if id_field in r:
                    all_fixture_ids.add(r[id_field])

        missing = full_ids - all_fixture_ids
        if missing:
            return ValidationResult(
                "fixture_refs", False,
                f"Record IDs not found in fixtures: {missing}"
            )

        return ValidationResult("fixture_refs", True, f"All {len(full_ids)} record IDs verified in fixtures")

    def _check_service_coverage(self, task_dir: Path) -> ValidationResult:
        """Check that the task involves at least 2 different services.

        Service names are matched against ALL_SERVICES via longest-prefix lookup
        (`_extract_service`) so multi-segment services like `claw_notion` and
        `claw_wechat_moments` are correctly recognized — the previous
        `tool_name.split("_")[0]` would have mapped them all to the bogus
        `"claw"` and dropped them silently.
        """
        task_yaml = task_dir / "task.yaml"
        with open(task_yaml, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        ref_solution = data.get("reference_solution", "")
        scoring = data.get("scoring_components", [])
        expected_actions = data.get("expected_actions", [])

        services_mentioned: set[str] = set()

        # From scoring_components.check.tool_name
        for sc in scoring:
            check = sc.get("check", {})
            svc = _extract_service(check.get("tool_name", ""))
            if svc:
                services_mentioned.add(svc)

        # From expected_actions[].service (already a clean service name)
        for ea in expected_actions:
            svc_name = ea.get("service") if isinstance(ea, dict) else None
            if svc_name in ALL_SERVICES:
                services_mentioned.add(svc_name)

        # From reference_solution: scan every snake_case identifier and try to
        # resolve it to a service. No verb whitelist — that approach was
        # missing append/post/archive/history/pin/etc.
        for match in re.finditer(r'\b([a-z][a-z0-9_]+)\b', ref_solution):
            svc = _extract_service(match.group(1))
            if svc:
                services_mentioned.add(svc)

        if len(services_mentioned) < 2:
            return ValidationResult(
                "service_coverage", False,
                f"Task only involves {len(services_mentioned)} service(s): {services_mentioned}. Need >=2",
                severity=Severity.BLOCKING,
            )

        return ValidationResult(
            "service_coverage", True,
            f"Task involves {len(services_mentioned)} services: {services_mentioned}",
            severity=Severity.BLOCKING,
        )

    def _check_signal_density(self, task_dir: Path, env: GoldEnvironment) -> ValidationResult:
        """Check that signal records are < 25% of total records."""
        task_yaml = task_dir / "task.yaml"
        with open(task_yaml, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        ref_solution = data.get("reference_solution", "")
        judge_rubric = data.get("judge_rubric", "")
        combined = ref_solution + "\n" + judge_rubric

        # Count referenced record IDs
        full_ids = set(re.findall(
            r'(?:MSG|TK|CUS|CON|KB|SKU|EVT|TODO|TXN|JOB|RSS|CFG|NOTE|INT)-[A-Za-z0-9_]+',
            combined,
        ))

        total = env.get_total_records()
        if total == 0:
            return ValidationResult("signal_density", True, "No fixture records (empty env)")

        density = len(full_ids) / total
        if density > 0.30:
            return ValidationResult(
                "signal_density", False,
                f"Signal density {density:.1%} exceeds 30% ({len(full_ids)}/{total} records)",
                severity=Severity.WARNING,
            )

        return ValidationResult(
            "signal_density", True,
            f"Signal density {density:.1%} ({len(full_ids)}/{total} records)",
            severity=Severity.WARNING,
        )

    def _check_safety_consistency(self, task_dir: Path) -> ValidationResult:
        """Check that safety_checks tools match FORBIDDEN_TOOLS in grader.py."""
        task_yaml = task_dir / "task.yaml"
        with open(task_yaml, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        grader_path = task_dir / "grader.py"
        grader_code = grader_path.read_text(encoding="utf-8")

        # Extract tool names from safety_checks
        yaml_forbidden = set()
        for sc in data.get("safety_checks", []):
            tool_name = sc.get("tool_name", "")
            if tool_name:
                yaml_forbidden.add(tool_name)

        # Extract FORBIDDEN_TOOLS from grader code
        # Look for the set definition
        forbidden_match = re.search(
            r'FORBIDDEN_TOOLS\s*=\s*\{([^}]+)\}',
            grader_code,
            re.DOTALL,
        )
        grader_forbidden = set()
        if forbidden_match:
            # Extract quoted strings
            grader_forbidden = set(re.findall(r'"([^"]+)"', forbidden_match.group(1)))

        # safety_checks tools must be subset of FORBIDDEN_TOOLS
        missing_in_grader = yaml_forbidden - grader_forbidden
        if missing_in_grader:
            return ValidationResult(
                "safety_consistency", False,
                f"safety_checks tools missing from grader FORBIDDEN_TOOLS: {missing_in_grader}",
                severity=Severity.WARNING,
            )

        return ValidationResult(
            "safety_consistency", True,
            f"All {len(yaml_forbidden)} safety_check tools present in FORBIDDEN_TOOLS "
            f"(grader has {len(grader_forbidden)} total)",
            severity=Severity.WARNING,
        )

    def _check_effect_grounding(self, task_dir: Path) -> ValidationResult:
        """For ACTION tasks (with expected_effects), require the grader to verify
        world-state via audit_data, and require every effect's action_key to be a
        real audit key. This is what stops a regression back to narration-only
        grading that misses silent write-failures.
        """
        from claw_anything.graders.base import ACTION_KEYS

        task_yaml = task_dir / "task.yaml"
        with open(task_yaml, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        effects = data.get("expected_effects", []) or []
        if not effects:
            # advisory task — nothing to verify here
            return ValidationResult(
                "effect_grounding", True,
                "No expected_effects (advisory task) — state check N/A",
                severity=Severity.WARNING,
            )

        # 1) every action_key must be a real audit key for its service
        bad_keys = []
        for e in effects:
            svc, key = e.get("service"), e.get("action_key")
            if svc not in ACTION_KEYS or key not in ACTION_KEYS.get(svc, []):
                bad_keys.append(f"{svc}.{key}")
        if bad_keys:
            return ValidationResult(
                "effect_grounding", False,
                f"expected_effects use unknown audit keys: {bad_keys}. "
                f"Use real keys from ACTION_KEYS (e.g. gmail.sent_messages, calendar.deleted).",
                severity=Severity.BLOCKING,
            )

        # 2) the grader must verify state, not just narration
        grader_code = (task_dir / "grader.py").read_text(encoding="utf-8")
        if not any(tok in grader_code for tok in (
            "assert_effect", "get_service_actions", "detect_silent_failure", "EXPECTED_EFFECTS",
        )):
            return ValidationResult(
                "effect_grounding", False,
                "Task has expected_effects but grader.py never verifies world-state via "
                "audit_data (no assert_effect / get_service_actions / detect_silent_failure / "
                "EXPECTED_EFFECTS). It would score narration and miss silent write-failures.",
                severity=Severity.BLOCKING,
            )

        return ValidationResult(
            "effect_grounding", True,
            f"{len(effects)} expected_effects with valid keys; grader verifies world-state",
            severity=Severity.BLOCKING,
        )

    # (service, action_key) → (fixture filenames, severity).
    # Severity reflects how a real agent reacts to the collision:
    #   BLOCKING — a reasonable agent will NOT redo the work when the gold
    #     state already exists (e.g. won't create a duplicate calendar event),
    #     so the task is unsolvable.
    #   WARNING — an agent will likely still perform the write even when a
    #     similar record exists (todos, drafts), but it's a poor-design signal.
    # gmail.sent_messages can't collide here because fixtures only ship
    # `inbox.json` (incoming) — outgoing mail has no pre-state.
    _COLLISION_FIXTURES: dict[tuple[str, str], tuple[list[str], "Severity"]] = {
        ("calendar", "created_events"): (["events.json"], Severity.BLOCKING),
        ("gmail", "drafts"):            (["drafts.json"], Severity.BLOCKING),
        ("todo", "created_tasks"):      (["tasks.json"],  Severity.WARNING),
    }

    def _check_fixture_collision(self, task_dir: Path) -> ValidationResult:
        """Reject tasks whose gold `expected_effects` state already exists in
        the seed fixtures — the agent's write would be a no-op and the grader
        would always judge it as a silent failure.

        We use the same case-insensitive substring matching as
        `AbstractGrader._record_matches` so a "collision" here is exactly what
        would later satisfy `assert_effect` at grade time.
        """
        from claw_anything.graders.base import AbstractGrader

        task_yaml = task_dir / "task.yaml"
        with open(task_yaml, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        effects = data.get("expected_effects", []) or []
        if not effects:
            return ValidationResult(
                "fixture_collision", True,
                "No expected_effects to check for collisions",
                severity=Severity.WARNING,
            )

        blocking: list[str] = []
        warning: list[str] = []
        for e in effects:
            svc = e.get("service")
            ak = e.get("action_key")
            match = e.get("match") or {}
            if not match:
                continue
            cfg = self._COLLISION_FIXTURES.get((svc, ak))
            if cfg is None:
                continue
            files, sev = cfg
            fixture_dir = task_dir / "fixtures" / svc
            if not fixture_dir.exists():
                continue
            for fname in files:
                fp = fixture_dir / fname
                if not fp.exists():
                    continue
                try:
                    records = json.loads(fp.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if not isinstance(records, list):
                    continue
                hits = [
                    r for r in records
                    if isinstance(r, dict) and AbstractGrader._record_matches(r, match)
                ]
                if hits:
                    sample = hits[0]
                    sid = (sample.get("event_id") or sample.get("message_id") or
                           sample.get("task_id") or sample.get("ticket_id") or
                           sample.get("title") or "<no-id>")
                    msg = f"{svc}.{ak} match={match} already satisfied by {fname} record {sid!r}"
                    (blocking if sev == Severity.BLOCKING else warning).append(msg)

        if blocking:
            return ValidationResult(
                "fixture_collision", False,
                "expected_effects collide with seed fixtures (gold state pre-exists, "
                "agent will reasonably skip the write): " + "; ".join(blocking),
                severity=Severity.BLOCKING,
            )
        if warning:
            return ValidationResult(
                "fixture_collision", False,
                "expected_effects share match keys with seed fixtures (agent may still "
                "land the write but task design is ambiguous): " + "; ".join(warning),
                severity=Severity.WARNING,
            )
        return ValidationResult(
            "fixture_collision", True,
            f"{len(effects)} expected_effects don't collide with seed fixtures",
            severity=Severity.BLOCKING,
        )

    def _check_grader_tool_existence(self, task_dir: Path) -> ValidationResult:
        """Check that tools referenced in grader.py actually exist in task.yaml."""
        task_yaml = task_dir / "task.yaml"
        grader_path = task_dir / "grader.py"

        if not task_yaml.exists() or not grader_path.exists():
            return ValidationResult("grader_tool_existence", False, "Missing task.yaml or grader.py")

        with open(task_yaml, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        # Collect all valid tool names from task.yaml
        valid_tools = set()
        for tool_def in data.get("tools", []):
            name = tool_def.get("name", "")
            if name:
                valid_tools.add(name)

        grader_code = grader_path.read_text(encoding="utf-8")

        # Extract all tool name strings referenced in grader code
        # Match quoted strings that look like tool names (service_action pattern)
        tool_pattern = r'"([a-z]+_[a-z_]+)"'
        referenced_tools = set(re.findall(tool_pattern, grader_code))

        # Filter to only those that look like service tools (have at least one underscore)
        # and are not Python builtins or other strings
        hallucinated = set()
        for tool in referenced_tools:
            # Use _extract_service so multi-segment service names like
            # `claw_notion_*` are recognized — `tool.split("_")[0]` would
            # have skipped them.
            if _extract_service(tool) and tool not in valid_tools:
                hallucinated.add(tool)

        if hallucinated:
            return ValidationResult(
                "grader_tool_existence", False,
                f"Grader references {len(hallucinated)} non-existent tools: {sorted(hallucinated)}"
            )

        return ValidationResult(
            "grader_tool_existence", True,
            f"All grader tool references exist in task.yaml"
        )

    def _check_answer_sheet(self, task_dir: Path) -> ValidationResult:
        """Validate answer_sheet items when present."""
        task_yaml = task_dir / "task.yaml"
        with open(task_yaml, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        sheet = data.get("answer_sheet") or {}
        items = sheet.get("items") or []
        if not items:
            return ValidationResult(
                "answer_sheet", True, "no answer_sheet (legacy task)",
                severity=Severity.WARNING,
            )

        valid_scorers = {
            "tool_call", "forbidden_tool", "effect_assert",
            "grounding", "enum_match", "llm_judge",
        }
        valid_kinds = {"objective", "subjective"}
        valid_fills = {"rule", "llm_extract"}
        ids: set[str] = set()
        weight_sum = 0.0
        errors: list[str] = []

        for i, item in enumerate(items):
            if not isinstance(item, dict):
                errors.append(f"item[{i}] is not a dict")
                continue
            iid = item.get("id", "")
            if not iid:
                errors.append(f"item[{i}] missing id")
            elif iid in ids:
                errors.append(f"duplicate id {iid!r}")
            else:
                ids.add(iid)
            if item.get("kind") not in valid_kinds:
                errors.append(f"{iid}: invalid kind {item.get('kind')!r}")
            if item.get("fill") not in valid_fills:
                errors.append(f"{iid}: invalid fill {item.get('fill')!r}")
            scorer = item.get("scorer", "")
            if scorer not in valid_scorers:
                errors.append(f"{iid}: invalid scorer {scorer!r}")
            weight_sum += float(item.get("weight", 0) or 0)
            if scorer == "enum_match":
                opts = item.get("options") or []
                expected = item.get("expected") or []
                if isinstance(expected, list):
                    for exp in expected:
                        if exp not in opts:
                            errors.append(
                                f"{iid}: expected value {exp!r} not in options"
                            )

        if items and abs(weight_sum - 1.0) > 0.1:
            errors.append(f"weights sum to {weight_sum:.3f}, expected ~1.0")

        if errors:
            return ValidationResult(
                "answer_sheet", False, "; ".join(errors[:5]),
            )
        return ValidationResult(
            "answer_sheet", True,
            f"{len(items)} items, weights={weight_sum:.2f}",
        )

    def print_results(self, results: list[ValidationResult]) -> bool:
        """Pretty-print validation results. Returns True if all passed."""
        all_passed = True
        for r in results:
            status = "PASS" if r.passed else "FAIL"
            icon = "✓" if r.passed else "✗"
            print(f"  {icon} [{status}] {r.name}: {r.message}")
            if not r.passed:
                all_passed = False

        if all_passed:
            print(f"\n  All {len(results)} checks passed!")
        else:
            failed = sum(1 for r in results if not r.passed)
            print(f"\n  {failed}/{len(results)} checks failed.")

        return all_passed
