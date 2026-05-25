"""Persona Builder: Phase 1 iterative history construction.

Builds a rich persona + fixtures by repeatedly:
1. Sampling a seed task
2. Adapting it to the persona
3. Generating incremental fixtures
3.5a. Generating Layer 1 activity logs (rule-based)
3.5b. Generating Layer 2 work journal (LLM-based)
4. Updating the persona with new data threads
"""

from __future__ import annotations

import json
import logging
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from openai import OpenAI

from ..llm_logger import log_llm_call

from .activity_log_engine import ActivityLogEngine
from .fixture_registry import FixtureRegistry
from .json_parser import parse_llm_json, should_use_response_format
from .persona import (
    ALL_SERVICES,
    SERVICE_FIXTURE_MAP,
    SERVICE_ID_FIELD,
    DataThread,
    GoldEnvironment,
    PersonaDefinition,
)
from .noise_generator import DailyNoiseGenerator
from .schema_enforcer import enforce_records
from .seed_noise import SeedNoiseLibrary
from .seed_task import SeedTask, SeedTaskLibrary
from .task_adapter import AdaptedTask, TaskAdapter

log = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
_TEMPLATE_DIR = Path(__file__).resolve().parents[3] / "template"

# Max retries when a seed task is incompatible
_MAX_SEED_RETRIES = 5


def _load_prompt(name: str) -> str:
    return (_PROMPT_DIR / name).read_text(encoding="utf-8")


def _load_schemas() -> dict[str, Any]:
    path = _TEMPLATE_DIR / "fixture_schemas.yaml"
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _format_schemas_for_prompt(
    schemas: dict[str, Any], services: set[str] | None = None,
) -> str:
    """Format schemas into a compact string for the LLM prompt.

    When ``services`` is provided, only schemas for those services are emitted
    so the LLM is not tempted to invent records for apps the persona doesn't
    have.
    """
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


def _format_id_rules(
    schemas: dict[str, Any], services: set[str] | None = None,
) -> str:
    allowed = services if services else set(ALL_SERVICES)
    lines = []
    for svc in ALL_SERVICES:
        if svc not in allowed:
            continue
        schema = schemas.get(svc)
        if schema:
            lines.append(f"  - {svc}: {schema['id_prefix']}XXXX (start from {schema['id_prefix']}{schema['id_start']})")
    return "\n".join(lines)


