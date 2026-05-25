"""Activity Log Engine (Layer 1) — rule-based service-level operation logs.

Generates realistic, timestamped action sequences from fixture records.
Each fixture record triggers a signal action sequence (e.g. send_email for a
sent gmail message), and noise actions are randomly interleaved to simulate
idle browsing, abandoned drafts, etc.

Output: per-service Markdown log files + a merged timeline.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .action_templates import (
    SERVICE_ACTION_TEMPLATES,
    classify_record,
)
from .persona import (
    ALL_SERVICES,
    SERVICE_ID_FIELD,
    GoldEnvironment,
    PersonaDefinition,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class LogEntry:
    """A single action in the activity log."""

    timestamp: datetime
    service: str
    action: str
    record_ref: str | None = None
    duration_sec: int | None = None
    metadata: dict[str, Any] | None = None

    def to_row(self) -> str:
        """Format as a Markdown table row."""
        ts = self.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        ref = self.record_ref or "-"
        dur = f"{self.duration_sec}s" if self.duration_sec else "-"
        return f"| {ts} | {self.action} | {ref} | {dur} |"


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class ActivityLogEngine:
    """Rule-based generator for service-level activity logs."""

    def __init__(
        self,
        noise_ratio: float = 0.3,
        seed: int | None = None,
    ):
        """
        Args:
            noise_ratio: approximate fraction of total entries that are noise
                         (0.0 = no noise, 1.0 = equal noise and signal entries).
            seed: random seed for reproducibility.
        """
        self.noise_ratio = noise_ratio
        self.rng = random.Random(seed)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_for_records(
        self,
        service: str,
        new_records: list[dict],
        existing_records: list[dict],
        time_window_start: datetime,
        time_window_end: datetime,
        persona_email: str | None = None,
    ) -> list[LogEntry]:
        """Generate activity log entries for a batch of new fixture records.

        This is the main entry point called during each build_persona round.

        Args:
            service: service name (e.g. "gmail").
            new_records: newly generated fixture records for this round.
            existing_records: all previously existing records (for noise refs).
            time_window_start: earliest timestamp for generated log entries.
            time_window_end: latest timestamp for generated log entries.
            persona_email: persona's email address (for gmail classification).

        Returns:
            Sorted list of LogEntry objects.
        """
        templates = SERVICE_ACTION_TEMPLATES.get(service)
        if not templates:
            log.warning("No action templates for service: %s", service)
            return []

        signal_patterns = templates["signal"]
        noise_patterns = templates["noise"]

        # 1. Generate signal sequences for each new record
        signal_entries: list[LogEntry] = []
        for record in new_records:
            record_type = classify_record(service, record, persona_email)
            pattern = signal_patterns.get(record_type)
            if not pattern:
                # fallback to first available pattern
                pattern = next(iter(signal_patterns.values()), None)
                if not pattern:
                    continue

            id_field = SERVICE_ID_FIELD.get(service, "id")
            record_id = record.get(id_field, "")

            # Determine base timestamp from record's date field
            base_ts = self._extract_timestamp(record, time_window_start, time_window_end)
            entries = self._expand_pattern(
                service, pattern, base_ts, record_id, record,
            )
            signal_entries.extend(entries)

        # 2. Generate noise entries
        noise_entries: list[LogEntry] = []
        if noise_patterns and signal_entries and self.noise_ratio > 0:
            n_noise = max(1, int(len(signal_entries) * self.noise_ratio))
            for _ in range(n_noise):
                pattern = self.rng.choice(noise_patterns)
                # Pick a random time within the window
                noise_ts = self._random_timestamp(time_window_start, time_window_end)
                # Pick a random existing record for "random_existing" refs
                ref_record = (
                    self.rng.choice(existing_records + new_records)
                    if (existing_records or new_records)
                    else None
                )
                ref_id = None
                if ref_record:
                    id_field = SERVICE_ID_FIELD.get(service, "id")
                    ref_id = ref_record.get(id_field, "")
                entries = self._expand_pattern(
                    service, pattern, noise_ts, ref_id, ref_record,
                )
                noise_entries.extend(entries)

        # 3. Merge and sort by timestamp
        all_entries = signal_entries + noise_entries
        all_entries.sort(key=lambda e: e.timestamp)
        return all_entries

    def generate_for_round(
        self,
        new_records_by_service: dict[str, list[dict]],
        env: GoldEnvironment,
        persona: PersonaDefinition,
        time_window_start: datetime,
        time_window_end: datetime,
    ) -> dict[str, list[LogEntry]]:
        """Generate activity logs for all services in one build_persona round.

        Returns:
            Dict mapping service name -> list of new LogEntry objects.
        """
        persona_email = self._infer_persona_email(persona)
        result: dict[str, list[LogEntry]] = {}

        for service, new_records in new_records_by_service.items():
            if not new_records:
                continue
            existing = env.get_fixtures(service)
            entries = self.generate_for_records(
                service=service,
                new_records=new_records,
                existing_records=existing,
                time_window_start=time_window_start,
                time_window_end=time_window_end,
                persona_email=persona_email,
            )
            if entries:
                result[service] = entries

        return result

    def generate_patrol_logs(
        self,
        patrol_signals: list[dict],
        persona: PersonaDefinition,
        time_window_start: datetime,
        time_window_end: datetime,
    ) -> dict[str, list[LogEntry]]:
        """Generate meaningful activity log entries from patrol signal metadata.

        Unlike random noise, these create-then-delete patterns have specific
        timing that reflects genuine user intent (longer edit durations, etc.).

        Args:
            patrol_signals: list of signal dicts from patrol setup fixture generation.
            persona: persona definition (unused currently, reserved for future).
            time_window_start: earliest timestamp for generated entries.
            time_window_end: latest timestamp for generated entries.

        Returns:
            Dict mapping service name -> list of LogEntry objects.
        """
        result: dict[str, list[LogEntry]] = {}

        # Pattern templates: each maps to a sequence of (action, duration_range) tuples.
        PATROL_PATTERNS: dict[str, list[tuple[str, tuple[int, int]]]] = {
            "compose_edit_discard": [
                ("open_inbox", (1, 3)),
                ("compose_new", (1, 5)),
                ("edit_draft", (30, 300)),  # duration from signal overrides this
                ("discard_draft", (1, 3)),
            ],
            "create_fill_delete": [
                ("open_todo", (1, 3)),
                ("create_new_task", (1, 5)),
                ("fill_task_details", (15, 120)),  # duration from signal overrides this
                ("delete_task", (1, 3)),
            ],
            "create_configure_delete": [
                ("open_calendar", (1, 3)),
                ("create_new_event", (2, 5)),
                ("configure_event_details", (20, 90)),
                ("delete_event", (1, 5)),
                ("close_calendar", (1, 3)),
            ],
            "search_browse_close": [
                ("open_search", (1, 3)),
                ("search_keywords", (5, 15)),
                ("browse_search_results", (15, 45)),
                ("close_search", (1, 3)),
            ],
            "start_abandon_ticket": [
                ("open_ticket_system", (1, 3)),
                ("start_new_ticket", (3, 8)),
                ("fill_problem_description", (30, 90)),
                ("abandon_ticket_creation", (1, 3)),
                ("close_ticket_system", (1, 2)),
            ],
            "create_note_delete": [
                ("open_notes", (1, 3)),
                ("create_new_note", (2, 5)),
                ("edit_note", (10, 60)),
                ("delete_note", (1, 3)),
            ],
            "create_job_delete": [
                ("open_scheduler", (1, 3)),
                ("create_new_scheduled_job", (3, 8)),
                ("set_execution_time", (10, 30)),
                ("set_job_content", (15, 45)),
                ("delete_job", (2, 8)),
                ("close_scheduler", (1, 2)),
            ],
        }

        # Map pattern → service override (for patterns that can apply to different services)
        PATTERN_SERVICE_MAP: dict[str, str] = {
            "compose_edit_discard": "gmail",
            "create_fill_delete": "todo",
            "create_configure_delete": "calendar",
            "search_browse_close": "kb",  # default, but signal.service overrides
            "start_abandon_ticket": "helpdesk",
            "create_note_delete": "notes",
            "create_job_delete": "scheduler",
        }

        for signal in patrol_signals:
            if signal.get("type") != "dynamic":
                continue

            pattern_name = signal.get("pattern", "")
            pattern_steps = PATROL_PATTERNS.get(pattern_name)
            if not pattern_steps:
                log.warning("Unknown patrol pattern: %s", pattern_name)
                continue

            service = signal.get("service", PATTERN_SERVICE_MAP.get(pattern_name, "gmail"))

            # Use signal-specified duration if available, otherwise use template range
            override_duration = signal.get("edit_duration_sec") or signal.get("fill_duration_sec")

            base_ts = self._random_timestamp(time_window_start, time_window_end)
            entries: list[LogEntry] = []
            current_ts = base_ts

            for action, dur_range in pattern_steps:
                # Override the "main action" duration (the longest step, typically index 2)
                if override_duration and action in ("edit_draft", "fill_task_details",
                                                     "configure_event_details", "browse_search_results",
                                                     "fill_problem_description", "edit_note",
                                                     "set_job_content"):
                    duration = override_duration
                else:
                    duration = self.rng.randint(dur_range[0], dur_range[1])

                entries.append(LogEntry(
                    timestamp=current_ts,
                    service=service,
                    action=action,
                    record_ref=None,  # deleted/discarded content has no persistent ref
                    duration_sec=duration,
                ))

                gap = self.rng.randint(1, 5)
                current_ts = current_ts + timedelta(seconds=duration + gap)

            result.setdefault(service, []).extend(entries)

        # Sort each service's entries by timestamp
        for svc in result:
            result[svc].sort(key=lambda e: e.timestamp)

        return result

    # ------------------------------------------------------------------
    # Markdown output
    # ------------------------------------------------------------------

    @staticmethod
    def format_service_log(service: str, entries: list[LogEntry]) -> str:
        """Format entries for a single service as a Markdown table."""
        header = f"# {service.title()} Activity Log\n\n"
        table = "| Time | Action | Reference | Duration |\n"
        table += "|------|--------|-----------|----------|\n"
        for entry in sorted(entries, key=lambda e: e.timestamp):
            table += entry.to_row() + "\n"
        return header + table

    @staticmethod
    def iso_week_bounds(dt: datetime | date) -> tuple[date, date]:
        """Monday and Sunday of the ISO week containing ``dt``."""
        d = dt.date() if isinstance(dt, datetime) else dt
        monday = d - timedelta(days=d.weekday())
        sunday = monday + timedelta(days=6)
        return monday, sunday

    @staticmethod
    def weekly_filename(start: date, end: date) -> str:
        """Filename for a weekly timeline bucket: ``timeline_YYYYMMDD_YYYYMMDD.md``."""
        return f"timeline_{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}.md"

    @staticmethod
    def format_merged_timeline(
        all_logs: dict[str, list[LogEntry]],
    ) -> str:
        """Merge all service logs into a single chronological timeline."""
        all_entries: list[LogEntry] = []
        for entries in all_logs.values():
            all_entries.extend(entries)
        all_entries.sort(key=lambda e: e.timestamp)

        header = "# Activity Timeline\n\n"
        table = "| Time | Service | Action | Reference | Duration |\n"
        table += "|------|---------|--------|-----------|----------|\n"
        for entry in all_entries:
            ts = entry.timestamp.strftime("%Y-%m-%d %H:%M:%S")
            ref = entry.record_ref or "-"
            dur = f"{entry.duration_sec}s" if entry.duration_sec else "-"
            table += f"| {ts} | {entry.service} | {entry.action} | {ref} | {dur} |\n"
        return header + table

    # ------------------------------------------------------------------
    # File I/O
    # ------------------------------------------------------------------

    @staticmethod
    def init_logs_dir(env_dir: Path) -> Path:
        """Create the logs/ directory structure. Returns logs_dir path."""
        logs_dir = env_dir / "logs"
        services_dir = logs_dir / "services"
        services_dir.mkdir(parents=True, exist_ok=True)
        # Create empty files. Weekly timeline_*.md files are materialized by
        # rebuild_timeline() once there are real entries to bucket.
        (logs_dir / "work_journal.md").touch()
        return logs_dir

    def append_service_logs(
        self,
        logs_dir: Path,
        logs_by_service: dict[str, list[LogEntry]],
    ) -> None:
        """Append new log entries to per-service files."""
        services_dir = logs_dir / "services"
        services_dir.mkdir(parents=True, exist_ok=True)

        for service, entries in logs_by_service.items():
            if not entries:
                continue
            file_path = services_dir / f"{service}_activity.md"

            # If file is empty or doesn't exist, write with header
            if not file_path.exists() or file_path.stat().st_size == 0:
                content = self.format_service_log(service, entries)
                file_path.write_text(content, encoding="utf-8")
            else:
                # Append only the new rows (no header)
                new_rows = "\n".join(e.to_row() for e in sorted(entries, key=lambda e: e.timestamp))
                with open(file_path, "a", encoding="utf-8") as f:
                    f.write(new_rows + "\n")

    def rebuild_timeline(self, logs_dir: Path) -> None:
        """Rebuild weekly ``timeline_YYYYMMDD_YYYYMMDD.md`` files.

        Reads every per-service ``*_activity.md`` in ``logs_dir/services/``,
        re-sorts each in place, merges all entries chronologically, buckets
        them by ISO week (Monday–Sunday), and writes one file per non-empty
        bucket. Filenames reflect the actual first/last entry date inside the
        week (so the leading/trailing week is clipped when data does not span
        a full Mon–Sun range).

        Idempotent: any pre-existing ``timeline.md`` or ``timeline_*.md`` in
        ``logs_dir`` is deleted before new files are written.
        """
        services_dir = logs_dir / "services"
        if not services_dir.exists():
            return

        all_entries: list[LogEntry] = []
        for log_file in sorted(services_dir.glob("*_activity.md")):
            service = log_file.stem.replace("_activity", "")
            entries = self._parse_service_log(log_file, service)
            if entries:
                sorted_entries = sorted(entries, key=lambda e: e.timestamp)
                log_file.write_text(
                    self.format_service_log(service, sorted_entries),
                    encoding="utf-8",
                )
                all_entries.extend(sorted_entries)

        all_entries.sort(key=lambda e: e.timestamp)

        legacy = logs_dir / "timeline.md"
        if legacy.exists():
            legacy.unlink()
        for stale in logs_dir.glob("timeline_*.md"):
            stale.unlink()

        if not all_entries:
            return

        buckets: dict[date, list[LogEntry]] = {}
        for entry in all_entries:
            monday, _ = self.iso_week_bounds(entry.timestamp)
            buckets.setdefault(monday, []).append(entry)

        for monday in sorted(buckets):
            week_entries = buckets[monday]
            start = week_entries[0].timestamp.date()
            end = week_entries[-1].timestamp.date()
            filename = self.weekly_filename(start, end)
            content = self.format_merged_timeline({"all": week_entries})
            (logs_dir / filename).write_text(content, encoding="utf-8")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _expand_pattern(
        self,
        service: str,
        pattern: list[dict],
        base_ts: datetime,
        record_id: str | None,
        record: dict | None,
    ) -> list[LogEntry]:
        """Expand a pattern template into concrete LogEntry objects."""
        entries: list[LogEntry] = []
        current_ts = base_ts

        for step in pattern:
            action = step["action"]
            dur_range = step.get("duration_sec", (1, 3))
            duration = self.rng.randint(dur_range[0], dur_range[1])

            # Resolve ref
            ref = step.get("ref")
            ref_value = None
            if ref == "record_id" and record_id:
                ref_value = record_id
            elif ref == "random_existing" and record_id:
                ref_value = record_id  # will be overridden by caller for noise
            elif ref == "event_date" and record:
                ref_value = record.get("start_time", record.get("date", ""))

            entries.append(LogEntry(
                timestamp=current_ts,
                service=service,
                action=action,
                record_ref=ref_value,
                duration_sec=duration,
            ))

            # Advance timestamp by duration + small gap
            gap = self.rng.randint(1, 5)
            current_ts = current_ts + timedelta(seconds=duration + gap)

        return entries

    def _extract_timestamp(
        self,
        record: dict,
        window_start: datetime,
        window_end: datetime,
    ) -> datetime:
        """Extract a base timestamp from a fixture record."""
        for field_name in ("date", "start_time", "created_at", "published_at", "last_run"):
            val = record.get(field_name)
            if val:
                try:
                    ts = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
                    # Strip timezone info for simplicity
                    ts = ts.replace(tzinfo=None)
                    # Clamp to window
                    ws = window_start.replace(tzinfo=None) if window_start.tzinfo else window_start
                    we = window_end.replace(tzinfo=None) if window_end.tzinfo else window_end
                    if ws <= ts <= we:
                        return ts
                except (ValueError, TypeError):
                    pass
        # Fallback: random time in window
        return self._random_timestamp(window_start, window_end)

    def _random_timestamp(self, start: datetime, end: datetime) -> datetime:
        """Generate a random timestamp within a window, biased toward work hours."""
        s = start.replace(tzinfo=None) if start.tzinfo else start
        e = end.replace(tzinfo=None) if end.tzinfo else end
        delta = (e - s).total_seconds()
        if delta <= 0:
            return s

        # Try to land in work hours (8-18) — retry up to 5 times
        for _ in range(5):
            offset = self.rng.random() * delta
            ts = s + timedelta(seconds=offset)
            if 8 <= ts.hour <= 18:
                return ts

        # Give up and return any time
        offset = self.rng.random() * delta
        return s + timedelta(seconds=offset)

    @staticmethod
    def _infer_persona_email(persona: PersonaDefinition) -> str | None:
        """Try to infer persona email from persona fields."""
        name = persona.persona_name.lower().replace(" ", ".")
        company = persona.company.lower().replace(" ", "")
        return f"{name}@{company}.com"

    @staticmethod
    def _parse_service_log(file_path: Path, service: str) -> list[LogEntry]:
        """Parse a service activity log Markdown file back into LogEntry objects."""
        entries: list[LogEntry] = []
        text = file_path.read_text(encoding="utf-8")
        for line in text.strip().split("\n"):
            line = line.strip()
            if not line.startswith("|") or line.startswith("| Time") or line.startswith("|---"):
                continue
            parts = [p.strip() for p in line.split("|")]
            # parts: ['', time, action, ref, duration, '']
            if len(parts) < 5:
                continue
            try:
                ts = datetime.strptime(parts[1], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            action = parts[2]
            ref = parts[3] if parts[3] != "-" else None
            dur_str = parts[4]
            duration = None
            if dur_str != "-" and dur_str.endswith("s"):
                try:
                    duration = int(dur_str[:-1])
                except ValueError:
                    pass
            entries.append(LogEntry(
                timestamp=ts,
                service=service,
                action=action,
                record_ref=ref,
                duration_sec=duration,
            ))
        return entries
