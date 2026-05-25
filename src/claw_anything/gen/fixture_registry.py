"""Fixture Registry: tracks used IDs and entities across incremental fixture generation."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .persona import ALL_SERVICES, SERVICE_FIXTURE_MAP, SERVICE_ID_FIELD

log = logging.getLogger(__name__)


class ServiceRegistry:
    """Tracks IDs and key entities for a single service."""

    def __init__(self, id_prefix: str, id_start: int):
        self.id_prefix: str = id_prefix
        self.next_id: int = id_start
        self.used_ids: list[str] = []
        self.entities: dict[str, list[str]] = {}

    def allocate_id(self) -> str:
        """Allocate the next available ID."""
        new_id = f"{self.id_prefix}{self.next_id}"
        self.used_ids.append(new_id)
        self.next_id += 1
        return new_id

    def register_id(self, record_id) -> None:
        """Register an existing ID (from LLM-generated data)."""
        record_id = str(record_id)
        if record_id in self.used_ids:
            log.warning("Duplicate ID registered: %s", record_id)
            return
        self.used_ids.append(record_id)
        # Update next_id if needed
        num_str = record_id.replace(self.id_prefix, "")
        try:
            num = int(num_str)
            self.next_id = max(self.next_id, num + 1)
        except ValueError:
            pass

    def add_entity(self, entity_type: str, value: str) -> None:
        """Track a named entity (e.g., company name, person name)."""
        if entity_type not in self.entities:
            self.entities[entity_type] = []
        if value not in self.entities[entity_type]:
            self.entities[entity_type].append(value)

    def has_id(self, record_id: str) -> bool:
        return record_id in self.used_ids

    def to_dict(self) -> dict:
        return {
            "id_prefix": self.id_prefix,
            "next_id": self.next_id,
            "used_ids": self.used_ids,
            "entities": self.entities,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ServiceRegistry:
        reg = cls(
            id_prefix=data.get("id_prefix", ""),
            id_start=data.get("next_id", 1),
        )
        reg.used_ids = data.get("used_ids", [])
        reg.entities = data.get("entities", {})
        return reg


# Entity extraction rules per service: field_name -> entity_type
_ENTITY_RULES: dict[str, dict[str, str]] = {
    "gmail": {"from": "senders", "to": "recipients", "subject": "subjects"},
    "crm": {"name": "companies", "contact_person": "people"},
    "contacts": {"name": "people", "email": "emails"},
    "inventory": {"name": "products", "supplier": "suppliers"},
    "helpdesk": {"reporter": "people", "assignee": "assignees"},
    "finance": {"merchant": "merchants"},
    "calendar": {"title": "event_titles"},
    "todo": {"title": "task_titles"},
    "kb": {"title": "article_titles"},
    "notes": {"title": "note_titles"},
    "rss": {"title": "article_titles", "source": "sources"},
    "scheduler": {"name": "job_names"},
    "config": {"name": "integration_names"},
    "workmail": {"from": "senders", "to": "recipients", "subject": "subjects"},
    "claw_notion": {"title": "page_titles"},
    "claw_obsidian": {"title": "note_titles", "folder": "folders"},
    "meeting": {"topic": "meeting_topics", "host": "meeting_hosts"},
    "claw_zotero": {"title": "paper_titles", "venue": "venues", "citation_key": "citation_keys"},
    "stumail": {"from": "senders", "to": "recipients", "subject": "subjects"},
    "claw_feishu": {"name": "group_names"},
    "claw_slack": {"name": "channel_names"},
    "claw_facebook": {"author": "people", "content": "post_contents"},
    "claw_zhihu": {"title": "question_titles"},
    "claw_wechat": {"name": "chat_names"},
    "claw_qq": {"name": "chat_names"},
    "claw_linkedin": {"company": "companies", "title": "job_titles", "location": "locations"},
    "sheet": {"name": "workbook_names"},
    "slide": {"name": "deck_names"},
    # Android GUI apps
    "gmail_clone_gui":      {"from": "senders", "to": "recipients", "subject": "subjects"},
    "fossify_calendar_gui": {"title": "event_titles", "location": "locations"},
    "fossify_messages_gui": {"contact_name": "people"},
    "fossify_notes_gui":    {"title": "note_titles"},
    "contacts_gui":         {"name": "people", "company": "companies"},
    "mastodon_gui":         {"author": "authors", "display_name": "people"},
    "mattermost_gui":       {"channel_name": "channels", "author": "people"},
    "testmall_gui":         {"name": "products", "category": "categories"},
    "odf_reader_gui":       {"title": "document_titles"},
    "photo_gallery_gui":    {"album": "albums"},
    "opentracks_gui":       {"name": "track_names", "activity_type": "activity_types"},
    "loop_habit_gui":       {"name": "habit_names"},
    "my_expenses_gui":      {"label": "account_labels"},
    "markor_gui":           {"filename": "filenames", "folder": "folders"},
    "dialer_gui":           {"contact_name": "people"},
    "maps_gui":             {"name": "place_names", "category": "categories"},
}


class FixtureRegistry:
    """Central registry tracking all fixture IDs and entities across services."""

    def __init__(self):
        self.services: dict[str, ServiceRegistry] = {}

    @classmethod
    def create_empty(cls, schemas: dict[str, Any]) -> FixtureRegistry:
        """Create an empty registry initialized from fixture schemas."""
        reg = cls()
        for svc in ALL_SERVICES:
            schema = schemas.get(svc, {})
            reg.services[svc] = ServiceRegistry(
                id_prefix=schema.get("id_prefix", ""),
                id_start=schema.get("id_start", 1),
            )
        return reg

    @classmethod
    def from_fixtures(cls, fixtures_dir: Path, schemas: dict[str, Any]) -> FixtureRegistry:
        """Build a registry by scanning existing fixture files."""
        reg = cls.create_empty(schemas)
        for svc in ALL_SERVICES:
            rel_path = SERVICE_FIXTURE_MAP.get(svc)
            if not rel_path:
                continue
            fixture_path = fixtures_dir / rel_path
            if not fixture_path.exists():
                continue
            with open(fixture_path, "r", encoding="utf-8") as f:
                records = json.load(f)

            id_field = SERVICE_ID_FIELD.get(svc, "id")
            entity_rules = _ENTITY_RULES.get(svc, {})

            for record in records:
                # Register ID
                rid = record.get(id_field, "")
                if rid:
                    reg.services[svc].register_id(rid)

                # Extract entities
                for field_name, entity_type in entity_rules.items():
                    val = record.get(field_name)
                    if val and isinstance(val, str) and val.strip():
                        reg.services[svc].add_entity(entity_type, val.strip())

        total_ids = sum(len(s.used_ids) for s in reg.services.values())
        log.info("Built registry from fixtures: %d total IDs across %d services", total_ids, len(reg.services))
        return reg

    def register_records(self, service: str, records: list[dict]) -> None:
        """Register a batch of new records for a service."""
        if service not in self.services:
            log.warning("Unknown service: %s", service)
            return

        svc_reg = self.services[service]
        id_field = SERVICE_ID_FIELD.get(service, "id")
        entity_rules = _ENTITY_RULES.get(service, {})

        for record in records:
            rid = record.get(id_field, "")
            if rid:
                svc_reg.register_id(rid)
            for field_name, entity_type in entity_rules.items():
                val = record.get(field_name)
                if val and isinstance(val, str) and val.strip():
                    svc_reg.add_entity(entity_type, val.strip())

    def validate_no_conflicts(self, service: str, records: list[dict]) -> list[str]:
        """Check for ID conflicts. Returns list of conflicting IDs."""
        if service not in self.services:
            return []
        svc_reg = self.services[service]
        id_field = SERVICE_ID_FIELD.get(service, "id")
        conflicts = []
        for record in records:
            rid = record.get(id_field, "")
            if rid and svc_reg.has_id(rid):
                conflicts.append(rid)
        return conflicts

    def get_next_ids_summary(self) -> str:
        """Format next available IDs for LLM prompt inclusion."""
        lines = []
        for svc in ALL_SERVICES:
            if svc in self.services:
                reg = self.services[svc]
                lines.append(f"  - {svc}: starting from {reg.id_prefix}{reg.next_id}")
        return "\n".join(lines)

    def get_entities_summary(self) -> str:
        """Format known entities for LLM prompt inclusion (to maintain consistency)."""
        lines = []
        for svc in ALL_SERVICES:
            if svc not in self.services:
                continue
            reg = self.services[svc]
            if not reg.entities:
                continue
            lines.append(f"\n### {svc}")
            for etype, values in reg.entities.items():
                display = values[:10]  # Limit to 10 for token efficiency
                lines.append(f"  {etype}: {', '.join(display)}")
        return "\n".join(lines) if lines else "(no known entities)"

    def get_used_ids_summary(self) -> str:
        """Format used IDs for LLM prompt inclusion."""
        lines = []
        for svc in ALL_SERVICES:
            if svc not in self.services:
                continue
            reg = self.services[svc]
            if reg.used_ids:
                lines.append(f"  {svc}: {', '.join(reg.used_ids)}")
        return "\n".join(lines) if lines else "(no used IDs)"

    def snapshot(self) -> dict:
        """Serialize current registry state. Pair with `restore()` to roll
        back appends performed since the snapshot — used by gen-eval to
        unwind a half-generated task when an LLM call fails."""
        return {svc: reg.to_dict() for svc, reg in self.services.items()}

    def restore(self, snapshot: dict) -> None:
        """Restore registry state captured by `snapshot()`. Replaces every
        service in-place; services added since the snapshot are dropped."""
        self.services = {
            svc: ServiceRegistry.from_dict(data) for svc, data in snapshot.items()
        }

    def save(self, path: Path) -> None:
        """Persist registry to JSON file."""
        data = {svc: reg.to_dict() for svc, reg in self.services.items()}
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        log.info("Saved fixture registry to %s", path)

    @classmethod
    def load(cls, path: Path) -> FixtureRegistry:
        """Load registry from JSON file."""
        reg = cls()
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for svc, svc_data in data.items():
            reg.services[svc] = ServiceRegistry.from_dict(svc_data)
        log.info("Loaded fixture registry from %s", path)
        return reg

    @classmethod
    def load_or_create(cls, registry_path: Path, fixtures_dir: Path, schemas: dict[str, Any]) -> FixtureRegistry:
        """Load existing registry or create from fixtures."""
        if registry_path.exists():
            return cls.load(registry_path)
        if fixtures_dir.exists():
            return cls.from_fixtures(fixtures_dir, schemas)
        return cls.create_empty(schemas)
