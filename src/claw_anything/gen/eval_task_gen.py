"""Eval Task Generator: Phase 2 — generates evaluation tasks from seed tasks + persona.

Pipeline: sample seed → adapt to persona → generate signal fixtures → generate scoring → generate grader → assemble → validate.
"""

from __future__ import annotations

import json
import logging
import random
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from openai import OpenAI

from ..llm_logger import log_llm_call

from .activity_log_engine import ActivityLogEngine
from .assembler import TaskAssembler
from .json_parser import parse_llm_json, should_use_response_format
from .fixture_registry import FixtureRegistry
from .grader_generator import GraderGenerator
from .persona import (
    ALL_SERVICES,
    SERVICE_FIXTURE_MAP,
    SERVICE_ID_FIELD,
    SERVICE_TOOLS,
    GoldEnvironment,
    PersonaDefinition,
)
from .schema_enforcer import enforce_records
from .seed_task import SeedTask, SeedTaskLibrary
from .task_adapter import AdaptedTask, TaskAdapter
from .task_generator import TaskGenerationResult
from .validator import Severity, TaskValidator

log = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
_TEMPLATE_DIR = Path(__file__).resolve().parents[3] / "template"

_MAX_SEED_RETRIES = 5


def _load_prompt(name: str) -> str:
    return (_PROMPT_DIR / name).read_text(encoding="utf-8")


def _load_schemas() -> dict[str, Any]:
    path = _TEMPLATE_DIR / "fixture_schemas.yaml"
    with open(path, "r", encoding="utf-8") as f:
        schemas: dict[str, Any] = yaml.safe_load(f)
    gui_path = _TEMPLATE_DIR / "fixture_schemas_gui.yaml"
    if gui_path.exists():
        with open(gui_path, "r", encoding="utf-8") as f:
            gui_schemas = yaml.safe_load(f)
        if gui_schemas:
            schemas.update(gui_schemas)
    return schemas


def _format_schemas_for_prompt(schemas: dict[str, Any], services: set[str] | None = None) -> str:
    allowed = services if services else set(ALL_SERVICES)
    lines = []
    for svc in ALL_SERVICES:
        if svc not in allowed:
            continue
        schema = schemas.get(svc)
        if not schema:
            continue
        lines.append(f"\n### {svc}")
        lines.append(f"ID field: {schema['id_field']}, prefix: {schema['id_prefix']}")
        fields = schema.get("fields", {})
        for fname, fdef in fields.items():
            lines.append(f"  - {fname}: {fdef.get('type', 'string')} (e.g. {fdef.get('example', '')})")
    return "\n".join(lines)


def _format_id_rules(schemas: dict[str, Any], services: set[str] | None = None) -> str:
    allowed = services if services else set(ALL_SERVICES)
    lines = []
    for svc in ALL_SERVICES:
        if svc not in allowed:
            continue
        schema = schemas.get(svc)
        if schema:
            lines.append(f"  - {svc}: {schema['id_prefix']}XXXX (start from {schema['id_prefix']}{schema['id_start']})")
    return "\n".join(lines)


