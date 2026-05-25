"""Persona definition and Gold Base Environment models."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# CLI fixture-driven services + Android GUI apps
ALL_SERVICES = [
    "gmail", "calendar", "contacts", "todo", "kb", "inventory",
    "helpdesk", "crm", "finance", "scheduler", "rss", "config", "notes",
    "workmail", "meeting", "claw_notion", "claw_obsidian", "claw_zotero",
    "stumail", "claw_feishu", "claw_slack",
    "messages",
    "claw_facebook", "claw_zhihu", "claw_wechat", "claw_qq", "claw_linkedin",
    "claw_wechat_moments", "claw_qq_zone",
    "sheet", "slide",
    # Android GUI apps — served by GUI harness (external, no mock process)
    "gmail_clone_gui", "fossify_calendar_gui", "fossify_messages_gui",
    "fossify_notes_gui", "contacts_gui", "mastodon_gui", "mattermost_gui",
    "testmall_gui", "odf_reader_gui", "photo_gallery_gui",
    "opentracks_gui", "loop_habit_gui", "my_expenses_gui", "markor_gui",
    "dialer_gui", "clock_gui", "maps_gui", "camera_gui", "settings_gui",
]

# Service name -> fixture subpath
SERVICE_FIXTURE_MAP = {
    "gmail": "gmail/inbox.json",
    "calendar": "calendar/events.json",
    "contacts": "contacts/contacts.json",
    "todo": "todo/tasks.json",
    "kb": "kb/articles.json",
    "inventory": "inventory/products.json",
    "helpdesk": "helpdesk/tickets.json",
    "crm": "crm/customers.json",
    "finance": "finance/transactions.json",
    "scheduler": "scheduler/jobs.json",
    "rss": "rss/articles.json",
    "config": "config/integrations.json",
    "notes": "notes/notes.json",
    "workmail": "workmail/inbox.json",
    "meeting": "meeting/meetings.json",
    "claw_notion": "claw_notion/pages.json",
    "claw_obsidian": "claw_obsidian/vault.json",
    "claw_zotero": "claw_zotero/library.json",
    "stumail": "stumail/inbox.json",
    "claw_feishu": "claw_feishu/workspace.json",
    "claw_slack": "claw_slack/workspace.json",
    "messages": "messages/threads.json",
    "claw_facebook": "claw_facebook/posts.json",
    "claw_zhihu": "claw_zhihu/questions.json",
    "claw_wechat": "claw_wechat/chats.json",
    "claw_qq": "claw_qq/chats.json",
    "claw_wechat_moments": "claw_wechat/moments.json",
    "claw_qq_zone": "claw_qq/zone.json",
    "claw_linkedin": "claw_linkedin/jobs.json",
    "sheet": "sheet/workbooks.json",
    "slide": "slide/decks.json",
    # Android GUI apps
    "gmail_clone_gui":        "gmail_clone_gui/emails.json",
    "fossify_calendar_gui":   "fossify_calendar_gui/events.json",
    "fossify_messages_gui":   "fossify_messages_gui/threads.json",
    "fossify_notes_gui":      "fossify_notes_gui/notes.json",
    "contacts_gui":           "contacts_gui/contacts.json",
    "mastodon_gui":           "mastodon_gui/posts.json",
    "mattermost_gui":         "mattermost_gui/messages.json",
    "testmall_gui":           "testmall_gui/products.json",
    "odf_reader_gui":         "odf_reader_gui/documents.json",
    "photo_gallery_gui":      "photo_gallery_gui/media.json",
    "opentracks_gui":         "opentracks_gui/tracks.json",
    "loop_habit_gui":         "loop_habit_gui/habits.json",
    "my_expenses_gui":        "my_expenses_gui/data.json",
    "markor_gui":             "markor_gui/notes.json",
    "dialer_gui":             "dialer_gui/calls.json",
    "clock_gui":              "clock_gui/alarms.json",
    "maps_gui":               "maps_gui/places.json",
    "camera_gui":             "camera_gui/captures.json",
    "settings_gui":           "settings_gui/settings.json",
}

# Some services have additional fixture files alongside the primary one tracked
# in SERVICE_FIXTURE_MAP. The assembler emits them in environment.fixtures and
# write_fixtures() initializes them as empty `[]` so the server can boot.
SERVICE_AUX_FIXTURES: dict[str, list[str]] = {
    "claw_qq": ["claw_qq/zone.json"],
    "claw_wechat": ["claw_wechat/moments.json"],
}

# Service -> primary ID field name (for record lookup)
SERVICE_ID_FIELD = {
    "gmail": "message_id",
    "calendar": "event_id",
    "contacts": "contact_id",
    "todo": "task_id",
    "kb": "article_id",
    "inventory": "product_id",
    "helpdesk": "ticket_id",
    "crm": "customer_id",
    "finance": "transaction_id",
    "scheduler": "job_id",
    "rss": "article_id",
    "config": "integration_id",
    "notes": "note_id",
    "workmail": "message_id",
    "meeting": "meeting_id",
    "claw_notion": "page_id",
    "claw_obsidian": "note_id",
    "claw_zotero": "item_id",
    "stumail": "message_id",
    "claw_feishu": "group_id",
    "claw_slack": "channel_id",
    "messages": "thread_id",
    "claw_facebook": "post_id",
    "claw_zhihu": "question_id",
    "claw_wechat": "chat_id",
    "claw_qq": "chat_id",
    "claw_linkedin": "job_id",
    "sheet": "workbook_id",
    "slide": "deck_id",
    # Android GUI apps
    "gmail_clone_gui":        "message_id",
    "fossify_calendar_gui":   "event_id",
    "fossify_messages_gui":   "thread_id",
    "fossify_notes_gui":      "note_id",
    "contacts_gui":           "contact_id",
    "mastodon_gui":           "post_id",
    "mattermost_gui":         "message_id",
    "testmall_gui":           "product_id",
    "odf_reader_gui":         "document_id",
    "photo_gallery_gui":      "media_id",
    "opentracks_gui":         "track_id",
    "loop_habit_gui":         "name",
    "my_expenses_gui":        "label",
    "markor_gui":             "filename",
    "dialer_gui":             "call_id",
    "clock_gui":              "alarm_id",
    "maps_gui":               "place_id",
    "camera_gui":             "capture_id",
    "settings_gui":           "setting_key",
}

# Service -> human-readable summary fields (for token-efficient LLM input)
SERVICE_SUMMARY_FIELDS = {
    "gmail": ["message_id", "from", "subject", "date", "labels", "is_read"],
    "calendar": ["event_id", "title", "start_time", "attendees"],
    "contacts": ["contact_id", "name", "department", "title"],
    "todo": ["task_id", "title", "status", "priority", "due_date"],
    "kb": ["article_id", "title", "category", "tags"],
    "inventory": ["product_id", "name", "category", "stock"],
    "helpdesk": ["ticket_id", "title", "priority", "status", "assignee"],
    "crm": ["customer_id", "name", "tier", "annual_revenue", "status"],
    "finance": ["transaction_id", "date", "amount", "category", "description"],
    "scheduler": ["job_id", "name", "enabled", "last_status"],
    "rss": ["article_id", "title", "source", "category"],
    "config": ["integration_id", "name", "status", "service"],
    "notes": ["note_id", "title", "created_at", "participants"],
    "workmail": ["message_id", "from", "subject", "date", "labels", "is_read"],
    "meeting": ["meeting_id", "topic", "host", "start_time", "duration_minutes", "status"],
    "claw_notion": ["page_id", "title", "parent_page_id", "database_id", "archived"],
    "claw_obsidian": ["note_id", "title", "folder", "tags", "last_updated"],
    "claw_zotero": ["item_id", "item_type", "title", "authors", "year", "venue", "tags", "citation_key"],
    "stumail": ["message_id", "from", "subject", "date", "labels", "is_read"],
    "claw_feishu": ["group_id", "name", "members", "owner", "archived", "last_updated"],
    "claw_slack": ["channel_id", "name", "is_private", "topic", "members", "archived", "last_updated"],
    "messages": ["thread_id", "participants", "last_message", "unread_count", "pinned", "muted"],
    "claw_facebook": ["post_id", "author", "content", "tags", "liked", "saved", "reposted", "timestamp"],
    "claw_zhihu": ["question_id", "title", "topic", "answer_count", "follow_count", "created_at"],
    "claw_wechat": ["chat_id", "name", "type", "last_message", "unread_count", "muted"],
    "claw_qq": ["chat_id", "name", "type", "last_message", "unread_count", "muted"],
    "claw_linkedin": ["job_id", "title", "company", "location", "experience_level", "job_type", "saved", "applied"],
    "sheet": ["workbook_id", "name", "owner", "sheets", "last_modified"],
    "slide": ["deck_id", "name", "owner", "slide_count", "last_modified"],
    # Android GUI apps
    "gmail_clone_gui":      ["message_id", "from", "subject", "date", "labels", "is_read"],
    "fossify_calendar_gui": ["event_id", "title", "start_time", "end_time"],
    "fossify_messages_gui": ["thread_id", "contact_name", "last_message", "unread_count"],
    "fossify_notes_gui":    ["note_id", "title", "updated_at", "tags"],
    "contacts_gui":         ["contact_id", "name", "phone", "email", "company"],
    "mastodon_gui":         ["post_id", "author", "content", "created_at", "visibility"],
    "mattermost_gui":       ["message_id", "channel_name", "author", "text", "created_at"],
    "testmall_gui":         ["product_id", "name", "category", "price", "status"],
    "odf_reader_gui":       ["document_id", "title", "file_type", "page_count"],
    "photo_gallery_gui":    ["media_id", "album", "media_type", "capture_time"],
    "opentracks_gui":       ["track_id", "name", "activity_type", "start_time", "distance_meters"],
    "loop_habit_gui":       ["name", "type", "frequency", "archived"],
    "my_expenses_gui":      ["label", "currency", "type", "opening_balance"],
    "markor_gui":           ["filename", "folder"],
    "dialer_gui":           ["call_id", "contact_name", "call_type", "timestamp"],
    "clock_gui":            ["alarm_id", "time", "label", "enabled"],
    "maps_gui":             ["place_id", "name", "address", "category"],
    "camera_gui":           ["capture_id", "path", "capture_time", "mode"],
    "settings_gui":         ["setting_key", "display_name", "category", "current_value"],
}

# Service -> tool names exposed to the agent at runtime (must match
# template/task_template.yaml's `tools:` and `tool_endpoints:` sections).
# Used by both gen_eval_scoring (per-task tool catalog for the LLM) and
# generate_grader (full available-tool catalog filtered by available_services).
SERVICE_TOOLS: dict[str, list[str]] = {
    "gmail": ["gmail_list_messages", "gmail_get_message", "gmail_send_message", "gmail_save_draft"],
    "calendar": ["calendar_list_events", "calendar_get_event", "calendar_get_user_events", "calendar_create_event", "calendar_delete_event"],
    "todo": ["todo_list_tasks", "todo_create_task", "todo_update_task", "todo_delete_task"],
    "contacts": ["contacts_search", "contacts_get", "contacts_send_message"],
    "finance": ["finance_list_transactions", "finance_get_transaction", "finance_report_submit"],
    "notes": ["notes_list", "notes_get"],
    "kb": ["kb_search", "kb_get_article", "kb_update_article"],
    "helpdesk": ["helpdesk_list_tickets", "helpdesk_get_ticket", "helpdesk_update_ticket", "helpdesk_close_ticket"],
    "inventory": ["inventory_list_products", "inventory_get_product", "inventory_create_order"],
    "rss": ["rss_list_feeds", "rss_list_articles", "rss_get_article", "rss_publish"],
    "crm": ["crm_list_customers", "crm_get_customer"],
    "config": ["config_list_integrations", "config_get_integration"],
    "scheduler": ["scheduler_list_jobs", "scheduler_get_job", "scheduler_create_job", "scheduler_update_job", "scheduler_delete_job", "scheduler_job_history"],
    "workmail": ["workmail_list_messages", "workmail_get_message", "workmail_send_message", "workmail_save_draft"],
    "claw_notion": ["claw_notion_list_pages", "claw_notion_get_page", "claw_notion_create_page", "claw_notion_update_page_properties", "claw_notion_append_blocks", "claw_notion_archive_page", "claw_notion_search", "claw_notion_query_database"],
    "claw_obsidian": ["claw_obsidian_list_notes", "claw_obsidian_get_note", "claw_obsidian_create_note", "claw_obsidian_update_note", "claw_obsidian_append_to_note", "claw_obsidian_delete_note", "claw_obsidian_search", "claw_obsidian_add_link", "claw_obsidian_remove_link", "claw_obsidian_get_backlinks"],
    "meeting": ["meeting_list", "meeting_get", "meeting_create", "meeting_update", "meeting_cancel", "meeting_start", "meeting_end", "meeting_add_participant", "meeting_remove_participant", "meeting_list_recordings"],
    "claw_zotero": ["claw_zotero_list_items", "claw_zotero_get_item", "claw_zotero_search", "claw_zotero_list_collections", "claw_zotero_add_item", "claw_zotero_update_item", "claw_zotero_add_note", "claw_zotero_add_to_collection", "claw_zotero_delete_item", "claw_zotero_remove_from_collection"],
    "stumail": ["stumail_list_messages", "stumail_get_message", "stumail_send_message", "stumail_save_draft"],
    "claw_feishu": ["claw_feishu_list_groups", "claw_feishu_get_group", "claw_feishu_send_message", "claw_feishu_search_messages", "claw_feishu_schedule_meeting", "claw_feishu_end_meeting", "claw_feishu_list_docs", "claw_feishu_create_doc", "claw_feishu_update_doc"],
    "claw_slack": ["claw_slack_list_channels", "claw_slack_get_channel", "claw_slack_list_messages", "claw_slack_get_thread", "claw_slack_post_message", "claw_slack_search_messages", "claw_slack_add_reaction", "claw_slack_remove_reaction", "claw_slack_pin_message", "claw_slack_unpin_message"],
    "messages": ["messages_list_threads", "messages_get_thread", "messages_send_message", "messages_mark_thread"],
    "claw_facebook": ["claw_facebook_browse", "claw_facebook_get_post", "claw_facebook_search", "claw_facebook_like", "claw_facebook_save", "claw_facebook_repost"],
    "claw_zhihu": ["claw_zhihu_search_questions", "claw_zhihu_get_question", "claw_zhihu_upvote_answer", "claw_zhihu_collect_answer", "claw_zhihu_follow_question", "claw_zhihu_post_answer"],
    "claw_wechat": ["claw_wechat_list_chats", "claw_wechat_get_chat", "claw_wechat_search_messages", "claw_wechat_send_message", "claw_wechat_recall_message", "claw_wechat_mute_chat", "claw_wechat_browse_moments", "claw_wechat_search_moments", "claw_wechat_like_moment", "claw_wechat_comment_moment", "claw_wechat_post_moment"],
    "claw_qq": ["claw_qq_list_chats", "claw_qq_get_chat", "claw_qq_search_messages", "claw_qq_send_message", "claw_qq_recall_message", "claw_qq_mute_chat", "claw_qq_browse_zone", "claw_qq_search_zone", "claw_qq_like_zone_post", "claw_qq_comment_zone_post", "claw_qq_post_zone"],
    "claw_linkedin": ["claw_linkedin_browse", "claw_linkedin_get_job", "claw_linkedin_search", "claw_linkedin_save", "claw_linkedin_apply", "claw_linkedin_follow_company"],
    "sheet": ["sheet_list_workbooks", "sheet_open", "sheet_get_range", "sheet_set_cell", "sheet_set_range", "sheet_insert_row", "sheet_create_workbook", "sheet_add_sheet", "sheet_rename_sheet", "sheet_compute", "sheet_delete_workbook", "sheet_delete_sheet"],
    "slide": ["slide_list_decks", "slide_open", "slide_get_slide", "slide_set_slide_content", "slide_add_element", "slide_update_element", "slide_add_slide", "slide_duplicate_slide", "slide_reorder_slide", "slide_create_deck", "slide_delete_deck", "slide_delete_slide"],
    # Android GUI apps — tool names are <service>_<action>
    "gmail_clone_gui":      ["gmail_clone_gui_list_messages", "gmail_clone_gui_get_message", "gmail_clone_gui_send_message", "gmail_clone_gui_update_message"],
    "fossify_calendar_gui": ["fossify_calendar_gui_list_events", "fossify_calendar_gui_get_event", "fossify_calendar_gui_create_event", "fossify_calendar_gui_update_event"],
    "fossify_messages_gui": ["fossify_messages_gui_list_threads", "fossify_messages_gui_get_thread", "fossify_messages_gui_send_message", "fossify_messages_gui_mark_thread"],
    "fossify_notes_gui":    ["fossify_notes_gui_list_notes", "fossify_notes_gui_get_note", "fossify_notes_gui_create_note", "fossify_notes_gui_update_note"],
    "contacts_gui":         ["contacts_gui_list", "contacts_gui_create", "contacts_gui_update"],
    "mastodon_gui":         ["mastodon_gui_browse", "mastodon_gui_get_post", "mastodon_gui_post", "mastodon_gui_favourite", "mastodon_gui_reblog"],
    "mattermost_gui":       ["mattermost_gui_list_channels", "mattermost_gui_get_channel_messages", "mattermost_gui_send_message"],
    "testmall_gui":         ["testmall_gui_list_products", "testmall_gui_get_product", "testmall_gui_search_products", "testmall_gui_add_to_cart"],
    "odf_reader_gui":       ["odf_reader_gui_list_documents", "odf_reader_gui_open_document", "odf_reader_gui_get_page"],
    "photo_gallery_gui":    ["photo_gallery_gui_list_media", "photo_gallery_gui_get_media", "photo_gallery_gui_share_media", "photo_gallery_gui_delete_media"],
    "opentracks_gui":       ["opentracks_gui_list_tracks", "opentracks_gui_get_track", "opentracks_gui_add_waypoint", "opentracks_gui_export_track"],
    "loop_habit_gui":       ["loop_habit_gui_list_habits", "loop_habit_gui_get_habit", "loop_habit_gui_check_habit", "loop_habit_gui_create_habit"],
    "my_expenses_gui":      ["my_expenses_gui_list_accounts", "my_expenses_gui_list_transactions", "my_expenses_gui_add_transaction", "my_expenses_gui_get_account"],
    "markor_gui":           ["markor_gui_list_notes", "markor_gui_get_note", "markor_gui_edit_note", "markor_gui_create_note"],
    "dialer_gui":           ["dialer_gui_list_calls", "dialer_gui_make_call"],
    "clock_gui":            ["clock_gui_list_alarms", "clock_gui_create_alarm", "clock_gui_update_alarm", "clock_gui_delete_alarm"],
    "maps_gui":             ["maps_gui_search", "maps_gui_get_place", "maps_gui_get_directions", "maps_gui_save_place"],
    "camera_gui":           ["camera_gui_list_captures", "camera_gui_take_photo"],
    "settings_gui":         ["settings_gui_list", "settings_gui_get", "settings_gui_update"],
}


class DataThread(BaseModel):
    """A cluster of related records across services that can form a task."""

    name: str
    description: str
    involved_services: list[str] = Field(default_factory=list)  # services involved (hand-written)
    involved_records: dict[str, list[str]] = Field(default_factory=dict)  # service -> [record_ids] (auto-backfilled after fixture gen)
    signal_density: float = 0.0  # fraction of total records involved
    difficulty: str | None = None  # simple/medium/hard
    category: str | None = None   # e.g. customer_service, finance_ops
    # Patrol fields: used by proactive patrol tasks
    thread_type: str = "standard"  # "standard" | "patrol_setup"
    patrol_signals: list[dict] = Field(default_factory=list)  # behavioral signals planted in logs


class TaskConstraints(BaseModel):
    """What tasks make sense for this persona."""

    plausible_categories: list[str] = Field(default_factory=list)
    implausible_categories: list[str] = Field(default_factory=list)


class Authority(BaseModel):
    """Role-based authority boundaries — directly affects grader generation."""

    can_approve: list[str] = Field(default_factory=list)
    cannot_approve: list[str] = Field(default_factory=list)
    communication_style: str = ""
    quality_focus: str = ""


class BusinessContext(BaseModel):
    """Business scenario seeds that guide fixture generation."""

    time_window: str = "2026-01-01 ~ 2026-03-31"
    work_schedule: str = ""
    active_issues: list[str] = Field(default_factory=list)
    business_goals: list[str] = Field(default_factory=list)
    risk_factors: list[str] = Field(default_factory=list)


class PersonaDefinition(BaseModel):
    """Complete persona definition loaded from persona.yaml."""

    persona_id: str
    persona_name: str
    language: str = "en"

    role: str
    company: str
    industry: str
    seniority: str = "mid"  # junior | mid | senior | executive

    traits: list[str] = Field(default_factory=list)
    daily_responsibilities: list[str] = Field(default_factory=list)

    primary_services: list[str] = Field(default_factory=list)
    secondary_services: list[str] = Field(default_factory=list)

    task_constraints: TaskConstraints = Field(default_factory=TaskConstraints)
    authority: Authority = Field(default_factory=Authority)
    business_context: BusinessContext = Field(default_factory=BusinessContext)

    fixture_scale: str = "standard"  # minimal(~80) | standard(~150) | rich(~250)

    data_threads: list[DataThread] = Field(default_factory=list)

    def get_data_threads_summary(self, max_threads: int = 5) -> str:
        """Return a summary of randomly sampled data threads for LLM context."""
        import random

        threads = self.data_threads
        if not threads:
            return "(no existing data threads)"
        if len(threads) > max_threads:
            threads = random.sample(threads, max_threads)
        lines = []
        for t in threads:
            lines.append(
                f"- **{t.name}**: {t.description}\n"
                f"  Services: {', '.join(t.involved_services)} | "
                f"Category: {t.category or 'N/A'} | Difficulty: {t.difficulty or 'N/A'}"
            )
        return "\n".join(lines)

    @classmethod
    def from_yaml(cls, path: str | Path) -> PersonaDefinition:
        path = Path(path)
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls(**data)


class GoldEnvironment:
    """Loads and provides access to a complete gold base environment."""

    def __init__(self, env_dir: str | Path):
        self.env_dir = Path(env_dir).resolve()
        self.persona = PersonaDefinition.from_yaml(self.env_dir / "persona.yaml")
        self.fixtures_dir = self.env_dir / "fixtures"
        self._fixtures_cache: dict[str, list[dict]] = {}
        self._available_services_cache: set[str] | None = None

    @property
    def env_name(self) -> str:
        return self.env_dir.name

    @property
    def available_services(self) -> set[str]:
        """Set of services this env actually has fixture data for.

        Computed once per ``GoldEnvironment`` instance from on-disk fixture
        counts (lazily, then cached). Aggregate methods like
        :meth:`get_all_fixtures` / :meth:`get_record_count` /
        :meth:`get_fixture_summary` iterate this set instead of the full
        ``ALL_SERVICES`` catalog so they never stat the ~35 service fixture
        files this persona has no data for.

        If fixtures are mutated on disk after the env is loaded, call
        :meth:`refresh_available_services` to recompute.
        """
        if self._available_services_cache is None:
            self._available_services_cache = available_services_in_env(self.env_dir)
        return self._available_services_cache

    def refresh_available_services(self) -> set[str]:
        """Recompute :attr:`available_services` after on-disk fixture changes.

        Call this from code that appends fixtures (e.g. ``_append_fixtures``)
        so the next aggregation picks up the newly populated services.
        """
        self._available_services_cache = available_services_in_env(self.env_dir)
        return self._available_services_cache

    def get_fixtures(self, service: str) -> list[dict]:
        """Load fixture records for a service.

        Returns ``[]`` for services not in :attr:`available_services` without
        any I/O — these are services the persona simply has no data for, and
        treating that as a warnable miss was the source of the ~35-warning
        spam-per-task in the gen-eval log. Callers that iterate
        ``available_services`` will never trigger that branch; callers that
        request an unknown / unavailable service get an empty list silently.
        """
        if service in self._fixtures_cache:
            return self._fixtures_cache[service]

        rel_path = SERVICE_FIXTURE_MAP.get(service)
        if not rel_path:
            log.warning("Unknown service: %s", service)
            self._fixtures_cache[service] = []
            return []

        # Persona simply didn't generate data for this app — silent miss.
        if service not in self.available_services:
            self._fixtures_cache[service] = []
            return []

        fixture_path = self.fixtures_dir / rel_path
        if not fixture_path.exists() or not fixture_path.is_file():
            # service is in available_services but file is gone — genuine
            # diagnostic problem, keep as a warning.
            log.warning("Fixture missing on disk despite available_services entry: %s", fixture_path)
            self._fixtures_cache[service] = []
            return []

        with open(fixture_path, "r", encoding="utf-8") as f:
            records = json.load(f)

        self._fixtures_cache[service] = records
        return records

    def get_all_fixtures(self) -> dict[str, list[dict]]:
        """Load fixtures for every service this persona actually uses."""
        return {svc: self.get_fixtures(svc) for svc in self.available_services}

    def get_record_count(self) -> dict[str, int]:
        """Return record counts per available service."""
        return {svc: len(self.get_fixtures(svc)) for svc in self.available_services}

    def get_total_records(self) -> int:
        counts = self.get_record_count()
        return sum(counts.values())

    def get_fixture_summary(self) -> str:
        """Return a token-efficient summary of all fixtures for LLM input.

        Only includes key fields (ID, title/subject, status) to save tokens.
        Iterates :attr:`available_services` so empty/uninstalled apps are not
        even probed.
        """
        # Render in ALL_SERVICES order for deterministic output across runs,
        # but only for services actually present.
        lines = []
        available = self.available_services
        for svc in ALL_SERVICES:
            if svc not in available:
                continue
            records = self.get_fixtures(svc)
            if not records:
                continue
            summary_fields = SERVICE_SUMMARY_FIELDS.get(svc, [])
            lines.append(f"\n## {svc} ({len(records)} records)")
            for rec in records:
                summary = {k: rec.get(k) for k in summary_fields if k in rec}
                lines.append(f"  - {json.dumps(summary, ensure_ascii=False)}")
        return "\n".join(lines)

    def get_thread_records(self, thread: DataThread) -> dict[str, list[dict]]:
        """Return full records for a specific data thread."""
        result: dict[str, list[dict]] = {}
        for svc, record_ids in thread.involved_records.items():
            id_field = SERVICE_ID_FIELD.get(svc, "id")
            all_records = self.get_fixtures(svc)
            matched = [r for r in all_records if r.get(id_field) in record_ids]
            if matched:
                result[svc] = matched
        return result

    def get_thread_records_json(self, thread: DataThread) -> str:
        """Return full records for a thread as a formatted JSON string."""
        records = self.get_thread_records(thread)
        return json.dumps(records, ensure_ascii=False, indent=2)

    def compute_signal_density(self, thread: DataThread) -> float:
        """Compute actual signal density for a thread."""
        total = self.get_total_records()
        if total == 0:
            return 0.0
        signal_count = sum(len(ids) for ids in thread.involved_records.values())
        return round(signal_count / total, 3)

    # ------------------------------------------------------------------
    # Activity log access
    # ------------------------------------------------------------------

    @property
    def logs_dir(self) -> Path:
        return self.env_dir / "logs"

    def get_activity_log(self, service: str) -> str:
        """Read a per-service activity log file."""
        path = self.logs_dir / "services" / f"{service}_activity.md"
        if path.exists():
            return path.read_text(encoding="utf-8")
        return ""

    def get_timeline(self) -> str:
        """Read the merged activity timeline.

        Concatenates weekly ``timeline_YYYYMMDD_YYYYMMDD.md`` files in
        chronological order. Falls back to a legacy monolithic
        ``timeline.md`` if no weekly files are present.
        """
        weekly = sorted(self.logs_dir.glob("timeline_*.md"))
        if weekly:
            return "\n".join(p.read_text(encoding="utf-8") for p in weekly)
        legacy = self.logs_dir / "timeline.md"
        if legacy.exists():
            return legacy.read_text(encoding="utf-8")
        return ""

    def get_work_journal(self) -> str:
        """Read the work journal (Layer 2)."""
        path = self.logs_dir / "work_journal.md"
        if path.exists():
            return path.read_text(encoding="utf-8")
        return ""

    def has_logs(self) -> bool:
        """Check if this environment has any activity logs."""
        if not self.logs_dir.exists():
            return False
        for f in self.logs_dir.rglob("*.md"):
            if f.stat().st_size > 0:
                return True
        return False


# ---------------------------------------------------------------------------
# Service-availability helpers for env-specific app filtering.
#
# Used by `build-persona` (clean up empty service dirs after generation) and
# `gen-eval` (drop services + tools from task.yaml that this persona never had
# any data for — same effect as "this person doesn't have that app installed").
# ---------------------------------------------------------------------------


def list_service_record_counts(env_dir: str | Path) -> dict[str, int]:
    """Return record count per registered service under ``env_dir/fixtures/``.

    Handles two fixture file shapes:
      - JSON list (the common case)
      - JSON object (zotero stores ``{"items": [...], "collections": [...]}``)
    Missing / unreadable / non-JSON files map to 0.
    """
    env_dir = Path(env_dir)
    counts: dict[str, int] = {}
    for svc, rel_path in SERVICE_FIXTURE_MAP.items():
        path = env_dir / "fixtures" / rel_path
        if not path.is_file():
            counts[svc] = 0
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            counts[svc] = 0
            continue
        if isinstance(data, list):
            counts[svc] = len(data)
        elif isinstance(data, dict):
            # Sum the lengths of all list-valued top-level fields (e.g. zotero
            # items + collections). A dict with no list values counts as 0.
            counts[svc] = sum(len(v) for v in data.values() if isinstance(v, list))
        else:
            counts[svc] = 0
    return counts


def cleanup_empty_service_fixtures(env_dir: str | Path) -> list[str]:
    """Delete every ``<env_dir>/fixtures/<service>/`` whose fixture is empty.

    "Empty" means the recognised fixture file has 0 records (or doesn't exist
    while the directory does). Returns the sorted list of service names that
    were removed so the caller can log them.
    """
    env_dir = Path(env_dir)
    fixtures_root = env_dir / "fixtures"
    counts = list_service_record_counts(env_dir)
    removed: list[str] = []
    for svc, count in counts.items():
        if count > 0:
            continue
        svc_dir = fixtures_root / svc
        if svc_dir.is_dir():
            shutil.rmtree(svc_dir)
            removed.append(svc)
    return sorted(removed)


def available_services_in_env(env_dir: str | Path) -> set[str]:
    """Return the set of services this env has any fixture records for.

    A service is "available" if its fixture file exists *and* contains at least
    one record. Used by gen-eval to filter task.yaml down to the persona's
    actual app set.
    """
    return {svc for svc, n in list_service_record_counts(env_dir).items() if n > 0}


def parse_template_services(template_path: str | Path) -> set[str]:
    """Read a task template yaml and return the set of services it declares.

    Only top-level ``services[].name`` entries that map to a known service in
    ``SERVICE_FIXTURE_MAP`` are returned — names that exist only as scoring
    component identifiers (e.g. ``core_tool_usage``, ``key_output``) are
    ignored. Used by ``build-persona`` and ``gen-eval`` to constrain fixture /
    seed / adapter generation to the apps the chosen template can serve.

    The same ``<<<CUSTOMIZE: ...>>>`` placeholders that the assembler strips
    are also stripped here so templates with unfilled metadata still parse.
    """
    import re

    template_path = Path(template_path)
    raw = template_path.read_text(encoding="utf-8")
    sanitized = re.sub(r'<<<CUSTOMIZE[^>]*>>>', 'PLACEHOLDER', raw)
    data = yaml.safe_load(sanitized) or {}
    services = data.get("services") or []
    names: set[str] = set()
    for svc in services:
        name = svc.get("name") if isinstance(svc, dict) else None
        if isinstance(name, str) and name in SERVICE_FIXTURE_MAP:
            names.add(name)
    return names