class PersonaBuilder:
    """Iteratively builds a rich persona with accumulated fixture data."""

    def __init__(
        self,
        model_id: str,
        api_key: str | None = None,
        base_url: str | None = None,
        allowed_services: set[str] | None = None,
    ):
        self.model_id = model_id
        self.client = OpenAI(
            api_key=api_key or "unused",
            base_url=base_url,
        )
        # Whitelist of services this builder is allowed to populate. Defaults
        # to ALL_SERVICES (no constraint) so callers that don't pass a template
        # behave exactly as before.
        self.allowed_services: set[str] = (
            set(allowed_services) if allowed_services else set(ALL_SERVICES)
        )
        self.adapter = TaskAdapter(
            model_id, api_key, base_url, allowed_services=self.allowed_services,
        )
        self.schemas = _load_schemas()
        self.log_engine = ActivityLogEngine(noise_ratio=0.3)

    def build(
        self,
        persona_path: Path,
        output_dir: Path,
        seed_library: SeedTaskLibrary | None = None,
        rounds: int = 0,
        noise_library: SeedNoiseLibrary | None = None,
        noise_rounds: int = 0,
        routines_per_day: int = 4,
    ) -> GoldEnvironment:
        """Run the iterative persona building process.

        Supports two independent modes that can be used separately or together:
        - **Task mode** (seed_library + rounds): adapts seed tasks to generate
          business-event fixtures, activity logs, and data threads.
        - **Noise mode** (noise_library + noise_rounds): generates event-independent
          daily routine noise (activity logs + optional fixture traces).

        Args:
            persona_path: Path to initial persona.yaml.
            output_dir: Output directory for gold environment.
            seed_library: Library of seed tasks (required for task mode).
            rounds: Number of task iteration rounds (0 = skip task mode).
            noise_library: Library of seed noise patterns (required for noise mode).
            noise_rounds: Number of noise iteration rounds (0 = skip noise mode).
            routines_per_day: Number of daily routine noise sessions per workday.

        Returns:
            The built GoldEnvironment.
        """
        # Initialize output structure
        output_dir.mkdir(parents=True, exist_ok=True)
        fixtures_dir = output_dir / "fixtures"
        registry_path = output_dir / "fixture_registry.json"
        persona_out_path = output_dir / "persona.yaml"

        # Copy initial persona if not already present
        if not persona_out_path.exists():
            persona = PersonaDefinition.from_yaml(persona_path)
            self._save_persona(persona, persona_out_path)
        else:
            persona = PersonaDefinition.from_yaml(persona_out_path)

        # Initialize empty fixtures if needed
        self._init_fixtures_dir(fixtures_dir)

        # Initialize logs directory
        logs_dir = self.log_engine.init_logs_dir(output_dir)

        # Load or create registry
        registry = FixtureRegistry.load_or_create(registry_path, fixtures_dir, self.schemas)

        # Build environment wrapper
        env = GoldEnvironment(output_dir)

        # ===== Mixed mode: interleave seed_task and seed_noise rounds =====
        # Rather than running every task round and *then* every noise round,
        # the two streams are interleaved into macro-rounds (e.g. 40 tasks +
        # 80 noise -> per macro-round: 1 task followed by 2 noise, x40). This
        # avoids an "all tasks then all noise" layering in fixture ID
        # allocation and persistent-data accumulation.
        do_tasks = bool(seed_library and rounds > 0)
        do_noise = bool(noise_library and noise_library.patterns and noise_rounds > 0)
        if do_tasks or do_noise:
            env, persona, registry = self._run_mixed_rounds(
                seed_library=seed_library if do_tasks else None,
                rounds=rounds if do_tasks else 0,
                noise_library=noise_library if do_noise else None,
                noise_rounds=noise_rounds if do_noise else 0,
                persona=persona,
                env=env,
                registry=registry,
                output_dir=output_dir,
                fixtures_dir=fixtures_dir,
                registry_path=registry_path,
                persona_out_path=persona_out_path,
                logs_dir=logs_dir,
                routines_per_day=routines_per_day,
            )

        log.info("Phase 1 complete: total records = %d", env.get_total_records())
        return env

    @staticmethod
    def _build_interleaved_schedule(
        task_rounds: int, noise_rounds: int
    ) -> list[tuple[str, int]]:
        """Build an interleaved schedule of task and noise rounds.

        Returns a flat list of ``("task", idx)`` / ``("noise", idx)`` tuples.
        Noise rounds are distributed as evenly as possible across the task
        slots and emitted *after* their owning task round, so a macro-round
        runs one task followed by its share of noise (e.g. 40 tasks + 80 noise
        -> per macro-round: 1 task + 2 noise).

        Degenerate cases:
        - ``task_rounds == 0`` -> all noise rounds in order.
        - ``noise_rounds == 0`` -> all task rounds in order.
        """
        if task_rounds <= 0:
            return [("noise", i) for i in range(max(0, noise_rounds))]
        if noise_rounds <= 0:
            return [("task", i) for i in range(task_rounds)]

        base, extra = divmod(noise_rounds, task_rounds)
        schedule: list[tuple[str, int]] = []
        noise_idx = 0
        for t in range(task_rounds):
            schedule.append(("task", t))
            # First `extra` task slots get one additional noise round so the
            # remainder is spread out rather than dumped at the end.
            n_here = base + (1 if t < extra else 0)
            for _ in range(n_here):
                schedule.append(("noise", noise_idx))
                noise_idx += 1
        return schedule

    def _run_mixed_rounds(
        self,
        seed_library: SeedTaskLibrary | None,
        rounds: int,
        noise_library: SeedNoiseLibrary | None,
        noise_rounds: int,
        persona: PersonaDefinition,
        env: GoldEnvironment,
        registry: FixtureRegistry,
        output_dir: Path,
        fixtures_dir: Path,
        registry_path: Path,
        persona_out_path: Path,
        logs_dir: Path,
        routines_per_day: int,
    ) -> tuple[GoldEnvironment, PersonaDefinition, FixtureRegistry]:
        """Run task and noise rounds interleaved in a single mixed loop.

        Task and noise rounds are woven together via
        :meth:`_build_interleaved_schedule`. Single-mode builds (only tasks or
        only noise) fall out naturally from the schedule's degenerate cases.
        """
        # ----- Task-side setup -----
        used_seeds: set[str] = set()
        anchor_dates: list[str] = []
        if seed_library and rounds > 0:
            # Pre-sample per-round scenario anchor dates spread across the full
            # time window. Without this, the LLM defaults to placing every
            # "recent/upcoming/this week" scenario near the window end, which
            # piles historical events into the last week.
            tw_start, tw_end = self._parse_time_window(persona.business_context.time_window)
            anchor_dates = self._sample_anchor_dates(tw_start, tw_end, rounds)
        else:
            rounds = 0

        # ----- Noise-side setup -----
        noise_gen: DailyNoiseGenerator | None = None
        day_batches: list[list[datetime]] = []
        rng = random.Random()
        if noise_library and noise_library.patterns and noise_rounds > 0:
            tw_start, tw_end = self._parse_time_window(persona.business_context.time_window)
            work_schedule = (
                persona.business_context.work_schedule
                if persona.business_context
                else None
            )
            noise_gen = DailyNoiseGenerator(
                model_id=self.model_id,
                allowed_services=self.allowed_services,
                api_key=self.client.api_key,
                base_url=str(self.client.base_url) if self.client.base_url else None,
            )
            # Split workdays evenly across noise rounds.
            day_batches = noise_gen.split_workdays_into_rounds(
                time_window_start=tw_start,
                time_window_end=tw_end,
                noise_rounds=noise_rounds,
                work_schedule=work_schedule,
            )
            if not day_batches:
                log.warning("No workdays found in time window for noise generation")

        # split_workdays_into_rounds may return fewer batches than requested
        # (e.g. too few workdays), so use the actual count for the schedule.
        actual_noise = len(day_batches)

        # ----- Interleaved schedule -----
        schedule = self._build_interleaved_schedule(rounds, actual_noise)
        log.info(
            "=== Mixed build: %d task rounds + %d noise rounds interleaved ===",
            rounds, actual_noise,
        )

        for kind, idx in schedule:
            if kind == "task":
                env, persona, registry = self._run_one_task_round(
                    r=idx + 1,
                    total_rounds=rounds,
                    scenario_anchor_date=anchor_dates[idx],
                    seed_library=seed_library,
                    persona=persona,
                    env=env,
                    registry=registry,
                    output_dir=output_dir,
                    fixtures_dir=fixtures_dir,
                    registry_path=registry_path,
                    persona_out_path=persona_out_path,
                    logs_dir=logs_dir,
                    used_seeds=used_seeds,
                )
            else:
                env, persona, registry = self._run_one_noise_round(
                    r=idx + 1,
                    total_rounds=actual_noise,
                    noise_gen=noise_gen,
                    noise_library=noise_library,
                    batch_days=day_batches[idx],
                    persona=persona,
                    env=env,
                    registry=registry,
                    output_dir=output_dir,
                    fixtures_dir=fixtures_dir,
                    registry_path=registry_path,
                    logs_dir=logs_dir,
                    routines_per_day=routines_per_day,
                    rng=rng,
                )

        log.info(
            "Mixed build complete: %d task rounds + %d noise rounds",
            rounds, actual_noise,
        )
        return env, persona, registry

    def _run_one_task_round(
        self,
        r: int,
        total_rounds: int,
        scenario_anchor_date: str,
        seed_library: SeedTaskLibrary,
        persona: PersonaDefinition,
        env: GoldEnvironment,
        registry: FixtureRegistry,
        output_dir: Path,
        fixtures_dir: Path,
        registry_path: Path,
        persona_out_path: Path,
        logs_dir: Path,
        used_seeds: set[str],
    ) -> tuple[GoldEnvironment, PersonaDefinition, FixtureRegistry]:
        """Run a single task-based build round (seed_task mode).

        Returns the (possibly unchanged) env/persona/registry. When the round
        is skipped — incompatible seed, patrol_setup, or no fixtures — the
        inputs are returned unchanged so the surrounding interleaved loop can
        still run the noise rounds that follow this task slot.
        """
        log.info("=== Task Round %d/%d ===", r, total_rounds)

        # 1. Sample a compatible seed task
        adapted = self._sample_and_adapt(seed_library, persona, registry, env, used_seeds)
        if adapted is None:
            log.warning("Task round %d: No compatible seed found, skipping", r)
            return env, persona, registry
        used_seeds.add(adapted.seed_id)

        log.info(
            "Task round %d: Adapted seed '%s' → '%s' (anchor=%s)",
            r, adapted.seed_id, adapted.adapted_name, scenario_anchor_date,
        )

        # Skip patrol_setup seeds — patrol signal planting is deferred to gen-eval
        if adapted.interaction_mode == "patrol_setup":
            log.info("Task round %d: Skipping patrol_setup seed '%s' (deferred to gen-eval)", r, adapted.seed_id)
            return env, persona, registry

        # Generate normal incremental fixtures
        new_records, signal_records = self._generate_incremental_fixtures(
            adapted, persona, registry, env,
            scenario_anchor_date=scenario_anchor_date,
        )

        if not any(new_records.values()):
            log.warning("Task round %d: No fixtures generated, skipping", r)
            return env, persona, registry

        # 3. Append fixtures to files
        if any(new_records.values()):
            self._append_fixtures(new_records, fixtures_dir)

        # 4. Register new records
        for svc, recs in new_records.items():
            if recs:
                registry.register_records(svc, recs)

        # 5. Generate activity logs (Layer 1 + Layer 2)
        tw_start, tw_end = self._parse_time_window(persona.business_context.time_window)

        logs_by_service = self.log_engine.generate_for_round(
            new_records_by_service=new_records,
            env=env,
            persona=persona,
            time_window_start=tw_start,
            time_window_end=tw_end,
        )
        if logs_by_service:
            self.log_engine.append_service_logs(logs_dir, logs_by_service)
            self.log_engine.rebuild_timeline(logs_dir)
            log.info("Task round %d: Generated Layer 1 activity logs for %s",
                     r, list(logs_by_service.keys()))

        # 5b. Generate work journal entry (Layer 2)
        self._generate_work_journal_entry(
            adapted, persona, new_records, signal_records,
            logs_by_service, logs_dir,
        )

        # 6. Update persona
        persona = self._update_persona(
            persona, adapted, new_records, signal_records, persona_out_path,
        )

        # 7. Save registry
        registry.save(registry_path)

        # 8. Refresh environment cache
        env = GoldEnvironment(output_dir)

        total = env.get_total_records()
        log.info("Task round %d complete: total records = %d", r, total)

        return env, persona, registry

    def _run_one_noise_round(
        self,
        r: int,
        total_rounds: int,
        noise_gen: DailyNoiseGenerator,
        noise_library: SeedNoiseLibrary,
        batch_days: list[datetime],
        persona: PersonaDefinition,
        env: GoldEnvironment,
        registry: FixtureRegistry,
        output_dir: Path,
        fixtures_dir: Path,
        registry_path: Path,
        logs_dir: Path,
        routines_per_day: int,
        rng: random.Random,
    ) -> tuple[GoldEnvironment, PersonaDefinition, FixtureRegistry]:
        """Run a single noise-based build round (seed_noise mode).

        Noise generation does not modify the persona; it is returned unchanged
        for a uniform call signature with :meth:`_run_one_task_round`.
        """
        log.info(
            "Noise round %d/%d: %d workdays (%s ~ %s)",
            r, total_rounds, len(batch_days),
            batch_days[0].strftime("%Y-%m-%d"),
            batch_days[-1].strftime("%Y-%m-%d"),
        )

        daily_logs, trace_records = noise_gen.generate_round(
            persona=persona,
            registry=registry,
            noise_library=noise_library,
            batch_days=batch_days,
            routines_per_day=routines_per_day,
            rng=rng,
        )

        # Append trace fixture records
        if trace_records:
            self._append_fixtures(trace_records, fixtures_dir)
            # Note: registry already updated inside noise_gen._parse_sessions

        # Append activity logs to per-service files and rebuild timeline
        if daily_logs:
            self.log_engine.append_service_logs(logs_dir, daily_logs)
            self.log_engine.rebuild_timeline(logs_dir)

        total_entries = sum(len(v) for v in daily_logs.values())
        total_traces = sum(len(v) for v in trace_records.values())
        log.info(
            "Noise round %d complete: %d log entries, %d trace records",
            r, total_entries, total_traces,
        )

        # Save registry
        registry.save(registry_path)

        # Refresh environment cache
        env = GoldEnvironment(output_dir)

        return env, persona, registry

    def _sample_and_adapt(
        self,
        library: SeedTaskLibrary,
        persona: PersonaDefinition,
        registry: FixtureRegistry,
        env: GoldEnvironment | None,
        used_seeds: set[str],
    ) -> AdaptedTask | None:
        """Sample a seed task and adapt it. Retry on incompatibility."""
        for attempt in range(_MAX_SEED_RETRIES):
            seeds = library.sample(persona, n=1, exclude_ids=used_seeds)
            if not seeds:
                return None
            seed = seeds[0]
            # Use patrol_setup mode for patrol setup seeds
            if seed.interaction_mode == "patrol_setup":
                adapt_mode = TaskAdapter.ADAPT_MODE_PATROL_SETUP
            else:
                adapt_mode = TaskAdapter.ADAPT_MODE_HISTORY
            try:
                adapted = self.adapter.adapt(
                    seed, persona, registry, env,
                    mode=adapt_mode,
                )
            except Exception as e:
                log.warning("Seed %s adapt failed (%s), trying another", seed.seed_id, e)
                used_seeds.add(seed.seed_id)
                continue
            if adapted.is_compatible:
                library.mark_used(seed.seed_id)
                return adapted
            log.info("Seed %s incompatible (attempt %d/%d), trying another",
                     seed.seed_id, attempt + 1, _MAX_SEED_RETRIES)
            used_seeds.add(seed.seed_id)  # Skip this one
        return None

    def _generate_incremental_fixtures(
        self,
        adapted: AdaptedTask,
        persona: PersonaDefinition,
        registry: FixtureRegistry,
        env: GoldEnvironment | None,
        scenario_anchor_date: str | None = None,
    ) -> tuple[dict[str, list[dict]], dict[str, list[str]]]:
        """Generate incremental fixture data for an adapted task.

        scenario_anchor_date anchors relative-date references ("recent",
        "upcoming", "this week") in the adapted task so that events cluster
        around that date rather than the end of the time window.
        """
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

        if not scenario_anchor_date:
            tw_start, tw_end = self._parse_time_window(ctx.time_window)
            scenario_anchor_date = self._sample_anchor_dates(tw_start, tw_end, 1)[0]

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
        result = parse_llm_json(raw, default={}, source="persona_builder")

        records = result.get("records", {})
        signal_records = result.get("signal_records", {})

        # Filter out invalid service names (must be a known service AND in the
        # current template whitelist).
        records = {
            svc: recs for svc, recs in records.items()
            if svc in ALL_SERVICES and svc in self.allowed_services and recs
        }
        signal_records = {
            svc: ids for svc, ids in signal_records.items()
            if svc in ALL_SERVICES and svc in self.allowed_services and ids
        }

        # Validate no ID conflicts
        for svc, recs in records.items():
            if not recs:
                continue
            conflicts = registry.validate_no_conflicts(svc, recs)
            if conflicts:
                log.warning("ID conflicts in %s: %s — removing conflicting records", svc, conflicts)
                id_field = SERVICE_ID_FIELD.get(svc, "id")
                records[svc] = [r for r in recs if r.get(id_field) not in conflicts]

        # Backfill any fields the LLM dropped, per fixture_schemas.yaml.
        records = {
            svc: enforce_records(svc, recs, self.schemas, source="persona_incremental")
            for svc, recs in records.items()
        }

        return records, signal_records

    def _generate_patrol_setup_fixtures(
        self,
        adapted: AdaptedTask,
        persona: PersonaDefinition,
        registry: FixtureRegistry,
        env: GoldEnvironment | None,
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
            schemas=_format_schemas_for_prompt(self.schemas, self.allowed_services),
            id_rules=_format_id_rules(self.schemas, self.allowed_services),
            next_ids=registry.get_next_ids_summary(),
            used_ids_summary=registry.get_used_ids_summary(),
            known_entities=registry.get_entities_summary(),
            data_threads_summary=data_threads_summary,
            allowed_services_list=", ".join(sorted(self.allowed_services)),
        )

        raw = self._call_llm(prompt)
        result = parse_llm_json(raw, default={}, source="patrol_setup")

        records = result.get("records", {})
        signal_records = result.get("signal_records", {})
        patrol_signals = result.get("patrol_signals", [])

        # Filter invalid service names (must be known + in template whitelist).
        records = {
            svc: recs for svc, recs in records.items()
            if svc in ALL_SERVICES and svc in self.allowed_services and recs
        }
        signal_records = {
            svc: ids for svc, ids in signal_records.items()
            if svc in ALL_SERVICES and svc in self.allowed_services and ids
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
            svc: enforce_records(svc, recs, self.schemas, source="persona_patrol")
            for svc, recs in records.items()
        }

        log.info("Patrol setup: %d fixture anchors, %d patrol signals",
                 sum(len(v) for v in records.values()), len(patrol_signals))

        return records, signal_records, patrol_signals

    def _append_fixtures(self, new_records: dict[str, list[dict]], fixtures_dir: Path) -> None:
        """Append new records to existing fixture JSON files."""
        for svc in ALL_SERVICES:
            recs = new_records.get(svc, [])
            if not recs:
                continue
            if svc not in self.allowed_services:
                log.warning(
                    "Skipping append for service %s — not in template whitelist (%d records dropped)",
                    svc, len(recs),
                )
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

            with open(fixture_path, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)

            log.info("Appended %d records to %s (total: %d)", len(recs), fixture_path, len(existing))

    def _update_persona(
        self,
        persona: PersonaDefinition,
        adapted: AdaptedTask,
        new_records: dict[str, list[dict]],
        signal_records: dict[str, list[str]],
        persona_path: Path,
        patrol_signals: list[dict] | None = None,
    ) -> PersonaDefinition:
        """Update persona with new data thread from this round."""
        # Build the new data thread
        thread_name = adapted.persona_updates.get("new_data_thread_name", adapted.adapted_name)
        thread_desc = adapted.persona_updates.get("new_data_thread_description", adapted.adapted_description)

        # Build involved_records from signal_records
        involved_records: dict[str, list[str]] = {}
        for svc, ids in signal_records.items():
            if ids:
                involved_records[svc] = [str(i) for i in ids]

        # If signal_records is empty, infer from generated records
        if not involved_records:
            for svc, recs in new_records.items():
                if recs:
                    id_field = SERVICE_ID_FIELD.get(svc, "id")
                    ids = [str(r[id_field]) for r in recs if id_field in r]
                    if ids:
                        involved_records[svc] = ids

        is_patrol_setup = adapted.interaction_mode == "patrol_setup"

        new_thread = DataThread(
            name=thread_name,
            description=thread_desc,
            involved_services=adapted.involved_services,
            involved_records=involved_records,
            difficulty=adapted.adapted_difficulty,
            category=adapted.adapted_category,
            thread_type="patrol_setup" if is_patrol_setup else "standard",
            patrol_signals=patrol_signals or [],
        )

        # Add to persona
        persona.data_threads.append(new_thread)

        # Add new active issue if provided
        new_issue = adapted.persona_updates.get("new_active_issue", "")
        if new_issue:
            persona.business_context.active_issues.append(new_issue)

        # Save updated persona
        self._save_persona(persona, persona_path)
        return persona

    def _init_fixtures_dir(self, fixtures_dir: Path) -> None:
        """Create empty fixture files for all services if not present."""
        for svc in ALL_SERVICES:
            rel_path = SERVICE_FIXTURE_MAP.get(svc)
            if not rel_path:
                continue
            fixture_path = fixtures_dir / rel_path
            fixture_path.parent.mkdir(parents=True, exist_ok=True)
            if not fixture_path.exists():
                with open(fixture_path, "w", encoding="utf-8") as f:
                    json.dump([], f)

    @staticmethod
    def _save_persona(persona: PersonaDefinition, path: Path) -> None:
        """Save persona to YAML."""
        data = persona.model_dump(exclude_none=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False, width=120)
        log.info("Saved persona to %s", path)

    def _generate_work_journal_entry(
        self,
        adapted: AdaptedTask,
        persona: PersonaDefinition,
        new_records: dict[str, list[dict]],
        signal_records: dict[str, list[str]],
        logs_by_service: dict,
        logs_dir: Path,
        is_patrol_setup: bool = False,
        patrol_signals: list[dict] | None = None,
    ) -> None:
        """Generate a Layer 2 work journal entry via LLM."""
        template = _load_prompt("gen_work_journal.txt")

        persona_summary = (
            f"- Name: {persona.persona_name}\n"
            f"- Role: {persona.role}\n"
            f"- Company: {persona.company}\n"
            f"- Industry: {persona.industry}\n"
            f"- Seniority: {persona.seniority}"
        )

        ctx = persona.business_context
        active_issues = "\n".join(f"  - {i}" for i in ctx.active_issues) or "  (none)"

        # Build records summary
        records_lines = []
        for svc, recs in new_records.items():
            if not recs:
                continue
            id_field = SERVICE_ID_FIELD.get(svc, "id")
            ids = [r.get(id_field, "?") for r in recs]
            records_lines.append(f"- {svc}: {', '.join(str(i) for i in ids)}")
        records_summary = "\n".join(records_lines) or "(no records)"

        # Build activity log summary (Layer 1 for this round)
        activity_lines = []
        for svc, entries in logs_by_service.items():
            if not entries:
                continue
            actions = [e.action for e in entries[:10]]  # limit for token efficiency
            activity_lines.append(f"- {svc}: {' → '.join(actions)}")
        activity_log_summary = "\n".join(activity_lines) or "(no activity log)"

        # For patrol setup, augment the task description with hesitation/abandonment context
        task_description = adapted.adapted_description
        if is_patrol_setup and patrol_signals:
            intents = [s.get("inferred_intent", "") for s in patrol_signals if s.get("type") == "dynamic"]
            if intents:
                task_description += (
                    "\n\nIMPORTANT NARRATIVE CONTEXT: This was an ABANDONED/HESITATION event. "
                    "The user started these actions but did NOT complete them. "
                    "The journal entry should subtly reflect the user's hesitation and the "
                    "reasons they stopped. Inferred intents:\n"
                    + "\n".join(f"  - {intent}" for intent in intents)
                )

        # Read existing journal for continuity
        journal_path = logs_dir / "work_journal.md"
        existing_journal = ""
        if journal_path.exists():
            existing_journal = journal_path.read_text(encoding="utf-8").strip()
        if not existing_journal:
            existing_journal = "(no existing entries)"

        prompt_text = template.format(
            persona_summary=persona_summary,
            time_window=ctx.time_window,
            work_schedule=ctx.work_schedule or "(not specified)",
            active_issues=active_issues,
            task_name=adapted.adapted_name,
            task_description=task_description,
            involved_services=", ".join(adapted.involved_services),
            key_actions="\n  - ".join([""] + adapted.key_actions),
            records_summary=records_summary,
            activity_log_summary=activity_log_summary,
            existing_journal=existing_journal,
            language=persona.language,
        )

        raw = self._call_llm(prompt_text)

        # Clean up: strip code fences if the LLM wrapped it
        entry = raw.strip()
        if entry.startswith("```"):
            lines = entry.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            entry = "\n".join(lines).strip()

        # Append to journal
        with open(journal_path, "a", encoding="utf-8") as f:
            if existing_journal and existing_journal != "(no existing entries)":
                f.write("\n\n---\n\n")
            f.write(entry + "\n")

        log.info("Generated Layer 2 work journal entry for '%s'", adapted.adapted_name)

    @staticmethod
    def _sample_anchor_dates(
        tw_start: datetime, tw_end: datetime, n: int
    ) -> list[str]:
        """Sample n scenario anchor dates spread across [tw_start, tw_end].

        Each round's anchor is uniformly sampled within its own equal-sized
        bucket of the window so coverage is enforced even for small n, then
        jittered. Dates are returned as YYYY-MM-DD strings.
        """
        if n <= 0:
            return []
        total_days = max(1, (tw_end - tw_start).days)
        bucket = total_days / n
        dates: list[str] = []
        for i in range(n):
            low = int(i * bucket)
            high = max(low, int((i + 1) * bucket) - 1)
            offset = random.randint(low, min(high, total_days))
            dates.append((tw_start + timedelta(days=offset)).strftime("%Y-%m-%d"))
        random.shuffle(dates)
        return dates

    @staticmethod
    def _parse_time_window(time_window: str) -> tuple[datetime, datetime]:
        """Parse time_window string like '2026-01-01 ~ 2026-03-31' into datetimes."""
        from datetime import timedelta

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

    def _call_llm(self, prompt: str, max_retries: int = 2) -> str:
        for attempt in range(max_retries):
            _kwargs = {
                "model": self.model_id,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.7 + attempt * 0.1,
                "max_tokens": 32768,
            }
            if should_use_response_format(self.model_id):
                _kwargs["response_format"] = {"type": "json_object"}
            response = self.client.chat.completions.create(**_kwargs)
            log_llm_call(
                data_id=f"gen-persona-{uuid4()}",
                model=self.model_id,
                request_kwargs=_kwargs,
                response=response,
                source="gen_persona",
            )
            raw = response.choices[0].message.content
            # Retry if model returns empty or trivial JSON
            if raw and raw.strip() not in ("", "{}", "null"):
                return raw
            log.warning("LLM returned empty/trivial response (attempt %d/%d)", attempt + 1, max_retries)
        return raw