class EvalTaskGenerator:
    """Generates complete evaluation tasks from seed tasks + persona environment."""

    def __init__(
        self,
        model_id: str,
        api_key: str | None = None,
        base_url: str | None = None,
        template_filename: str = "task_template.yaml",
        allowed_services: set[str] | None = None,
    ):
        self.model_id = model_id
        self.client = OpenAI(
            api_key=api_key or "unused",
            base_url=base_url,
            max_retries=5,
            timeout=120.0,
        )
        # Whitelist of services this generator may use. Defaults to ALL_SERVICES
        # (no constraint). When the caller passes a template-derived whitelist,
        # we propagate it into the adapter and intersect _available_services
        # with it so the assembler / fixture / scoring generation never reaches
        # for an out-of-template service.
        self.allowed_services: set[str] = (
            set(allowed_services) if allowed_services else set(ALL_SERVICES)
        )
        self.adapter = TaskAdapter(
            model_id, api_key, base_url, allowed_services=self.allowed_services,
        )
        self.grader_gen = GraderGenerator(model_id, api_key, base_url)
        self.assembler = TaskAssembler(template_filename=template_filename)
        self.log_engine = ActivityLogEngine(noise_ratio=0.3)
        self.schemas = _load_schemas()
        # Populated by `generate()` so CLI can surface skipped tasks to the user.
        self.last_skipped_tasks: list[tuple[int, str]] = []

    @staticmethod
    def _ensure_str(value) -> str:
        """Coerce LLM output to string (it may return a list of steps)."""
        if isinstance(value, list):
            return "\n".join(str(item) for item in value)
        return str(value) if value is not None else ""

    def generate(
        self,
        env: GoldEnvironment,
        seed_library: SeedTaskLibrary,
        output_dir: Path,
        max_tasks: int = 3,
        use_symlinks: bool = False,
        difficulty_override: str | None = None,
        execution_date: str | None = None,
    ) -> list[Path]:
        """Generate evaluation tasks.

        Args:
            env: Gold environment with persona + fixtures.
            seed_library: Seed task library to sample from.
            output_dir: Output directory for generated tasks.
            max_tasks: Maximum number of tasks to generate.
            use_symlinks: Symlink fixtures (True) or copy (False).

        Returns:
            List of paths to generated task directories.
        """
        persona = env.persona
        registry_path = env.env_dir / "fixture_registry.json"
        registry = FixtureRegistry.load_or_create(registry_path, env.fixtures_dir, self.schemas)

        # Discover which services this persona's gold env actually has data for.
        # Services with no records are treated as "apps not installed" — their
        # blocks/tools/endpoints are stripped from every task.yaml we emit,
        # and their entries are hidden from SERVICE_TOOLS when building the
        # LLM prompt for scoring/grader generation.
        from .persona import available_services_in_env
        env_services = available_services_in_env(env.env_dir)
        # Intersect env's actual records with the template whitelist so the
        # downstream assembler / scoring / fixture generation never references
        # services the chosen template can't serve.
        self._available_services: set[str] = env_services & self.allowed_services
        log.info(
            "Persona env exposes %d services: %s (template whitelist: %s)",
            len(self._available_services),
            sorted(self._available_services),
            sorted(self.allowed_services),
        )

        # Constrain seed sampling to seeds whose required_services fit the
        # template whitelist — otherwise _sample_and_adapt will retry forever.
        seed_library.set_allowed_services(self.allowed_services)

        # Resolve execution_date: default to end of time_window
        if not execution_date:
            tw = persona.business_context.time_window
            if "~" in tw:
                execution_date = tw.split("~")[-1].strip()
            else:
                execution_date = "2026-03-31"
        log.info("Using execution_date: %s", execution_date)

        generated_dirs: list[Path] = []
        skipped_tasks: list[tuple[int, str]] = []
        used_seeds: set[str] = set()
        patrol_task_generated = False

        def _snapshot_fixtures(fixtures_dir: Path) -> dict[str, bytes]:
            """Capture raw bytes of every fixture file under env.fixtures_dir
            so a failed gen-eval task can be unwound without leaving partial
            records on disk."""
            snap: dict[str, bytes] = {}
            if not fixtures_dir.exists():
                return snap
            for p in fixtures_dir.rglob("*.json"):
                snap[str(p.relative_to(fixtures_dir))] = p.read_bytes()
            return snap

        def _restore_fixtures(fixtures_dir: Path, snap: dict[str, bytes]) -> None:
            """Restore fixture files to a previous snapshot. Files that existed
            before but are now missing are recreated; files that didn't exist
            but were created since are deleted."""
            current = {
                str(p.relative_to(fixtures_dir)): p
                for p in fixtures_dir.rglob("*.json")
            } if fixtures_dir.exists() else {}
            for rel, content in snap.items():
                target = fixtures_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            for rel, p in current.items():
                if rel not in snap:
                    p.unlink(missing_ok=True)

        for task_num in range(1, max_tasks + 1):
            log.info("=== Generating eval task %d/%d ===", task_num, max_tasks)

            # Take a checkpoint so a half-generated task can be rolled back
            # without polluting the gold env's registry + fixtures.
            registry_snapshot = registry.snapshot()
            fixtures_snapshot = _snapshot_fixtures(env.fixtures_dir)
            task_dir_for_cleanup: Path | None = None

            try:
                # 1. Sample and adapt seed task
                adapted = self._sample_and_adapt(seed_library, persona, registry, env, used_seeds)
                if adapted is None:
                    log.warning("Task %d: No compatible seed found, stopping", task_num)
                    break
                used_seeds.add(adapted.seed_id)
                if difficulty_override:
                    adapted.adapted_difficulty = difficulty_override
                log.info("Task %d: Adapted seed '%s' → '%s'", task_num, adapted.seed_id, adapted.adapted_name)

                is_patrol = adapted.interaction_mode == "patrol"

                # Patrol-specific state (populated only for patrol tasks)
                patrol_fixture_records: dict[str, list[dict]] = {}
                patrol_logs: dict = {}

                if is_patrol:
                    # Limit to at most 1 patrol task per gen-eval run
                    if patrol_task_generated:
                        log.info("Task %d: Skipping patrol (already generated 1)", task_num)
                        continue

                    # 2a. Generate fresh patrol signals at eval time
                    patrol_fixture_records, patrol_signals = self._generate_patrol_signals_for_task(
                        adapted, persona, registry, env, seed_library,
                    )
                    if not patrol_signals:
                        log.warning("Task %d: Failed to generate patrol signals, skipping", task_num)
                        continue

                    # 2b. Generate activity logs from patrol signals
                    tw_start, tw_end = self._parse_time_window(persona.business_context.time_window)
                    patrol_logs = self.log_engine.generate_patrol_logs(
                        patrol_signals=patrol_signals,
                        persona=persona,
                        time_window_start=tw_start,
                        time_window_end=tw_end,
                    )

                    new_records, signal_records = {}, {}

                    # 4. Generate scoring with fresh signals
                    scoring_result = self._generate_patrol_scoring(
                        adapted, persona, patrol_signals, env,
                        difficulty_override=difficulty_override,
                        execution_date=execution_date,
                    )
                else:
                    # 2. Generate signal fixtures for standard task
                    new_records, signal_records = self._generate_eval_fixtures(
                        adapted, persona, registry, env,
                        scenario_anchor_date=execution_date,
                    )

                    # 3. Append fixtures to env if any new records generated
                    if any(new_records.values()):
                        self._append_fixtures(new_records, env.fixtures_dir)
                        for svc, recs in new_records.items():
                            if recs:
                                registry.register_records(svc, recs)
                        registry.save(registry_path)
                        # Refresh env cache
                        env = GoldEnvironment(env.env_dir)

                    # 4. Generate scoring
                    scoring_result = self._generate_scoring(adapted, persona, signal_records, env, difficulty_override=difficulty_override, execution_date=execution_date)

                if scoring_result is None:
                    log.warning("Task %d: Scoring generation failed, rolling back", task_num)
                    raise RuntimeError("scoring_result is None")

                # 5. Build TaskGenerationResult for grader generator
                task_id = self._make_task_id(persona, task_num, adapted)
                task_result = TaskGenerationResult(
                    task_id=task_id,
                    task_name=adapted.adapted_name,
                    category=adapted.adapted_category,
                    difficulty=adapted.adapted_difficulty,
                    prompt_text=scoring_result["prompt_text"],
                    scoring_components=scoring_result.get("scoring_components", []),
                    safety_checks=scoring_result.get("safety_checks", []),
                    expected_actions=scoring_result.get("expected_actions", []),
                    judge_rubric=scoring_result.get("judge_rubric", ""),
                    reference_solution=self._ensure_str(scoring_result.get("reference_solution", "")),
                    language=persona.language,
                )

                # 6. Generate grader
                grader_code = self.grader_gen.generate(
                    env, task_result, adapted.involved_services,
                    available_services=self._available_services,
                )

                # 7. Build task.yaml
                task_yaml = self.assembler.build_task_yaml(
                    task_id=task_id,
                    task_name=adapted.adapted_name,
                    category=adapted.adapted_category,
                    difficulty=adapted.adapted_difficulty,
                    prompt_text=task_result.prompt_text,
                    scoring_components=task_result.scoring_components,
                    safety_checks=task_result.safety_checks,
                    expected_actions=task_result.expected_actions,
                    judge_rubric=task_result.judge_rubric,
                    reference_solution=task_result.reference_solution,
                    language=persona.language,
                    output_dir=output_dir,
                    execution_date=execution_date,
                    available_services=self._available_services,
                )

                # 8. Assemble task directory
                task_dir = self.assembler.assemble(
                    env=env,
                    task_id=task_id,
                    task_yaml_content=task_yaml,
                    grader_code=grader_code,
                    output_dir=output_dir,
                    use_symlinks=use_symlinks,
                )
                task_dir_for_cleanup = task_dir
                log.info("Task %d: Assembled → %s", task_num, task_dir)

                # 8b. Post-assembly: inject patrol logs and fixture anchors into task dir
                if is_patrol:
                    task_logs_dir = task_dir / "logs"
                    # Replace symlink with real copy (must not modify gold env's shared logs)
                    if task_logs_dir.is_symlink():
                        link_target = task_logs_dir.resolve()
                        task_logs_dir.unlink()
                        shutil.copytree(link_target, task_logs_dir)

                    # Append patrol-specific activity logs
                    if patrol_logs:
                        self.log_engine.append_service_logs(task_logs_dir, patrol_logs)
                        self.log_engine.rebuild_timeline(task_logs_dir)
                        log.info("Task %d: Appended patrol logs for %s", task_num, list(patrol_logs.keys()))

                    # Append fixture anchors (contacts, partial notes, etc.)
                    if patrol_fixture_records and any(patrol_fixture_records.values()):
                        task_fixtures_dir = task_dir / "fixtures"
                        if task_fixtures_dir.is_symlink():
                            link_target = task_fixtures_dir.resolve()
                            task_fixtures_dir.unlink()
                            shutil.copytree(link_target, task_fixtures_dir)
                        self._append_fixtures(patrol_fixture_records, task_fixtures_dir)

                    patrol_task_generated = True

                # 9. Validate. BLOCKING failures reject the task (delete dir,
                # don't append). WARNING failures only get logged.
                rejected = False
                try:
                    validator = TaskValidator()
                    results = validator.validate(task_dir, env)
                    blocking_failed = [
                        r for r in results
                        if not r.passed and r.severity == Severity.BLOCKING
                    ]
                    warning_failed = [
                        r for r in results
                        if not r.passed and r.severity == Severity.WARNING
                    ]
                    if blocking_failed:
                        log.error(
                            "Task %d REJECTED — blocking validation failures: %s",
                            task_num, [str(r) for r in blocking_failed],
                        )
                        if warning_failed:
                            log.warning(
                                "Task %d also has warnings: %s",
                                task_num, [str(r) for r in warning_failed],
                            )
                        shutil.rmtree(task_dir, ignore_errors=True)
                        task_dir_for_cleanup = None  # already deleted; roll back fixtures via except path
                        skipped_tasks.append((task_num, "validation blocked: " + "; ".join(str(r) for r in blocking_failed)))
                        # Roll back fixture/registry mutations since we're discarding this task.
                        registry.restore(registry_snapshot)
                        registry.save(registry_path)
                        _restore_fixtures(env.fixtures_dir, fixtures_snapshot)
                        env = GoldEnvironment(env.env_dir)
                        rejected = True
                    elif warning_failed:
                        log.warning(
                            "Task %d validation warnings (non-blocking): %s",
                            task_num, [str(r) for r in warning_failed],
                        )
                        log.info("Task %d: %d/%d validations passed", task_num,
                                 len(results) - len(warning_failed), len(results))
                    else:
                        log.info("Task %d: All %d validations passed", task_num, len(results))
                except Exception as e:
                    log.warning("Task %d: Validation error: %s", task_num, e)

                if not rejected:
                    generated_dirs.append(task_dir)

            except Exception as exc:
                # Single-task failure must not abort the whole gen-eval run.
                # Roll back fixture registry + on-disk fixtures so the next
                # task starts from the same env state we entered with, and
                # drop any half-written task directory.
                log.error(
                    "Task %d FAILED — %s: %s — rolling back and skipping",
                    task_num, type(exc).__name__, exc,
                    exc_info=True,
                )
                try:
                    registry.restore(registry_snapshot)
                    registry.save(registry_path)
                    _restore_fixtures(env.fixtures_dir, fixtures_snapshot)
                    env = GoldEnvironment(env.env_dir)
                except Exception as rollback_exc:
                    log.error("Task %d: rollback failed: %s", task_num, rollback_exc)
                if task_dir_for_cleanup is not None and task_dir_for_cleanup.exists():
                    shutil.rmtree(task_dir_for_cleanup, ignore_errors=True)
                skipped_tasks.append((task_num, f"{type(exc).__name__}: {exc}"))
                continue

        log.info("Phase 2 complete: generated %d eval tasks", len(generated_dirs))
        if skipped_tasks:
            log.warning("Phase 2 skipped %d task(s):", len(skipped_tasks))
            for n, reason in skipped_tasks:
                log.warning("  task %d: %s", n, reason[:200])
        # Expose skipped list to the caller (CLI uses it to exit non-zero).
        self.last_skipped_tasks = skipped_tasks
        return generated_dirs

    def _sample_and_adapt(
        self,
        library: SeedTaskLibrary,
        persona: PersonaDefinition,
        registry: FixtureRegistry,
        env: GoldEnvironment,
        used_seeds: set[str],
    ) -> AdaptedTask | None:
        from .seed_task import TASK_CONTENT_VALUES

        for attempt in range(_MAX_SEED_RETRIES):
            seeds = library.sample(persona, n=1, exclude_ids=used_seeds)
            if not seeds:
                return None
            seed = seeds[0]
            # patrol_setup seeds are only used internally by _generate_patrol_signals_for_task(),
            # never directly as eval tasks
            if seed.interaction_mode == "patrol_setup":
                log.info("Seed %s is patrol_setup (reserved for signal generation), skipping", seed.seed_id)
                used_seeds.add(seed.seed_id)
                continue

            # Pick content_direction inside the retry loop so a bad-fit direction
            # doesn't re-fire on every attempt.
            if seed.plausible_content_directions:
                content_direction = random.choice(seed.plausible_content_directions)
            else:
                content_direction = seed.task_content
            assert content_direction in TASK_CONTENT_VALUES, (
                f"bad content_direction '{content_direction}' for seed {seed.seed_id}"
            )
            log.info("Seed %s draw → content_direction=%s (native=%s)",
                     seed.seed_id, content_direction, seed.task_content)

            adapted = self.adapter.adapt(
                seed, persona, registry, env,
                mode=TaskAdapter.ADAPT_MODE_EVAL,
                content_direction=content_direction,
            )
            if adapted.is_compatible:
                library.mark_used(seed.seed_id)
                return adapted
            log.info("Seed %s incompatible (attempt %d/%d), retrying",
                     seed.seed_id, attempt + 1, _MAX_SEED_RETRIES)
            used_seeds.add(seed.seed_id)
        return None

    def _generate_eval_fixtures(
        self,
        adapted: AdaptedTask,
        persona: PersonaDefinition,
        registry: FixtureRegistry,
        env: GoldEnvironment,
        scenario_anchor_date: str | None = None,
    ) -> tuple[dict[str, list[dict]], dict[str, list[str]]]:
        """Generate signal + noise fixtures for the eval task."""
        template = _load_prompt("gen_incremental_fixtures.txt")

        persona_summary = (
            f"- Name: {persona.persona_name}\n"
            f"- Role: {persona.role}\n"
            f"- Company: {persona.company}\n"
            f"- Industry: {persona.industry}\n"
            f"- Seniority: {persona.seniority}"
        )

        ctx = persona.business_context
        active_issues = "\n".join(f"  - {i}" for i in ctx.active_issues) or "  (none)"

        data_threads_summary = persona.get_data_threads_summary()

        # The fixture-gen prompt template references {scenario_anchor_date} to
        # cluster dated records near the task's eval-time anchor. Fall back to
        # the end of the persona's time window if no explicit anchor.
        if not scenario_anchor_date:
            tw = ctx.time_window
            scenario_anchor_date = tw.split("~")[-1].strip() if "~" in tw else "2026-03-31"

        prompt = template.format(
            persona_summary=persona_summary,
            time_window=ctx.time_window,
            work_schedule=ctx.work_schedule or "(not specified)",
            active_issues=active_issues,
            task_name=adapted.adapted_name,
            task_description=adapted.adapted_description,
            involved_services=", ".join(adapted.involved_services),
            key_actions="\n  - ".join([""] + adapted.key_actions),
            schemas=_format_schemas_for_prompt(self.schemas, self.allowed_services),
            id_rules=_format_id_rules(self.schemas, self.allowed_services),
            next_ids=registry.get_next_ids_summary(),
            used_ids_summary=registry.get_used_ids_summary(),
            known_entities=registry.get_entities_summary(),
            data_threads_summary=data_threads_summary,
            scenario_anchor_date=scenario_anchor_date,
            allowed_services_list=", ".join(sorted(self.allowed_services)),
        )

        raw = self._call_llm(prompt)
        result = parse_llm_json(raw, default={}, source="eval_task_gen")

        records = result.get("records", {})
        signal_records = result.get("signal_records", {})

        # Validate no conflicts
        for svc, recs in records.items():
            if not recs:
                continue
            conflicts = registry.validate_no_conflicts(svc, recs)
            if conflicts:
                log.warning("ID conflicts in %s: %s — removing", svc, conflicts)
                id_field = SERVICE_ID_FIELD.get(svc, "id")
                records[svc] = [r for r in recs if r.get(id_field) not in conflicts]

        # Backfill any fields the LLM dropped, per fixture_schemas.yaml.
        records = {
            svc: enforce_records(svc, recs, self.schemas, source="eval_incremental")
            for svc, recs in records.items()
        }

        return records, signal_records

    # Service-tool catalog now lives in persona.SERVICE_TOOLS so that
    # grader_generator can import the same source of truth without circular
    # imports. Reference imported `SERVICE_TOOLS` directly (no class attr).

    DIFFICULTY_GUIDANCE: dict[str, str] = {
        "hard": (
            "prompt_text must be a minimal, outcome-only request (1-2 sentences). "
            "Do NOT mention services/tools to consult, do NOT reveal the core conflict, "
            "do NOT list sub-steps. The agent discovers everything on its own."
        ),
        "medium": (
            "prompt_text should hint at the general situation but not list services or steps (2-3 sentences). "
            "You may hint at the type of issue (e.g., 'there might be a scheduling problem') "
            "but do NOT name specific conflicting items or the resolution."
        ),
        "simple": (
            "prompt_text may provide moderate context (2-4 sentences). "
            "You may mention general areas to look at (e.g., 'check my emails and calendar') "
            "and hint at the issue. You may suggest a general approach but not every sub-step."
        ),
    }

    def _generate_scoring(
        self,
        adapted: AdaptedTask,
        persona: PersonaDefinition,
        signal_records: dict[str, list[str]],
        env: GoldEnvironment,
        difficulty_override: str | None = None,
        execution_date: str | None = None,
    ) -> dict | None:
        """Generate scoring components, safety checks, judge rubric, prompt, and reference solution."""
        template = _load_prompt("gen_eval_scoring.txt")

        persona_summary = (
            f"- Name: {persona.persona_name}\n"
            f"- Role: {persona.role}\n"
            f"- Company: {persona.company}\n"
            f"- Industry: {persona.industry}\n"
            f"- Seniority: {persona.seniority}\n"
            f"- Traits: {', '.join(persona.traits)}\n"
            f"- Responsibilities: {', '.join(persona.daily_responsibilities)}"
        )

        # Gather signal records data for the prompt
        signal_data: dict[str, list[dict]] = {}
        for svc, ids in signal_records.items():
            if not ids:
                continue
            all_recs = env.get_fixtures(svc)
            id_field = SERVICE_ID_FIELD.get(svc, "id")
            matched = [r for r in all_recs if r.get(id_field) in ids]
            if matched:
                signal_data[svc] = matched
        signal_records_json = json.dumps(signal_data, ensure_ascii=False, indent=2)

        ctx = persona.business_context

        # Build available tools by service
        tool_lines = []
        for svc in adapted.involved_services:
            # Hide services this persona does not have installed — the agent
            # will not see them at runtime, so the LLM shouldn't score against them.
            if self._available_services and svc not in self._available_services:
                continue
            tools = SERVICE_TOOLS.get(svc, [])
            tool_lines.append(f"- **{svc}**: {', '.join(tools)}")
        available_tools_by_service = "\n".join(tool_lines) if tool_lines else "(all services)"

        # Build difficulty-specific prompt guidance
        difficulty = difficulty_override or adapted.adapted_difficulty
        difficulty_prompt_guidance = self.DIFFICULTY_GUIDANCE.get(
            difficulty, self.DIFFICULTY_GUIDANCE["hard"]
        )

        from .seed_task import (
            TASK_CONTENT_DESCRIPTIONS,
            INTERACTION_MODE_DESCRIPTIONS,
        )

        prompt = template.format(
            persona_summary=persona_summary,
            can_approve=", ".join(persona.authority.can_approve),
            cannot_approve=", ".join(persona.authority.cannot_approve),
            communication_style=persona.authority.communication_style,
            quality_focus=persona.authority.quality_focus,
            work_schedule=ctx.work_schedule or "(not specified)",
            execution_date=execution_date or "2026-03-31",
            task_name=adapted.adapted_name,
            task_description=adapted.adapted_description,
            task_content=adapted.task_content,
            task_content_description=TASK_CONTENT_DESCRIPTIONS.get(
                adapted.task_content, adapted.task_content,
            ),
            interaction_mode=adapted.interaction_mode,
            interaction_mode_description=INTERACTION_MODE_DESCRIPTIONS.get(
                adapted.interaction_mode, adapted.interaction_mode,
            ),
            involved_services=", ".join(adapted.involved_services),
            key_actions="\n  - ".join([""] + adapted.key_actions),
            safety_concerns="\n  - ".join([""] + adapted.safety_concerns),
            signal_records_json=signal_records_json,
            seniority=persona.seniority,
            available_tools_by_service=available_tools_by_service,
            difficulty_prompt_guidance=difficulty_prompt_guidance,
        )

        raw = self._call_llm(prompt)
        result = parse_llm_json(raw, default={}, source="eval_task_gen")
        if not result.get("prompt_text"):
            log.error("Scoring generation returned no prompt_text")
            return None
        return result

    PATROL_DIFFICULTY_GUIDANCE: dict[str, str] = {
        "simple": (
            "prompt_text should be a system patrol trigger with moderate guidance "
            "(e.g., 'Check my recent activity patterns and let me know if there is "
            "anything I started but did not finish that might need attention.'). "
            "Focus scoring on 1-2 clear dynamic behavioral patterns from logs."
        ),
        "medium": (
            "prompt_text should be a brief patrol trigger "
            "(e.g., 'Do a status check across my workspace.'). "
            "Score multiple behavioral patterns that require cross-service correlation."
        ),
        "hard": (
            "prompt_text should be minimal ('Run periodic patrol.'). "
            "Score on subtle behavioral patterns hidden among daily noise, "
            "requiring temporal reasoning and cross-service evidence chaining."
        ),
    }

    def _generate_patrol_scoring(
        self,
        adapted: AdaptedTask,
        persona: PersonaDefinition,
        patrol_signals: list[dict],
        env: GoldEnvironment,
        difficulty_override: str | None = None,
        execution_date: str | None = None,
    ) -> dict | None:
        """Generate scoring for patrol tasks using patrol-specific prompt."""
        template = _load_prompt("gen_patrol_scoring.txt")

        persona_summary = (
            f"- Name: {persona.persona_name}\n"
            f"- Role: {persona.role}\n"
            f"- Company: {persona.company}\n"
            f"- Industry: {persona.industry}\n"
            f"- Seniority: {persona.seniority}\n"
            f"- Traits: {', '.join(persona.traits)}\n"
            f"- Responsibilities: {', '.join(persona.daily_responsibilities)}"
        )

        patrol_signals_json = json.dumps(patrol_signals, ensure_ascii=False, indent=2)
        patrol_threads_summary = "(generated at eval time for this specific task)"

        ctx = persona.business_context

        # Build available tools by service
        tool_lines = []
        for svc in adapted.involved_services:
            # Hide services this persona does not have installed — the agent
            # will not see them at runtime, so the LLM shouldn't score against them.
            if self._available_services and svc not in self._available_services:
                continue
            tools = SERVICE_TOOLS.get(svc, [])
            tool_lines.append(f"- **{svc}**: {', '.join(tools)}")
        available_tools_by_service = "\n".join(tool_lines) if tool_lines else "(all services)"

        # Difficulty guidance
        difficulty = difficulty_override or adapted.adapted_difficulty
        difficulty_prompt_guidance = self.PATROL_DIFFICULTY_GUIDANCE.get(
            difficulty, self.PATROL_DIFFICULTY_GUIDANCE["simple"]
        )

        prompt = template.format(
            persona_summary=persona_summary,
            can_approve=", ".join(persona.authority.can_approve),
            cannot_approve=", ".join(persona.authority.cannot_approve),
            communication_style=persona.authority.communication_style,
            quality_focus=persona.authority.quality_focus,
            work_schedule=ctx.work_schedule or "(not specified)",
            execution_date=execution_date or "2026-03-31",
            patrol_signals_json=patrol_signals_json,
            patrol_threads_summary=patrol_threads_summary,
            available_tools_by_service=available_tools_by_service,
            difficulty_prompt_guidance=difficulty_prompt_guidance,
        )

        raw = self._call_llm(prompt)
        result = parse_llm_json(raw, default={}, source="patrol_scoring")
        if not result.get("prompt_text"):
            log.error("Patrol scoring generation returned no prompt_text")
            return None
        return result

    def _append_fixtures(self, new_records: dict[str, list[dict]], fixtures_dir: Path) -> None:
        """Append new records to fixture files."""
        for svc in ALL_SERVICES:
            recs = new_records.get(svc, [])
            if not recs:
                continue
            rel_path = SERVICE_FIXTURE_MAP.get(svc)
            if not rel_path:
                continue
            fixture_path = fixtures_dir / rel_path

            existing = []
            if fixture_path.exists():
                with open(fixture_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)

            existing.extend(recs)
            fixture_path.parent.mkdir(parents=True, exist_ok=True)
            with open(fixture_path, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
            log.info("Appended %d records to %s (total: %d)", len(recs), fixture_path, len(existing))

    @staticmethod
    def _make_task_id(persona: PersonaDefinition, task_num: int, adapted: AdaptedTask) -> str:
        """Generate a task ID like E03_T01_product_demo_prep."""
        pid = persona.persona_id
        name = adapted.adapted_name
        # Convert to ASCII snake_case slug
        name_slug = re.sub(r"[^a-zA-Z0-9]", "_", name)
        name_slug = re.sub(r"_+", "_", name_slug).strip("_").lower()
        if not name_slug:
            name_slug = f"{adapted.adapted_category}_{adapted.adapted_difficulty}"
        name_slug = name_slug[:40].rstrip("_")
        return f"{pid}_T{task_num:02d}_{name_slug}"

    def _generate_patrol_signals_for_task(
        self,
        adapted: AdaptedTask,
        persona: PersonaDefinition,
        registry: FixtureRegistry,
        env: GoldEnvironment,
        seed_library: SeedTaskLibrary,
    ) -> tuple[dict[str, list[dict]], list[dict]]:
        """Generate fresh patrol signals at eval time.

        Internally picks a random patrol_setup seed (P004-P006), adapts it
        with ADAPT_MODE_PATROL_SETUP, and generates patrol signals + fixture anchors.

        Returns:
            (fixture_records, patrol_signals)
        """
        patrol_setup_seeds = [
            t for t in seed_library.tasks
            if t.interaction_mode == "patrol_setup"
        ]
        if not patrol_setup_seeds:
            log.warning("No patrol_setup seeds available for signal generation")
            return {}, []

        setup_seed = random.choice(patrol_setup_seeds)
        log.info("Selected patrol_setup seed '%s' for signal generation", setup_seed.seed_id)

        try:
            setup_adapted = self.adapter.adapt(
                setup_seed, persona, registry, env,
                mode=TaskAdapter.ADAPT_MODE_PATROL_SETUP,
            )
        except Exception as e:
            log.warning("Patrol setup seed '%s' adapt failed: %s", setup_seed.seed_id, e)
            return {}, []

        records, signal_records, patrol_signals = self._generate_patrol_setup_fixtures(
            setup_adapted, persona, registry, env,
        )
        return records, patrol_signals

    def _generate_patrol_setup_fixtures(
        self,
        adapted: AdaptedTask,
        persona: PersonaDefinition,
        registry: FixtureRegistry,
        env: GoldEnvironment,
    ) -> tuple[dict[str, list[dict]], dict[str, list[str]], list[dict]]:
        """Generate patrol setup data: minimal fixture anchors + patrol signal metadata.

        Returns:
            (records, signal_records, patrol_signals) where patrol_signals describes
            the behavioral log patterns to generate.
        """
        template = _load_prompt("gen_patrol_setup_fixtures.txt")

        persona_summary = (
            f"- Name: {persona.persona_name}\n"
            f"- Role: {persona.role}\n"
            f"- Company: {persona.company}\n"
            f"- Industry: {persona.industry}\n"
            f"- Seniority: {persona.seniority}"
        )

        ctx = persona.business_context
        active_issues = "\n".join(f"  - {i}" for i in ctx.active_issues) or "  (none)"

        data_threads_summary = persona.get_data_threads_summary()

        prompt = template.format(
            persona_summary=persona_summary,
            time_window=ctx.time_window,
            work_schedule=ctx.work_schedule or "(not specified)",
            active_issues=active_issues,
            task_name=adapted.adapted_name,
            task_description=adapted.adapted_description,
            involved_services=", ".join(adapted.involved_services),
            key_actions="\n  - ".join([""] + adapted.key_actions),
            schemas=_format_schemas_for_prompt(self.schemas, self._available_services),
            id_rules=_format_id_rules(self.schemas, self._available_services),
            next_ids=registry.get_next_ids_summary(),
            used_ids_summary=registry.get_used_ids_summary(),
            known_entities=registry.get_entities_summary(),
            data_threads_summary=data_threads_summary,
            allowed_services_list=", ".join(sorted(self._available_services)),
        )

        raw = self._call_llm(prompt)
        result = parse_llm_json(raw, default={}, source="patrol_setup")

        records = result.get("records", {})
        signal_records = result.get("signal_records", {})
        patrol_signals = result.get("patrol_signals", [])

        # Filter invalid service names
        records = {
            svc: recs for svc, recs in records.items()
            if svc in ALL_SERVICES and svc in self._available_services and recs
        }
        signal_records = {
            svc: ids for svc, ids in signal_records.items()
            if svc in ALL_SERVICES and svc in self._available_services and ids
        }

        # Validate no ID conflicts
        for svc, recs in records.items():
            if not recs:
                continue
            conflicts = registry.validate_no_conflicts(svc, recs)
            if conflicts:
                log.warning("Patrol setup ID conflicts in %s: %s — removing", svc, conflicts)
                id_field = SERVICE_ID_FIELD.get(svc, "id")
                records[svc] = [r for r in recs if r.get(id_field) not in conflicts]

        # Backfill any fields the LLM dropped, per fixture_schemas.yaml.
        records = {
            svc: enforce_records(svc, recs, self.schemas, source="eval_patrol")
            for svc, recs in records.items()
        }

        log.info("Patrol setup: %d fixture anchors, %d patrol signals",
                 sum(len(v) for v in records.values()), len(patrol_signals))

        return records, signal_records, patrol_signals

    @staticmethod
    def _parse_time_window(time_window: str) -> tuple[datetime, datetime]:
        """Parse time_window string like '2026-01-01 ~ 2026-03-31' into datetimes."""
        parts = time_window.split("~")
        if len(parts) != 2:
            now = datetime.now()
            return now - timedelta(days=45), now + timedelta(days=45)
        start_str = parts[0].strip()
        end_str = parts[1].strip()
        try:
            start = datetime.strptime(start_str, "%Y-%m-%d")
            end = datetime.strptime(end_str, "%Y-%m-%d")
        except ValueError:
            now = datetime.now()
            return now - timedelta(days=45), now + timedelta(days=45)
        return start, end

    def _call_llm(self, prompt: str) -> str:
        _kwargs = {
            "model": self.model_id,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.5,
            "max_tokens": 32768,
        }
        if should_use_response_format(self.model_id):
            _kwargs["response_format"] = {"type": "json_object"}
        response = self.client.chat.completions.create(**_kwargs)
        log_llm_call(
            data_id=f"gen-eval-{uuid4()}",
            model=self.model_id,
            request_kwargs=_kwargs,
            response=response,
            source="gen_eval",
        )
        return response.choices[0].message.content
