"""Mock Stumail API service (student/campus email) for agent evaluation (FastAPI on port 9125)."""

from __future__ import annotations

import json
import copy
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from fastapi import FastAPI
from pydantic import BaseModel, Field

app = FastAPI(title="Mock Stumail API")

from mock_services._base import add_error_injection, frozen_now
add_error_injection(app)

FIXTURES_PATH = Path(os.environ.get(
    "STUMAIL_FIXTURES",
    str(Path(__file__).resolve().parent.parent.parent / "new_tasks" / "_service_smoke" / "fixtures" / "stumail" / "inbox.json"),
))

# In-memory state
_emails: list[dict[str, Any]] = []
_audit_log: list[dict[str, Any]] = []
_sent_messages: list[dict[str, Any]] = []
_drafts: list[dict[str, Any]] = []


def _load_fixtures() -> None:
    """Load email fixtures verbatim.

    Fixture dates are absolute and intentionally not shifted — reproducibility
    requires identical content regardless of wall clock. The list endpoint
    derives its cutoff from EXECUTION_DATE (via frozen_now()).
    """
    global _emails
    with open(FIXTURES_PATH) as f:
        _emails = json.load(f)


# Load on startup
_load_fixtures()


def _log_call(endpoint: str, request_body: dict[str, Any], response_body: Any) -> None:
    _audit_log.append({
        "endpoint": endpoint,
        "request_body": request_body,
        "response_body": response_body,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


# --- Request/Response models ---


class ListMessagesRequest(BaseModel):
    days_back: int = 7
    max_results: int = 20
    offset: int = 0
    query: str | None = None


class GetMessageRequest(BaseModel):
    message_id: str


class SendMessageRequest(BaseModel):
    to: str
    subject: str
    body: str


class SaveDraftRequest(BaseModel):
    to: str
    subject: str
    body: str
    reply_to_message_id: str | None = None


# --- Endpoints ---


@app.post("/stumail/messages")
def list_messages(req: ListMessagesRequest | None = None) -> dict[str, Any]:
    """List emails from inbox, filtered by recency."""
    if req is None:
        req = ListMessagesRequest()

    cutoff = frozen_now() - timedelta(days=req.days_back)
    filtered = []
    for email in _emails:
        email_date = datetime.fromisoformat(email["date"].replace("Z", "+00:00"))
        if email_date >= cutoff:
            # keyword search in subject and from fields (all query words must appear)
            if req.query:
                searchable = (email.get("subject", "") + " " + email.get("from", "")).lower()
                query_words = req.query.lower().split()
                if not all(w in searchable for w in query_words):
                    continue
            filtered.append({
                "message_id": email["message_id"],
                "from": email["from"],
                "subject": email["subject"],
                "date": email["date"],
                "is_read": email["is_read"],
                "labels": email["labels"],
            })
    total = len(filtered)
    results = filtered[req.offset: req.offset + req.max_results]

    resp = {"messages": results, "total": total, "returned": len(results), "offset": req.offset}
    _log_call("/stumail/messages", req.model_dump(), resp)
    return resp


@app.post("/stumail/messages/get")
def get_message(req: GetMessageRequest) -> dict[str, Any]:
    """Get a single email by message_id."""
    for email in _emails:
        if email["message_id"] == req.message_id:
            resp = copy.deepcopy(email)
            _log_call("/stumail/messages/get", req.model_dump(), resp)
            return resp

    resp = {"error": f"Message {req.message_id} not found"}
    _log_call("/stumail/messages/get", req.model_dump(), resp)
    return resp


@app.post("/stumail/send")
def send_message(req: SendMessageRequest) -> dict[str, Any]:
    """Send an email (recorded for audit)."""
    msg = {
        "to": req.to,
        "subject": req.subject,
        "body": req.body,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _sent_messages.append(msg)
    resp = {"status": "sent", "message": msg}
    _log_call("/stumail/send", req.model_dump(), resp)
    return resp


@app.post("/stumail/drafts/save")
def save_draft(req: SaveDraftRequest) -> dict[str, Any]:
    """Save an email as draft (not sent)."""
    draft = {
        "to": req.to,
        "subject": req.subject,
        "body": req.body,
        "reply_to_message_id": req.reply_to_message_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _drafts.append(draft)
    resp = {"status": "draft_saved", "draft": draft}
    _log_call("/stumail/drafts/save", req.model_dump(), resp)
    return resp


@app.get("/stumail/audit")
def get_audit() -> dict[str, Any]:
    """Return all API calls for grader inspection."""
    return {
        "calls": _audit_log,
        "sent_messages": _sent_messages,
        "drafts": _drafts,
    }


@app.post("/stumail/reset")
def reset_state() -> dict[str, str]:
    """Reset state between trials."""
    global _audit_log, _sent_messages, _drafts
    _audit_log = []
    _sent_messages = []
    _drafts = []
    _load_fixtures()
    return {"status": "reset"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "9125")))
