"""Daily Noise Generator: LLM-driven event-independent routine noise.

Generates realistic daily routine activities (browsing inbox, checking
calendar, jotting then deleting a note, etc.) spread across the persona's
time window.  Produces both activity log entries and optional fixture trace
records for noise that leaves persistent data.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from openai import OpenAI

from ..llm_logger import log_llm_call

from .activity_log_engine import LogEntry
from .fixture_registry import FixtureRegistry
from .json_parser import parse_llm_json, should_use_response_format
from .persona import (
    ALL_SERVICES,
    SERVICE_ID_FIELD,
    GoldEnvironment,
    PersonaDefinition,
)
from .schema_enforcer import enforce_record
from .seed_noise import SeedNoise, SeedNoiseLibrary

log = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
_TEMPLATE_DIR = Path(__file__).resolve().parents[3] / "template"

# How many workdays to batch in a single LLM call
_DAYS_PER_BATCH = 7


def _load_prompt(name: str) -> str:
    return (_PROMPT_DIR / name).read_text(encoding="utf-8")


def _load_schemas() -> dict[str, Any]:
    path = _TEMPLATE_DIR / "fixture_schemas.yaml"
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _format_trace_schemas(schemas: dict[str, Any], trace_services: set[str]) -> str:
    """Format schemas only for services that have trace-leaving noise."""
    if not trace_services:
        return "(no trace-leaving noise in this batch)"
    lines = []
    for svc in sorted(trace_services):
        schema = schemas.get(svc)
        if not schema:
            continue
        lines.append(f"\n### {svc}")
        lines.append(f"ID field: {schema['id_field']}, prefix: {schema['id_prefix']}")
        fields = schema.get("fields", {})
        for fname, fdef in fields.items():
            lines.append(f"  - {fname}: {fdef.get('type', 'string')} (e.g. {fdef.get('example', '')})")
    return "\n".join(lines) if lines else "(no schemas needed)"


def _enumerate_workdays(
    start: datetime,
    end: datetime,
    work_schedule: str | None = None,
) -> list[datetime]:
    """Return list of workday dates between start and end (inclusive).

    Parses work_schedule for day-of-week info; defaults to Mon-Fri.
    """
    # Determine working days of week (0=Monday, 6=Sunday)
    work_dow = {0, 1, 2, 3, 4}  # default Mon-Fri

    if work_schedule:
        schedule_lower = work_schedule.lower()
        if "mon-sat" in schedule_lower or "monday-saturday" in schedule_lower:
            work_dow = {0, 1, 2, 3, 4, 5}
        elif "every day" in schedule_lower or "7 days" in schedule_lower:
            work_dow = {0, 1, 2, 3, 4, 5, 6}

    days = []
    current = start.replace(hour=0, minute=0, second=0, microsecond=0)
    end_date = end.replace(hour=0, minute=0, second=0, microsecond=0)
    while current <= end_date:
        if current.weekday() in work_dow:
            days.append(current)
        current += timedelta(days=1)
    return days


def _format_seed_noise_for_prompt(seeds: list[SeedNoise]) -> str:
    """Format a list of seed noise patterns for the LLM prompt."""
    lines = []
    for s in seeds:
        lines.append(f"### {s.noise_id}: {s.name}")
        lines.append(f"- Description: {s.description}")
        lines.append(f"- Services: {', '.join(s.involved_services)}")
        lines.append(f"- Leaves trace: {s.leaves_trace}")
        if s.leaves_trace:
            lines.append(f"- Trace type: {s.trace_type}")
        lines.append(f"- Action sequence: {' → '.join(s.action_sequence)}")
        lines.append(f"- Time preference: {s.time_preference}")
        lines.append("")
    return "\n".join(lines)


class DailyNoiseGenerator:
    """LLM-driven generator for event-independent daily routine noise."""

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
        self.schemas = _load_schemas()
        # Whitelist of services this generator may emit. Defaults to ALL_SERVICES
        # (no constraint), matching pre-template behavior.
        self.allowed_services: set[str] = (
            set(allowed_services) if allowed_services else set(ALL_SERVICES)
        )

    def generate_round(
        self,
        persona: PersonaDefinition,
        registry: FixtureRegistry,
        noise_library: SeedNoiseLibrary,
        batch_days: list[datetime],
        routines_per_day: int = 4,
        rng: random.Random | None = None,
    ) -> tuple[dict[str, list[LogEntry]], dict[str, list[dict]]]:
        """Generate daily noise for a single batch of workdays (one round).

        Args:
            persona: Target persona.
            registry: Fixture registry for ID allocation.
            noise_library: Library of seed noise patterns.
            batch_days: List of workday datetimes to generate noise for.
            routines_per_day: Number of noise sessions per workday.
            rng: Random instance for reproducibility.

        Returns:
            (logs_by_service, trace_records_by_service)
            - logs_by_service: service -> list of LogEntry
            - trace_records_by_service: service -> list of fixture record dicts
        """
        if not batch_days:
            return {}, {}

        rng = rng or random.Random()

        # Sample seed noise for this batch — ensure service coverage
        seeds_per_day: dict[str, list[SeedNoise]] = {}
        for day in batch_days:
            day_str = day.strftime("%Y-%m-%d")
            sampled = noise_library.sample_with_coverage(
                n=routines_per_day,
                primary_services=persona.primary_services,
                rng=rng,
            )
            seeds_per_day[day_str] = sampled

        # Collect all unique seeds for this batch
        all_seeds_in_batch: list[SeedNoise] = []
        seen_ids: set[str] = set()
        for seeds in seeds_per_day.values():
            for s in seeds:
                if s.noise_id not in seen_ids:
                    all_seeds_in_batch.append(s)
                    seen_ids.add(s.noise_id)

        # Identify trace-leaving services (intersect with the template
        # whitelist so we don't show schemas for forbidden apps).
        trace_services: set[str] = set()
        for s in all_seeds_in_batch:
            if s.leaves_trace:
                trace_services.update(s.involved_services)
        trace_services &= self.allowed_services

        # Build prompt
        prompt = self._build_prompt(
            persona=persona,
            registry=registry,
            batch_days=batch_days,
            seeds_per_day=seeds_per_day,
            all_seeds=all_seeds_in_batch,
            trace_services=trace_services,
            routines_per_day=routines_per_day,
        )

        # Call LLM
        raw = self._call_llm(prompt)
        result = parse_llm_json(raw, default={}, source="noise_generator")

        # Parse result
        sessions = result.get("noise_sessions", [])
        batch_logs, batch_traces = self._parse_sessions(sessions, registry)

        # Sort logs by timestamp
        for svc in batch_logs:
            batch_logs[svc].sort(key=lambda e: e.timestamp)

        return batch_logs, batch_traces

    def split_workdays_into_rounds(
        self,
        time_window_start: datetime,
        time_window_end: datetime,
        noise_rounds: int,
        work_schedule: str | None = None,
    ) -> list[list[datetime]]:
        """Split the full time window's workdays into noise_rounds batches.

        Each round gets an approximately equal chunk of workdays.

        Args:
            time_window_start: Start of the time window.
            time_window_end: End of the time window.
            noise_rounds: Number of noise rounds.
            work_schedule: Work schedule string (e.g. "9am-6pm Mon-Fri").

        Returns:
            List of lists, each containing workday datetimes for one round.
        """
        workdays = _enumerate_workdays(time_window_start, time_window_end, work_schedule)
        if not workdays:
            return []

        # Distribute workdays evenly across rounds
        batches: list[list[datetime]] = []
        chunk_size = max(1, len(workdays) // noise_rounds)
        for i in range(noise_rounds):
            start_idx = i * chunk_size
            if i == noise_rounds - 1:
                # Last round gets all remaining days
                batch = workdays[start_idx:]
            else:
                batch = workdays[start_idx : start_idx + chunk_size]
            if batch:
                batches.append(batch)
        return batches

    def _build_prompt(
        self,
        persona: PersonaDefinition,
        registry: FixtureRegistry,
        batch_days: list[datetime],
        seeds_per_day: dict[str, list[SeedNoise]],
        all_seeds: list[SeedNoise],
        trace_services: set[str],
        routines_per_day: int,
    ) -> str:
        """Build the LLM prompt for a batch of days."""
        template = _load_prompt("gen_daily_noise.txt")

        persona_summary = (
            f"- Name: {persona.persona_name}\n"
            f"- Role: {persona.role}\n"
            f"- Company: {persona.company}\n"
            f"- Industry: {persona.industry}\n"
            f"- Seniority: {persona.seniority}"
        )

        ctx = persona.business_context

        # Format target days with their assigned seeds
        target_days_lines = []
        for day in batch_days:
            day_str = day.strftime("%Y-%m-%d")
            dow = day.strftime("%A")
            seeds = seeds_per_day.get(day_str, [])
            seed_ids = ", ".join(s.noise_id for s in seeds)
            target_days_lines.append(f"- {day_str} ({dow}): generate sessions for {seed_ids}")
        target_days = "\n".join(target_days_lines)

        return template.format(
            persona_summary=persona_summary,
            time_window=ctx.time_window,
            work_schedule=ctx.work_schedule or "(not specified)",
            target_days=target_days,
            seed_noise_examples=_format_seed_noise_for_prompt(all_seeds),
            trace_schemas=_format_trace_schemas(self.schemas, trace_services),
            next_ids=registry.get_next_ids_summary(),
            used_ids_summary=registry.get_used_ids_summary(),
            routines_per_day=routines_per_day,
            allowed_services_list=", ".join(sorted(self.allowed_services)),
        )

    def _parse_sessions(
        self,
        sessions: list[dict],
        registry: FixtureRegistry,
    ) -> tuple[dict[str, list[LogEntry]], dict[str, list[dict]]]:
        """Parse LLM output sessions into LogEntry objects and trace records."""
        logs_by_service: dict[str, list[LogEntry]] = {}
        traces_by_service: dict[str, list[dict]] = {}

        for session in sessions:
            noise_id = session.get("noise_id", "")
            timestamp_str = session.get("timestamp", "")

            try:
                base_ts = datetime.fromisoformat(
                    timestamp_str.replace("Z", "+00:00")
                ).replace(tzinfo=None)
            except (ValueError, TypeError):
                log.warning("Invalid timestamp in noise session: %s", timestamp_str)
                continue

            # Parse activity log entries
            activity_log = session.get("activity_log", [])
            current_ts = base_ts
            for step in activity_log:
                service = step.get("service", "")
                if service not in ALL_SERVICES or service not in self.allowed_services:
                    continue
                action = step.get("action", "")
                duration = step.get("duration_sec", 1)
                if not isinstance(duration, int):
                    try:
                        duration = int(duration)
                    except (ValueError, TypeError):
                        duration = 1

                entry = LogEntry(
                    timestamp=current_ts,
                    service=service,
                    action=action,
                    record_ref=None,
                    duration_sec=duration,
                )
                logs_by_service.setdefault(service, []).append(entry)

                # Advance timestamp
                current_ts = current_ts + timedelta(seconds=duration + 1)

            # Parse trace record
            trace = session.get("trace_record")
            if trace and isinstance(trace, dict):
                svc = trace.get("service", "")
                record = trace.get("record", {})
                if svc in ALL_SERVICES and svc in self.allowed_services and record:
                    # Validate no ID conflict
                    id_field = SERVICE_ID_FIELD.get(svc, "id")
                    record_id = record.get(id_field, "")
                    conflicts = registry.validate_no_conflicts(svc, [record])
                    if conflicts:
                        log.warning(
                            "Noise trace ID conflict in %s: %s, skipping",
                            svc, conflicts,
                        )
                    else:
                        # Backfill any fields the LLM dropped before registering.
                        filled, missing = enforce_record(svc, record, self.schemas)
                        if missing:
                            log.warning(
                                "schema_enforcer[noise_trace/%s]: record %s missing %s; backfilled defaults",
                                svc, record_id or "<no-id>", missing,
                            )
                        # Register the ID immediately to prevent future conflicts
                        registry.register_records(svc, [filled])
                        traces_by_service.setdefault(svc, []).extend([filled])

        return logs_by_service, traces_by_service

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
                data_id=f"gen-noise-{uuid4()}",
                model=self.model_id,
                request_kwargs=_kwargs,
                response=response,
                source="gen_daily_noise",
            )
            raw = response.choices[0].message.content
            if raw and raw.strip() not in ("", "{}", "null"):
                return raw
            log.warning(
                "LLM returned empty/trivial response (attempt %d/%d)",
                attempt + 1, max_retries,
            )
        return raw
