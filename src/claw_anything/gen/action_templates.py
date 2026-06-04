"""Action templates for generating service-level activity logs (Layer 1).

Each service defines:
- **signal_patterns**: action sequences triggered by a fixture record being created.
  Keyed by a record classifier string (e.g. "sent", "received" for gmail).
- **noise_patterns**: idle / cancelled action sequences inserted randomly between
  signal sequences to simulate realistic user behaviour.

Every step in a pattern is a dict with:
    action       – human-readable action name
    duration_sec – (min, max) seconds the action takes (randomised at generation time)
    ref          – optional; "record_id" means the step references the triggering
                   fixture record, "random_existing" picks a random existing record,
                   "event_date" uses the event's date field, etc.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Per-step defaults
# ---------------------------------------------------------------------------
_SHORT = (1, 3)
_MEDIUM = (5, 30)
_LONG = (30, 300)
_QUICK = (1, 5)

# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------
_GMAIL_SIGNAL = {
    "sent": [
        {"action": "open_inbox", "duration_sec": _SHORT},
        {"action": "compose_new", "duration_sec": _QUICK},
        {"action": "edit_draft", "duration_sec": _LONG},
        {"action": "send_message", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "close_compose", "duration_sec": _SHORT},
    ],
    "received": [
        {"action": "open_inbox", "duration_sec": _SHORT},
        {"action": "open_message", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "read_message", "duration_sec": (10, 120)},
        {"action": "close_message", "duration_sec": _SHORT},
    ],
    "reply": [
        {"action": "open_inbox", "duration_sec": _SHORT},
        {"action": "open_message", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "read_message", "duration_sec": (10, 60)},
        {"action": "click_reply", "duration_sec": _SHORT},
        {"action": "edit_reply", "duration_sec": _LONG},
        {"action": "send_reply", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "close_message", "duration_sec": _SHORT},
    ],
    "draft": [
        {"action": "open_inbox", "duration_sec": _SHORT},
        {"action": "compose_new", "duration_sec": _QUICK},
        {"action": "edit_draft", "duration_sec": _LONG},
        {"action": "save_draft", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "close_compose", "duration_sec": _SHORT},
    ],
}

_GMAIL_NOISE = [
    # browse inbox then leave
    [
        {"action": "open_inbox", "duration_sec": _SHORT},
        {"action": "browse_inbox", "duration_sec": _MEDIUM},
        {"action": "close_inbox", "duration_sec": _SHORT},
    ],
    # start drafting then discard
    [
        {"action": "open_inbox", "duration_sec": _SHORT},
        {"action": "compose_new", "duration_sec": _QUICK},
        {"action": "edit_draft", "duration_sec": (10, 60)},
        {"action": "discard_draft", "duration_sec": _SHORT},
    ],
    # re-read an old message
    [
        {"action": "open_inbox", "duration_sec": _SHORT},
        {"action": "open_message", "duration_sec": _SHORT, "ref": "random_existing"},
        {"action": "read_message", "duration_sec": _MEDIUM},
        {"action": "close_message", "duration_sec": _SHORT},
    ],
    # search inbox
    [
        {"action": "open_inbox", "duration_sec": _SHORT},
        {"action": "search_inbox", "duration_sec": _MEDIUM},
        {"action": "browse_results", "duration_sec": _MEDIUM},
        {"action": "close_inbox", "duration_sec": _SHORT},
    ],
]

# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------
_CALENDAR_SIGNAL = {
    "created": [
        {"action": "open_calendar", "duration_sec": _SHORT},
        {"action": "navigate_to_date", "duration_sec": _QUICK, "ref": "event_date"},
        {"action": "create_new_event", "duration_sec": _QUICK},
        {"action": "fill_event_details", "duration_sec": (20, 120), "ref": "record_id"},
        {"action": "save_event", "duration_sec": _SHORT},
        {"action": "close_calendar", "duration_sec": _SHORT},
    ],
    "viewed": [
        {"action": "open_calendar", "duration_sec": _SHORT},
        {"action": "navigate_to_date", "duration_sec": _QUICK, "ref": "event_date"},
        {"action": "view_event", "duration_sec": (5, 30), "ref": "record_id"},
        {"action": "close_calendar", "duration_sec": _SHORT},
    ],
    "accepted": [
        {"action": "open_calendar", "duration_sec": _SHORT},
        {"action": "open_event", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "accept_invite", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "close_calendar", "duration_sec": _SHORT},
    ],
}

_CALENDAR_NOISE = [
    [
        {"action": "open_calendar", "duration_sec": _SHORT},
        {"action": "browse_week", "duration_sec": _MEDIUM},
        {"action": "close_calendar", "duration_sec": _SHORT},
    ],
    [
        {"action": "open_calendar", "duration_sec": _SHORT},
        {"action": "browse_month", "duration_sec": _MEDIUM},
        {"action": "close_calendar", "duration_sec": _SHORT},
    ],
]

# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------
_CONTACTS_SIGNAL = {
    "added": [
        {"action": "open_contacts", "duration_sec": _SHORT},
        {"action": "create_new_contact", "duration_sec": _QUICK},
        {"action": "fill_contact_details", "duration_sec": (15, 60), "ref": "record_id"},
        {"action": "save_contact", "duration_sec": _SHORT},
        {"action": "close_contacts", "duration_sec": _SHORT},
    ],
    "viewed": [
        {"action": "open_contacts", "duration_sec": _SHORT},
        {"action": "search_contacts", "duration_sec": _QUICK},
        {"action": "view_contact", "duration_sec": (5, 20), "ref": "record_id"},
        {"action": "close_contacts", "duration_sec": _SHORT},
    ],
}

_CONTACTS_NOISE = [
    [
        {"action": "open_contacts", "duration_sec": _SHORT},
        {"action": "browse_contacts", "duration_sec": _MEDIUM},
        {"action": "close_contacts", "duration_sec": _SHORT},
    ],
    [
        {"action": "open_contacts", "duration_sec": _SHORT},
        {"action": "search_contacts", "duration_sec": _QUICK},
        {"action": "browse_results", "duration_sec": _MEDIUM},
        {"action": "close_contacts", "duration_sec": _SHORT},
    ],
]

# ---------------------------------------------------------------------------
# Todo
# ---------------------------------------------------------------------------
_TODO_SIGNAL = {
    "created": [
        {"action": "open_todo", "duration_sec": _SHORT},
        {"action": "create_new_task", "duration_sec": _QUICK},
        {"action": "fill_task_details", "duration_sec": (10, 60), "ref": "record_id"},
        {"action": "set_priority", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "save_task", "duration_sec": _SHORT},
        {"action": "close_todo", "duration_sec": _SHORT},
    ],
    "completed": [
        {"action": "open_todo", "duration_sec": _SHORT},
        {"action": "view_task", "duration_sec": _QUICK, "ref": "record_id"},
        {"action": "mark_complete", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "close_todo", "duration_sec": _SHORT},
    ],
    "viewed": [
        {"action": "open_todo", "duration_sec": _SHORT},
        {"action": "view_task", "duration_sec": (5, 30), "ref": "record_id"},
        {"action": "close_todo", "duration_sec": _SHORT},
    ],
}

_TODO_NOISE = [
    [
        {"action": "open_todo", "duration_sec": _SHORT},
        {"action": "browse_tasks", "duration_sec": _MEDIUM},
        {"action": "close_todo", "duration_sec": _SHORT},
    ],
    [
        {"action": "open_todo", "duration_sec": _SHORT},
        {"action": "create_new_task", "duration_sec": _QUICK},
        {"action": "fill_task_details", "duration_sec": (10, 30)},
        {"action": "delete_task", "duration_sec": _SHORT},
    ],
]

# ---------------------------------------------------------------------------
# KB (Knowledge Base)
# ---------------------------------------------------------------------------
_KB_SIGNAL = {
    "read": [
        {"action": "open_kb", "duration_sec": _SHORT},
        {"action": "search_articles", "duration_sec": _QUICK},
        {"action": "open_article", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "read_article", "duration_sec": (30, 300)},
        {"action": "close_article", "duration_sec": _SHORT},
    ],
    "created": [
        {"action": "open_kb", "duration_sec": _SHORT},
        {"action": "create_new_article", "duration_sec": _QUICK},
        {"action": "edit_article", "duration_sec": _LONG},
        {"action": "publish_article", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "close_kb", "duration_sec": _SHORT},
    ],
}

_KB_NOISE = [
    [
        {"action": "open_kb", "duration_sec": _SHORT},
        {"action": "browse_categories", "duration_sec": _MEDIUM},
        {"action": "close_kb", "duration_sec": _SHORT},
    ],
    [
        {"action": "open_kb", "duration_sec": _SHORT},
        {"action": "search_articles", "duration_sec": _QUICK},
        {"action": "browse_results", "duration_sec": _MEDIUM},
        {"action": "close_kb", "duration_sec": _SHORT},
    ],
]

# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------
_NOTES_SIGNAL = {
    "created": [
        {"action": "open_notes", "duration_sec": _SHORT},
        {"action": "create_new_note", "duration_sec": _QUICK},
        {"action": "edit_note", "duration_sec": _LONG, "ref": "record_id"},
        {"action": "save_note", "duration_sec": _SHORT},
        {"action": "close_notes", "duration_sec": _SHORT},
    ],
    "viewed": [
        {"action": "open_notes", "duration_sec": _SHORT},
        {"action": "open_note", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "read_note", "duration_sec": (10, 120)},
        {"action": "close_note", "duration_sec": _SHORT},
    ],
}

_NOTES_NOISE = [
    [
        {"action": "open_notes", "duration_sec": _SHORT},
        {"action": "browse_notes", "duration_sec": _MEDIUM},
        {"action": "close_notes", "duration_sec": _SHORT},
    ],
    [
        {"action": "open_notes", "duration_sec": _SHORT},
        {"action": "create_new_note", "duration_sec": _QUICK},
        {"action": "edit_note", "duration_sec": (5, 20)},
        {"action": "delete_note", "duration_sec": _SHORT},
    ],
]

# ---------------------------------------------------------------------------
# Finance
# ---------------------------------------------------------------------------
_FINANCE_SIGNAL = {
    "recorded": [
        {"action": "open_finance", "duration_sec": _SHORT},
        {"action": "view_transaction", "duration_sec": (5, 15), "ref": "record_id"},
        {"action": "categorize_transaction", "duration_sec": _QUICK, "ref": "record_id"},
        {"action": "close_finance", "duration_sec": _SHORT},
    ],
    "created": [
        {"action": "open_finance", "duration_sec": _SHORT},
        {"action": "create_transaction", "duration_sec": _QUICK},
        {"action": "fill_transaction_details", "duration_sec": (10, 60), "ref": "record_id"},
        {"action": "save_transaction", "duration_sec": _SHORT},
        {"action": "close_finance", "duration_sec": _SHORT},
    ],
}

_FINANCE_NOISE = [
    [
        {"action": "open_finance", "duration_sec": _SHORT},
        {"action": "browse_transactions", "duration_sec": _MEDIUM},
        {"action": "close_finance", "duration_sec": _SHORT},
    ],
    [
        {"action": "open_finance", "duration_sec": _SHORT},
        {"action": "view_balance", "duration_sec": _MEDIUM},
        {"action": "close_finance", "duration_sec": _SHORT},
    ],
]

# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------
_INVENTORY_SIGNAL = {
    "checked": [
        {"action": "open_inventory", "duration_sec": _SHORT},
        {"action": "search_product", "duration_sec": _QUICK},
        {"action": "view_product", "duration_sec": (5, 20), "ref": "record_id"},
        {"action": "close_inventory", "duration_sec": _SHORT},
    ],
    "updated": [
        {"action": "open_inventory", "duration_sec": _SHORT},
        {"action": "search_product", "duration_sec": _QUICK},
        {"action": "view_product", "duration_sec": _QUICK, "ref": "record_id"},
        {"action": "update_stock", "duration_sec": (10, 30), "ref": "record_id"},
        {"action": "save_changes", "duration_sec": _SHORT},
        {"action": "close_inventory", "duration_sec": _SHORT},
    ],
    "created": [
        {"action": "open_inventory", "duration_sec": _SHORT},
        {"action": "add_new_product", "duration_sec": _QUICK},
        {"action": "fill_product_details", "duration_sec": (15, 60), "ref": "record_id"},
        {"action": "save_product", "duration_sec": _SHORT},
        {"action": "close_inventory", "duration_sec": _SHORT},
    ],
}

_INVENTORY_NOISE = [
    [
        {"action": "open_inventory", "duration_sec": _SHORT},
        {"action": "browse_products", "duration_sec": _MEDIUM},
        {"action": "close_inventory", "duration_sec": _SHORT},
    ],
]

# ---------------------------------------------------------------------------
# Helpdesk
# ---------------------------------------------------------------------------
_HELPDESK_SIGNAL = {
    "created": [
        {"action": "open_helpdesk", "duration_sec": _SHORT},
        {"action": "create_new_ticket", "duration_sec": _QUICK},
        {"action": "fill_ticket_details", "duration_sec": (20, 120), "ref": "record_id"},
        {"action": "submit_ticket", "duration_sec": _SHORT},
        {"action": "close_helpdesk", "duration_sec": _SHORT},
    ],
    "updated": [
        {"action": "open_helpdesk", "duration_sec": _SHORT},
        {"action": "open_ticket", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "update_ticket_status", "duration_sec": (5, 30), "ref": "record_id"},
        {"action": "save_changes", "duration_sec": _SHORT},
        {"action": "close_helpdesk", "duration_sec": _SHORT},
    ],
    "viewed": [
        {"action": "open_helpdesk", "duration_sec": _SHORT},
        {"action": "open_ticket", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "read_ticket", "duration_sec": (10, 60)},
        {"action": "close_helpdesk", "duration_sec": _SHORT},
    ],
}

_HELPDESK_NOISE = [
    [
        {"action": "open_helpdesk", "duration_sec": _SHORT},
        {"action": "browse_tickets", "duration_sec": _MEDIUM},
        {"action": "close_helpdesk", "duration_sec": _SHORT},
    ],
]

# ---------------------------------------------------------------------------
# CRM
# ---------------------------------------------------------------------------
_CRM_SIGNAL = {
    "viewed": [
        {"action": "open_crm", "duration_sec": _SHORT},
        {"action": "search_customer", "duration_sec": _QUICK},
        {"action": "view_customer", "duration_sec": (10, 60), "ref": "record_id"},
        {"action": "close_crm", "duration_sec": _SHORT},
    ],
    "updated": [
        {"action": "open_crm", "duration_sec": _SHORT},
        {"action": "search_customer", "duration_sec": _QUICK},
        {"action": "view_customer", "duration_sec": _QUICK, "ref": "record_id"},
        {"action": "edit_customer", "duration_sec": (10, 60), "ref": "record_id"},
        {"action": "save_changes", "duration_sec": _SHORT},
        {"action": "close_crm", "duration_sec": _SHORT},
    ],
    "created": [
        {"action": "open_crm", "duration_sec": _SHORT},
        {"action": "create_new_customer", "duration_sec": _QUICK},
        {"action": "fill_customer_details", "duration_sec": (20, 90), "ref": "record_id"},
        {"action": "save_customer", "duration_sec": _SHORT},
        {"action": "close_crm", "duration_sec": _SHORT},
    ],
}

_CRM_NOISE = [
    [
        {"action": "open_crm", "duration_sec": _SHORT},
        {"action": "browse_customers", "duration_sec": _MEDIUM},
        {"action": "close_crm", "duration_sec": _SHORT},
    ],
    [
        {"action": "open_crm", "duration_sec": _SHORT},
        {"action": "view_dashboard", "duration_sec": _MEDIUM},
        {"action": "close_crm", "duration_sec": _SHORT},
    ],
]

# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
_SCHEDULER_SIGNAL = {
    "created": [
        {"action": "open_scheduler", "duration_sec": _SHORT},
        {"action": "create_new_job", "duration_sec": _QUICK},
        {"action": "configure_job", "duration_sec": (15, 60), "ref": "record_id"},
        {"action": "save_job", "duration_sec": _SHORT},
        {"action": "close_scheduler", "duration_sec": _SHORT},
    ],
    "viewed": [
        {"action": "open_scheduler", "duration_sec": _SHORT},
        {"action": "view_job", "duration_sec": (5, 20), "ref": "record_id"},
        {"action": "view_execution_history", "duration_sec": _MEDIUM, "ref": "record_id"},
        {"action": "close_scheduler", "duration_sec": _SHORT},
    ],
}

_SCHEDULER_NOISE = [
    [
        {"action": "open_scheduler", "duration_sec": _SHORT},
        {"action": "browse_jobs", "duration_sec": _MEDIUM},
        {"action": "close_scheduler", "duration_sec": _SHORT},
    ],
]

# ---------------------------------------------------------------------------
# RSS
# ---------------------------------------------------------------------------
_RSS_SIGNAL = {
    "read": [
        {"action": "open_rss", "duration_sec": _SHORT},
        {"action": "browse_feed", "duration_sec": _MEDIUM},
        {"action": "open_article", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "read_article", "duration_sec": (30, 300)},
        {"action": "close_article", "duration_sec": _SHORT},
    ],
}

_RSS_NOISE = [
    [
        {"action": "open_rss", "duration_sec": _SHORT},
        {"action": "browse_feed", "duration_sec": _MEDIUM},
        {"action": "close_rss", "duration_sec": _SHORT},
    ],
    [
        {"action": "open_rss", "duration_sec": _SHORT},
        {"action": "refresh_feed", "duration_sec": _QUICK},
        {"action": "browse_feed", "duration_sec": _MEDIUM},
        {"action": "close_rss", "duration_sec": _SHORT},
    ],
]

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_CONFIG_SIGNAL = {
    "viewed": [
        {"action": "open_config", "duration_sec": _SHORT},
        {"action": "view_integration", "duration_sec": (5, 20), "ref": "record_id"},
        {"action": "close_config", "duration_sec": _SHORT},
    ],
    "updated": [
        {"action": "open_config", "duration_sec": _SHORT},
        {"action": "view_integration", "duration_sec": _QUICK, "ref": "record_id"},
        {"action": "edit_integration", "duration_sec": (10, 60), "ref": "record_id"},
        {"action": "test_connection", "duration_sec": (5, 15), "ref": "record_id"},
        {"action": "save_config", "duration_sec": _SHORT},
        {"action": "close_config", "duration_sec": _SHORT},
    ],
}

_CONFIG_NOISE = [
    [
        {"action": "open_config", "duration_sec": _SHORT},
        {"action": "browse_integrations", "duration_sec": _MEDIUM},
        {"action": "close_config", "duration_sec": _SHORT},
    ],
]

# ---------------------------------------------------------------------------
# Workmail (company-internal email — colleague-facing only)
# ---------------------------------------------------------------------------
_WORKMAIL_SIGNAL = {
    "sent": [
        {"action": "open_workmail", "duration_sec": _SHORT},
        {"action": "compose_new", "duration_sec": _QUICK},
        {"action": "edit_draft", "duration_sec": _LONG},
        {"action": "send_message", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "close_compose", "duration_sec": _SHORT},
    ],
    "received": [
        {"action": "open_workmail", "duration_sec": _SHORT},
        {"action": "open_message", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "read_message", "duration_sec": (10, 120)},
        {"action": "close_message", "duration_sec": _SHORT},
    ],
    "reply": [
        {"action": "open_workmail", "duration_sec": _SHORT},
        {"action": "open_message", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "read_message", "duration_sec": (10, 60)},
        {"action": "click_reply", "duration_sec": _SHORT},
        {"action": "edit_reply", "duration_sec": _LONG},
        {"action": "send_reply", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "close_message", "duration_sec": _SHORT},
    ],
    "draft": [
        {"action": "open_workmail", "duration_sec": _SHORT},
        {"action": "compose_new", "duration_sec": _QUICK},
        {"action": "edit_draft", "duration_sec": _LONG},
        {"action": "save_draft", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "close_compose", "duration_sec": _SHORT},
    ],
}

_WORKMAIL_NOISE = [
    [
        {"action": "open_workmail", "duration_sec": _SHORT},
        {"action": "browse_inbox", "duration_sec": _MEDIUM},
        {"action": "close_workmail", "duration_sec": _SHORT},
    ],
    [
        {"action": "open_workmail", "duration_sec": _SHORT},
        {"action": "compose_new", "duration_sec": _QUICK},
        {"action": "edit_draft", "duration_sec": (10, 60)},
        {"action": "discard_draft", "duration_sec": _SHORT},
    ],
    [
        {"action": "open_workmail", "duration_sec": _SHORT},
        {"action": "open_message", "duration_sec": _SHORT, "ref": "random_existing"},
        {"action": "read_message", "duration_sec": _MEDIUM},
        {"action": "close_message", "duration_sec": _SHORT},
    ],
]

# ---------------------------------------------------------------------------
# Claw-Notion (page + block + database knowledge tree)
# ---------------------------------------------------------------------------
_CLAW_NOTION_SIGNAL = {
    "created": [
        {"action": "open_notion", "duration_sec": _SHORT},
        {"action": "create_new_page", "duration_sec": _QUICK},
        {"action": "set_page_title", "duration_sec": _QUICK, "ref": "record_id"},
        {"action": "fill_page_blocks", "duration_sec": _LONG},
        {"action": "save_page", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "close_notion", "duration_sec": _SHORT},
    ],
    "database_added": [
        {"action": "open_notion", "duration_sec": _SHORT},
        {"action": "navigate_to_database", "duration_sec": _QUICK},
        {"action": "create_database_row", "duration_sec": _QUICK},
        {"action": "fill_properties", "duration_sec": _MEDIUM, "ref": "record_id"},
        {"action": "save_page", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "close_notion", "duration_sec": _SHORT},
    ],
    "viewed": [
        {"action": "open_notion", "duration_sec": _SHORT},
        {"action": "open_page", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "read_blocks", "duration_sec": (10, 90)},
        {"action": "close_page", "duration_sec": _SHORT},
    ],
}

_CLAW_NOTION_NOISE = [
    [
        {"action": "open_notion", "duration_sec": _SHORT},
        {"action": "browse_sidebar", "duration_sec": _MEDIUM},
        {"action": "close_notion", "duration_sec": _SHORT},
    ],
    [
        {"action": "open_notion", "duration_sec": _SHORT},
        {"action": "create_new_page", "duration_sec": _QUICK},
        {"action": "edit_page_draft", "duration_sec": (10, 60)},
        {"action": "discard_page", "duration_sec": _SHORT},
    ],
    [
        {"action": "open_notion", "duration_sec": _SHORT},
        {"action": "open_page", "duration_sec": _SHORT, "ref": "random_existing"},
        {"action": "read_blocks", "duration_sec": _MEDIUM},
        {"action": "close_page", "duration_sec": _SHORT},
    ],
]

# ---------------------------------------------------------------------------
# Claw-Obsidian (markdown vault with bidirectional links + tags)
# ---------------------------------------------------------------------------
_CLAW_OBSIDIAN_SIGNAL = {
    "created": [
        {"action": "open_obsidian", "duration_sec": _SHORT},
        {"action": "create_new_note", "duration_sec": _QUICK},
        {"action": "set_note_title", "duration_sec": _QUICK, "ref": "record_id"},
        {"action": "write_markdown", "duration_sec": _LONG},
        {"action": "save_note", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "close_obsidian", "duration_sec": _SHORT},
    ],
    "linked": [
        {"action": "open_obsidian", "duration_sec": _SHORT},
        {"action": "open_note", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "insert_wikilink", "duration_sec": _QUICK},
        {"action": "save_note", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "close_note", "duration_sec": _SHORT},
    ],
    "appended": [
        {"action": "open_obsidian", "duration_sec": _SHORT},
        {"action": "open_note", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "append_paragraph", "duration_sec": _MEDIUM},
        {"action": "save_note", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "close_note", "duration_sec": _SHORT},
    ],
    "viewed": [
        {"action": "open_obsidian", "duration_sec": _SHORT},
        {"action": "open_note", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "read_content", "duration_sec": (10, 90)},
        {"action": "close_note", "duration_sec": _SHORT},
    ],
}

_CLAW_OBSIDIAN_NOISE = [
    [
        {"action": "open_obsidian", "duration_sec": _SHORT},
        {"action": "browse_vault", "duration_sec": _MEDIUM},
        {"action": "close_obsidian", "duration_sec": _SHORT},
    ],
    [
        {"action": "open_obsidian", "duration_sec": _SHORT},
        {"action": "create_new_note", "duration_sec": _QUICK},
        {"action": "write_markdown", "duration_sec": (10, 60)},
        {"action": "discard_note", "duration_sec": _SHORT},
    ],
    [
        {"action": "open_obsidian", "duration_sec": _SHORT},
        {"action": "search_notes", "duration_sec": _MEDIUM},
        {"action": "browse_results", "duration_sec": _MEDIUM},
        {"action": "close_obsidian", "duration_sec": _SHORT},
    ],
]

# ---------------------------------------------------------------------------
# Meeting (Zoom-like lifecycle: scheduled -> started -> ended | cancelled)
# ---------------------------------------------------------------------------
_MEETING_SIGNAL = {
    "created": [
        {"action": "open_meeting_app", "duration_sec": _SHORT},
        {"action": "create_new_meeting", "duration_sec": _QUICK},
        {"action": "fill_meeting_details", "duration_sec": _MEDIUM, "ref": "record_id"},
        {"action": "invite_participants", "duration_sec": _MEDIUM},
        {"action": "save_meeting", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "close_meeting_app", "duration_sec": _SHORT},
    ],
    "started": [
        {"action": "open_meeting_app", "duration_sec": _SHORT},
        {"action": "open_meeting", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "start_meeting", "duration_sec": _QUICK, "ref": "record_id"},
        {"action": "talk_in_meeting", "duration_sec": _LONG},
    ],
    "ended": [
        {"action": "open_meeting_app", "duration_sec": _SHORT},
        {"action": "open_meeting", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "start_meeting", "duration_sec": _QUICK, "ref": "record_id"},
        {"action": "talk_in_meeting", "duration_sec": _LONG},
        {"action": "end_meeting", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "close_meeting_app", "duration_sec": _SHORT},
    ],
    "cancelled": [
        {"action": "open_meeting_app", "duration_sec": _SHORT},
        {"action": "open_meeting", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "cancel_meeting", "duration_sec": _QUICK, "ref": "record_id"},
        {"action": "close_meeting_app", "duration_sec": _SHORT},
    ],
    "viewed": [
        {"action": "open_meeting_app", "duration_sec": _SHORT},
        {"action": "open_meeting", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "read_meeting_details", "duration_sec": _MEDIUM},
        {"action": "close_meeting_app", "duration_sec": _SHORT},
    ],
}

_MEETING_NOISE = [
    [
        {"action": "open_meeting_app", "duration_sec": _SHORT},
        {"action": "browse_meeting_list", "duration_sec": _MEDIUM},
        {"action": "close_meeting_app", "duration_sec": _SHORT},
    ],
    [
        {"action": "open_meeting_app", "duration_sec": _SHORT},
        {"action": "create_new_meeting", "duration_sec": _QUICK},
        {"action": "fill_meeting_details", "duration_sec": (10, 45)},
        {"action": "discard_meeting", "duration_sec": _SHORT},
    ],
]

# ---------------------------------------------------------------------------
# Claw-Zotero (literature manager: items + collections + per-item notes)
# ---------------------------------------------------------------------------
_CLAW_ZOTERO_SIGNAL = {
    "added": [
        {"action": "open_zotero", "duration_sec": _SHORT},
        {"action": "add_new_item", "duration_sec": _QUICK},
        {"action": "fill_item_metadata", "duration_sec": _MEDIUM, "ref": "record_id"},
        {"action": "save_item", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "close_zotero", "duration_sec": _SHORT},
    ],
    "noted": [
        {"action": "open_zotero", "duration_sec": _SHORT},
        {"action": "open_item", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "add_item_note", "duration_sec": _LONG, "ref": "record_id"},
        {"action": "save_item", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "close_item", "duration_sec": _SHORT},
    ],
    "collected": [
        {"action": "open_zotero", "duration_sec": _SHORT},
        {"action": "open_item", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "add_to_collection", "duration_sec": _QUICK, "ref": "record_id"},
        {"action": "close_item", "duration_sec": _SHORT},
    ],
    "read": [
        {"action": "open_zotero", "duration_sec": _SHORT},
        {"action": "open_item", "duration_sec": _SHORT, "ref": "record_id"},
        {"action": "read_abstract", "duration_sec": (10, 90)},
        {"action": "close_item", "duration_sec": _SHORT},
    ],
}

_CLAW_ZOTERO_NOISE = [
    [
        {"action": "open_zotero", "duration_sec": _SHORT},
        {"action": "browse_library", "duration_sec": _MEDIUM},
        {"action": "close_zotero", "duration_sec": _SHORT},
    ],
    [
        {"action": "open_zotero", "duration_sec": _SHORT},
        {"action": "search_library", "duration_sec": _MEDIUM},
        {"action": "browse_results", "duration_sec": _MEDIUM},
        {"action": "close_zotero", "duration_sec": _SHORT},
    ],
]

_STUMAIL_SIGNAL = {
    "received": [
        {"action": "open_stumail",   "duration_sec": _SHORT},
        {"action": "read_message",   "duration_sec": _MEDIUM},
        {"action": "close_stumail",  "duration_sec": _SHORT},
    ],
    "sent": [
        {"action": "open_stumail",   "duration_sec": _SHORT},
        {"action": "compose_email",  "duration_sec": _LONG},
        {"action": "send_email",     "duration_sec": _SHORT},
        {"action": "close_stumail",  "duration_sec": _SHORT},
    ],
    "draft": [
        {"action": "open_stumail",   "duration_sec": _SHORT},
        {"action": "compose_draft",  "duration_sec": _MEDIUM},
        {"action": "save_draft",     "duration_sec": _SHORT},
        {"action": "close_stumail",  "duration_sec": _SHORT},
    ],
}

_STUMAIL_NOISE = [
    [
        {"action": "open_stumail",  "duration_sec": _SHORT},
        {"action": "check_inbox",   "duration_sec": _MEDIUM},
        {"action": "close_stumail", "duration_sec": _SHORT},
    ],
    [
        {"action": "open_stumail",  "duration_sec": _SHORT},
        {"action": "browse_inbox",  "duration_sec": _MEDIUM},
        {"action": "mark_as_read",  "duration_sec": _SHORT},
        {"action": "close_stumail", "duration_sec": _SHORT},
    ],
]

_CLAW_FEISHU_SIGNAL = {
    "messaged": [
        {"action": "open_feishu",      "duration_sec": _SHORT},
        {"action": "open_group",       "duration_sec": _SHORT},
        {"action": "send_message",     "duration_sec": _MEDIUM},
        {"action": "close_feishu",     "duration_sec": _SHORT},
    ],
    "meeting_scheduled": [
        {"action": "open_feishu",      "duration_sec": _SHORT},
        {"action": "open_group",       "duration_sec": _SHORT},
        {"action": "schedule_meeting", "duration_sec": _MEDIUM},
        {"action": "close_feishu",     "duration_sec": _SHORT},
    ],
    "doc_created": [
        {"action": "open_feishu",      "duration_sec": _SHORT},
        {"action": "open_group",       "duration_sec": _SHORT},
        {"action": "create_doc",       "duration_sec": _LONG},
        {"action": "close_feishu",     "duration_sec": _SHORT},
    ],
}

_CLAW_FEISHU_NOISE = [
    [
        {"action": "open_feishu",     "duration_sec": _SHORT},
        {"action": "browse_groups",   "duration_sec": _MEDIUM},
        {"action": "close_feishu",    "duration_sec": _SHORT},
    ],
    [
        {"action": "open_feishu",     "duration_sec": _SHORT},
        {"action": "read_messages",   "duration_sec": _MEDIUM},
        {"action": "view_docs",       "duration_sec": _MEDIUM},
        {"action": "close_feishu",    "duration_sec": _SHORT},
    ],
]

_CLAW_SLACK_SIGNAL = {
    "posted": [
        {"action": "open_slack",      "duration_sec": _SHORT},
        {"action": "open_channel",    "duration_sec": _SHORT},
        {"action": "post_message",    "duration_sec": _MEDIUM},
        {"action": "close_slack",     "duration_sec": _SHORT},
    ],
    "replied": [
        {"action": "open_slack",      "duration_sec": _SHORT},
        {"action": "open_thread",     "duration_sec": _SHORT},
        {"action": "post_reply",      "duration_sec": _MEDIUM},
        {"action": "close_slack",     "duration_sec": _SHORT},
    ],
    "reacted": [
        {"action": "open_slack",      "duration_sec": _SHORT},
        {"action": "open_channel",    "duration_sec": _SHORT},
        {"action": "add_reaction",    "duration_sec": _SHORT},
        {"action": "close_slack",     "duration_sec": _SHORT},
    ],
}

_CLAW_SLACK_NOISE = [
    [
        {"action": "open_slack",      "duration_sec": _SHORT},
        {"action": "browse_channels", "duration_sec": _MEDIUM},
        {"action": "close_slack",     "duration_sec": _SHORT},
    ],
    [
        {"action": "open_slack",      "duration_sec": _SHORT},
        {"action": "read_messages",   "duration_sec": _MEDIUM},
        {"action": "search_slack",    "duration_sec": _MEDIUM},
        {"action": "close_slack",     "duration_sec": _SHORT},
    ],
]

_CLAW_FACEBOOK_SIGNAL = {
    "liked": [
        {"action": "open_facebook",   "duration_sec": _SHORT},
        {"action": "browse_feed",     "duration_sec": _MEDIUM},
        {"action": "like_post",       "duration_sec": _SHORT},
        {"action": "close_facebook",  "duration_sec": _SHORT},
    ],
    "saved": [
        {"action": "open_facebook",   "duration_sec": _SHORT},
        {"action": "browse_feed",     "duration_sec": _MEDIUM},
        {"action": "save_post",       "duration_sec": _SHORT},
        {"action": "close_facebook",  "duration_sec": _SHORT},
    ],
    "reposted": [
        {"action": "open_facebook",   "duration_sec": _SHORT},
        {"action": "view_post",       "duration_sec": _MEDIUM},
        {"action": "repost",          "duration_sec": _MEDIUM},
        {"action": "close_facebook",  "duration_sec": _SHORT},
    ],
    "viewed": [
        {"action": "open_facebook",   "duration_sec": _SHORT},
        {"action": "view_post",       "duration_sec": _MEDIUM},
        {"action": "close_facebook",  "duration_sec": _SHORT},
    ],
}

_CLAW_FACEBOOK_NOISE = [
    [
        {"action": "open_facebook",   "duration_sec": _SHORT},
        {"action": "scroll_feed",     "duration_sec": _MEDIUM},
        {"action": "close_facebook",  "duration_sec": _SHORT},
    ],
    [
        {"action": "open_facebook",   "duration_sec": _SHORT},
        {"action": "search_facebook", "duration_sec": _MEDIUM},
        {"action": "close_facebook",  "duration_sec": _SHORT},
    ],
]

_CLAW_ZHIHU_SIGNAL = {
    "answered": [
        {"action": "open_zhihu",      "duration_sec": _SHORT},
        {"action": "view_question",   "duration_sec": _MEDIUM},
        {"action": "post_answer",     "duration_sec": _LONG},
        {"action": "close_zhihu",     "duration_sec": _SHORT},
    ],
    "upvoted": [
        {"action": "open_zhihu",      "duration_sec": _SHORT},
        {"action": "read_answer",     "duration_sec": _MEDIUM},
        {"action": "upvote_answer",   "duration_sec": _SHORT},
        {"action": "close_zhihu",     "duration_sec": _SHORT},
    ],
    "followed": [
        {"action": "open_zhihu",      "duration_sec": _SHORT},
        {"action": "view_question",   "duration_sec": _MEDIUM},
        {"action": "follow_question", "duration_sec": _SHORT},
        {"action": "close_zhihu",     "duration_sec": _SHORT},
    ],
    "browsed": [
        {"action": "open_zhihu",      "duration_sec": _SHORT},
        {"action": "search_questions","duration_sec": _MEDIUM},
        {"action": "close_zhihu",     "duration_sec": _SHORT},
    ],
}

_CLAW_ZHIHU_NOISE = [
    [
        {"action": "open_zhihu",      "duration_sec": _SHORT},
        {"action": "browse_topics",   "duration_sec": _MEDIUM},
        {"action": "close_zhihu",     "duration_sec": _SHORT},
    ],
    [
        {"action": "open_zhihu",      "duration_sec": _SHORT},
        {"action": "read_answers",    "duration_sec": _MEDIUM},
        {"action": "close_zhihu",     "duration_sec": _SHORT},
    ],
]

_CLAW_WECHAT_SIGNAL = {
    "messaged": [
        {"action": "open_wechat",     "duration_sec": _SHORT},
        {"action": "open_chat",       "duration_sec": _SHORT},
        {"action": "send_message",    "duration_sec": _MEDIUM},
        {"action": "close_wechat",    "duration_sec": _SHORT},
    ],
    "moment_posted": [
        {"action": "open_wechat",     "duration_sec": _SHORT},
        {"action": "compose_moment",  "duration_sec": _MEDIUM},
        {"action": "post_moment",     "duration_sec": _SHORT},
        {"action": "close_wechat",    "duration_sec": _SHORT},
    ],
    "muted": [
        {"action": "open_wechat",     "duration_sec": _SHORT},
        {"action": "open_chat",       "duration_sec": _SHORT},
        {"action": "mute_chat",       "duration_sec": _SHORT},
        {"action": "close_wechat",    "duration_sec": _SHORT},
    ],
}

_CLAW_WECHAT_NOISE = [
    [
        {"action": "open_wechat",     "duration_sec": _SHORT},
        {"action": "browse_chats",    "duration_sec": _MEDIUM},
        {"action": "close_wechat",    "duration_sec": _SHORT},
    ],
    [
        {"action": "open_wechat",     "duration_sec": _SHORT},
        {"action": "browse_moments",  "duration_sec": _MEDIUM},
        {"action": "close_wechat",    "duration_sec": _SHORT},
    ],
]

_CLAW_QQ_SIGNAL = {
    "messaged": [
        {"action": "open_qq",         "duration_sec": _SHORT},
        {"action": "open_chat",       "duration_sec": _SHORT},
        {"action": "send_message",    "duration_sec": _MEDIUM},
        {"action": "close_qq",        "duration_sec": _SHORT},
    ],
    "zone_posted": [
        {"action": "open_qq",         "duration_sec": _SHORT},
        {"action": "compose_zone",    "duration_sec": _MEDIUM},
        {"action": "post_zone",       "duration_sec": _SHORT},
        {"action": "close_qq",        "duration_sec": _SHORT},
    ],
    "muted": [
        {"action": "open_qq",         "duration_sec": _SHORT},
        {"action": "open_chat",       "duration_sec": _SHORT},
        {"action": "mute_chat",       "duration_sec": _SHORT},
        {"action": "close_qq",        "duration_sec": _SHORT},
    ],
}

_CLAW_QQ_NOISE = [
    [
        {"action": "open_qq",         "duration_sec": _SHORT},
        {"action": "browse_chats",    "duration_sec": _MEDIUM},
        {"action": "close_qq",        "duration_sec": _SHORT},
    ],
    [
        {"action": "open_qq",         "duration_sec": _SHORT},
        {"action": "browse_zone",     "duration_sec": _MEDIUM},
        {"action": "close_qq",        "duration_sec": _SHORT},
    ],
]

_CLAW_LINKEDIN_SIGNAL = {
    "applied": [
        {"action": "open_linkedin",   "duration_sec": _SHORT},
        {"action": "view_job",        "duration_sec": _MEDIUM},
        {"action": "apply_job",       "duration_sec": _LONG},
        {"action": "close_linkedin",  "duration_sec": _SHORT},
    ],
    "saved": [
        {"action": "open_linkedin",   "duration_sec": _SHORT},
        {"action": "browse_jobs",     "duration_sec": _MEDIUM},
        {"action": "save_job",        "duration_sec": _SHORT},
        {"action": "close_linkedin",  "duration_sec": _SHORT},
    ],
    "followed": [
        {"action": "open_linkedin",   "duration_sec": _SHORT},
        {"action": "view_company",    "duration_sec": _MEDIUM},
        {"action": "follow_company",  "duration_sec": _SHORT},
        {"action": "close_linkedin",  "duration_sec": _SHORT},
    ],
    "viewed": [
        {"action": "open_linkedin",   "duration_sec": _SHORT},
        {"action": "view_job",        "duration_sec": _MEDIUM},
        {"action": "close_linkedin",  "duration_sec": _SHORT},
    ],
}

_CLAW_LINKEDIN_NOISE = [
    [
        {"action": "open_linkedin",   "duration_sec": _SHORT},
        {"action": "browse_jobs",     "duration_sec": _MEDIUM},
        {"action": "close_linkedin",  "duration_sec": _SHORT},
    ],
    [
        {"action": "open_linkedin",   "duration_sec": _SHORT},
        {"action": "search_jobs",     "duration_sec": _MEDIUM},
        {"action": "close_linkedin",  "duration_sec": _SHORT},
    ],
]

_SHEET_SIGNAL = {
    "edited": [
        {"action": "open_sheet",      "duration_sec": _SHORT},
        {"action": "open_workbook",   "duration_sec": _SHORT},
        {"action": "edit_cells",      "duration_sec": _LONG},
        {"action": "close_sheet",     "duration_sec": _SHORT},
    ],
    "computed": [
        {"action": "open_sheet",      "duration_sec": _SHORT},
        {"action": "open_workbook",   "duration_sec": _SHORT},
        {"action": "compute_range",   "duration_sec": _MEDIUM},
        {"action": "close_sheet",     "duration_sec": _SHORT},
    ],
    "created": [
        {"action": "open_sheet",      "duration_sec": _SHORT},
        {"action": "create_workbook", "duration_sec": _MEDIUM},
        {"action": "close_sheet",     "duration_sec": _SHORT},
    ],
    "viewed": [
        {"action": "open_sheet",      "duration_sec": _SHORT},
        {"action": "view_workbook",   "duration_sec": _MEDIUM},
        {"action": "close_sheet",     "duration_sec": _SHORT},
    ],
}

_SHEET_NOISE = [
    [
        {"action": "open_sheet",      "duration_sec": _SHORT},
        {"action": "browse_workbooks","duration_sec": _MEDIUM},
        {"action": "close_sheet",     "duration_sec": _SHORT},
    ],
    [
        {"action": "open_sheet",      "duration_sec": _SHORT},
        {"action": "view_workbook",   "duration_sec": _MEDIUM},
        {"action": "close_sheet",     "duration_sec": _SHORT},
    ],
]

_SLIDE_SIGNAL = {
    "edited": [
        {"action": "open_slide",      "duration_sec": _SHORT},
        {"action": "open_deck",       "duration_sec": _SHORT},
        {"action": "edit_slide",      "duration_sec": _LONG},
        {"action": "close_slide",     "duration_sec": _SHORT},
    ],
    "added": [
        {"action": "open_slide",      "duration_sec": _SHORT},
        {"action": "open_deck",       "duration_sec": _SHORT},
        {"action": "add_slide",       "duration_sec": _MEDIUM},
        {"action": "close_slide",     "duration_sec": _SHORT},
    ],
    "created": [
        {"action": "open_slide",      "duration_sec": _SHORT},
        {"action": "create_deck",     "duration_sec": _MEDIUM},
        {"action": "close_slide",     "duration_sec": _SHORT},
    ],
    "viewed": [
        {"action": "open_slide",      "duration_sec": _SHORT},
        {"action": "view_deck",       "duration_sec": _MEDIUM},
        {"action": "close_slide",     "duration_sec": _SHORT},
    ],
}

_SLIDE_NOISE = [
    [
        {"action": "open_slide",      "duration_sec": _SHORT},
        {"action": "browse_decks",    "duration_sec": _MEDIUM},
        {"action": "close_slide",     "duration_sec": _SHORT},
    ],
    [
        {"action": "open_slide",      "duration_sec": _SHORT},
        {"action": "view_deck",       "duration_sec": _MEDIUM},
        {"action": "close_slide",     "duration_sec": _SHORT},
    ],
]

# ---------------------------------------------------------------------------
# Aggregate registry
# ---------------------------------------------------------------------------

ServiceTemplates = dict[str, Any]

SERVICE_ACTION_TEMPLATES: dict[str, ServiceTemplates] = {
    "gmail":     {"signal": _GMAIL_SIGNAL,     "noise": _GMAIL_NOISE},
    "calendar":  {"signal": _CALENDAR_SIGNAL,  "noise": _CALENDAR_NOISE},
    "contacts":  {"signal": _CONTACTS_SIGNAL,  "noise": _CONTACTS_NOISE},
    "todo":      {"signal": _TODO_SIGNAL,      "noise": _TODO_NOISE},
    "kb":        {"signal": _KB_SIGNAL,        "noise": _KB_NOISE},
    "notes":     {"signal": _NOTES_SIGNAL,     "noise": _NOTES_NOISE},
    "finance":   {"signal": _FINANCE_SIGNAL,   "noise": _FINANCE_NOISE},
    "inventory": {"signal": _INVENTORY_SIGNAL, "noise": _INVENTORY_NOISE},
    "helpdesk":  {"signal": _HELPDESK_SIGNAL,  "noise": _HELPDESK_NOISE},
    "crm":       {"signal": _CRM_SIGNAL,       "noise": _CRM_NOISE},
    "scheduler": {"signal": _SCHEDULER_SIGNAL, "noise": _SCHEDULER_NOISE},
    "rss":       {"signal": _RSS_SIGNAL,       "noise": _RSS_NOISE},
    "config":    {"signal": _CONFIG_SIGNAL,    "noise": _CONFIG_NOISE},
    "workmail":      {"signal": _WORKMAIL_SIGNAL,      "noise": _WORKMAIL_NOISE},
    "claw_notion":   {"signal": _CLAW_NOTION_SIGNAL,   "noise": _CLAW_NOTION_NOISE},
    "claw_obsidian": {"signal": _CLAW_OBSIDIAN_SIGNAL, "noise": _CLAW_OBSIDIAN_NOISE},
    "meeting":       {"signal": _MEETING_SIGNAL,       "noise": _MEETING_NOISE},
    "claw_zotero":   {"signal": _CLAW_ZOTERO_SIGNAL,   "noise": _CLAW_ZOTERO_NOISE},
    "stumail":       {"signal": _STUMAIL_SIGNAL,       "noise": _STUMAIL_NOISE},
    "claw_feishu":   {"signal": _CLAW_FEISHU_SIGNAL,   "noise": _CLAW_FEISHU_NOISE},
    "claw_slack":    {"signal": _CLAW_SLACK_SIGNAL,    "noise": _CLAW_SLACK_NOISE},
    "claw_facebook": {"signal": _CLAW_FACEBOOK_SIGNAL, "noise": _CLAW_FACEBOOK_NOISE},
    "claw_zhihu":    {"signal": _CLAW_ZHIHU_SIGNAL,    "noise": _CLAW_ZHIHU_NOISE},
    "claw_wechat":   {"signal": _CLAW_WECHAT_SIGNAL,   "noise": _CLAW_WECHAT_NOISE},
    "claw_qq":       {"signal": _CLAW_QQ_SIGNAL,       "noise": _CLAW_QQ_NOISE},
    "claw_linkedin": {"signal": _CLAW_LINKEDIN_SIGNAL, "noise": _CLAW_LINKEDIN_NOISE},
    "sheet":         {"signal": _SHEET_SIGNAL,         "noise": _SHEET_NOISE},
    "slide":         {"signal": _SLIDE_SIGNAL,         "noise": _SLIDE_NOISE},
}

# ---------------------------------------------------------------------------
# Record classifier: given a service name and a fixture record dict,
# return the signal pattern key (e.g. "sent", "received", "created").
# ---------------------------------------------------------------------------


def classify_record(service: str, record: dict, persona_email: str | None = None) -> str:
    """Return the signal pattern key for a fixture record.

    The classification is heuristic and service-specific.  Falls back to a
    sensible default when the record doesn't match any specific rule.
    """
    if service == "gmail":
        labels = [l.upper() for l in record.get("labels", [])]
        if "DRAFT" in labels:
            return "draft"
        # Explicit folder label is the most reliable signal of authorship.
        if "SENT" in labels:
            return "sent"
        sender = record.get("from", "")
        if persona_email and persona_email.lower() in sender.lower():
            return "sent"
        # Check if it looks like a reply (has In-Reply-To or Re: in subject)
        subject = record.get("subject", "")
        if subject.lower().startswith("re:"):
            return "reply"
        return "received"

    if service == "calendar":
        # If the persona is the organizer it was created, otherwise viewed/accepted.
        organizer = record.get("organizer", "")
        if persona_email and persona_email.lower() in str(organizer).lower():
            return "created"
        # Generated fixtures almost never populate ``organizer``; the LLM lists
        # the creator as the *first* attendee instead, so honor that convention.
        if persona_email:
            attendees = record.get("attendees") or []
            if isinstance(attendees, (list, tuple)) and attendees:
                first = attendees[0]
                if isinstance(first, dict):
                    first = first.get("email") or first.get("address") or ""
                if isinstance(first, str) and persona_email.lower() in first.lower():
                    return "created"
        return "viewed"

    if service == "contacts":
        return "added"

    if service == "todo":
        status = record.get("status", "").lower()
        if status in ("completed", "done"):
            return "completed"
        return "created"

    if service == "kb":
        return "read"

    if service == "notes":
        return "created"

    if service == "finance":
        return "recorded"

    if service == "inventory":
        return "created"

    if service == "helpdesk":
        status = record.get("status", "").lower()
        if status in ("closed", "resolved"):
            return "updated"
        return "created"

    if service == "crm":
        return "created"

    if service == "scheduler":
        return "created"

    if service == "rss":
        return "read"

    if service == "config":
        return "viewed"

    if service == "workmail":
        labels = [l.upper() for l in record.get("labels", [])]
        if "SENT" in labels:
            return "sent"
        sender = record.get("from", "")
        if persona_email and persona_email.lower() in sender.lower():
            return "sent"
        subject = record.get("subject", "")
        if subject.lower().startswith("re:"):
            return "reply"
        return "received"

    if service == "meeting":
        status = (record.get("status") or "").lower()
        if status in ("cancelled", "canceled"):
            return "cancelled"
        if status == "ended":
            return "ended"
        if status == "started":
            return "started"
        return "created"

    if service == "claw_notion":
        if record.get("database_id"):
            return "database_added"
        return "created"

    if service == "claw_obsidian":
        if record.get("outlinks"):
            return "linked"
        return "created"

    if service == "claw_zotero":
        if record.get("notes"):
            return "noted"
        if record.get("collection_ids"):
            return "collected"
        return "added"

    if service == "stumail":
        sender = (record.get("from") or "").lower()
        if persona_email and persona_email.lower() in sender:
            return "sent"
        # A message from someone else is incoming mail, even when it is a reply
        # (``reply_to_message_id`` set). Only treat a senderless reply as the
        # persona's unsent draft.
        if not sender and record.get("reply_to_message_id"):
            return "draft"
        return "received"

    if service == "claw_feishu":
        if record.get("meetings"):
            return "meeting_scheduled"
        if record.get("docs"):
            return "doc_created"
        return "messaged"

    if service == "claw_slack":
        messages = record.get("messages", [])
        if any(m.get("parent_message_id") for m in messages):
            return "replied"
        if any(m.get("reactions") for m in messages):
            return "reacted"
        return "posted"

    if service == "claw_facebook":
        if record.get("reposted"):
            return "reposted"
        if record.get("saved"):
            return "saved"
        if record.get("liked"):
            return "liked"
        return "viewed"

    if service == "claw_zhihu":
        if record.get("answers"):
            return "answered"
        if record.get("followed"):
            return "followed"
        return "browsed"

    if service in ("claw_wechat", "claw_qq"):
        if record.get("muted"):
            return "muted"
        if record.get("type") == "moments" or record.get("type") == "zone":
            return "moment_posted" if service == "claw_wechat" else "zone_posted"
        return "messaged"

    if service == "claw_linkedin":
        if record.get("applied"):
            return "applied"
        if record.get("saved"):
            return "saved"
        if record.get("followed"):
            return "followed"
        return "viewed"

    if service == "sheet":
        sheets = record.get("sheets", [])
        if any(s.get("formulas") for s in sheets):
            return "computed"
        if sheets:
            return "edited"
        return "created"

    if service == "slide":
        slides = record.get("slides", [])
        if any(s.get("elements") for s in slides):
            return "edited"
        if slides:
            return "added"
        return "created"

    # Fallback: pick the first signal key available
    templates = SERVICE_ACTION_TEMPLATES.get(service, {}).get("signal", {})
    if templates:
        return next(iter(templates))
    return "viewed"
