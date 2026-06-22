"""Mock Claw-Feishu API service (group x (messages + meetings + docs)) for agent evaluation (FastAPI on port 9126).

Model:
- A group is the first-class entity containing members, owner, announcement,
  and three child collections: messages, meetings, docs.
- send_message appends to group.messages; schedule_meeting appends to group.meetings;
  end_meeting flips meeting.status to "ended" (irreversible).
- create_doc appends a doc; update_doc overwrites doc.content.
- meeting.minutes is a read-only system-transcript field (fixture may fill it).
"""

from __future__ import annotations

import json
import copy
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from fastapi import FastAPI
from pydantic import BaseModel, Field

app = FastAPI(title="Mock Claw-Feishu API")

from mock_services._base import add_error_injection, frozen_now
add_error_injection(app)

FIXTURES_PATH = Path(os.environ.get(
    "CLAW_FEISHU_FIXTURES",
    str(Path(__file__).resolve().parent.parent.parent / "new_tasks" / "_service_smoke" / "fixtures" / "claw_feishu" / "workspace.json"),
))

_groups: list[dict[str, Any]] = []
_audit_log: list[dict[str, Any]] = []
_sent_messages: list[dict[str, Any]] = []
_scheduled_meetings: list[dict[str, Any]] = []
_ended_meetings: list[dict[str, Any]] = []
_created_docs: list[dict[str, Any]] = []
_updated_docs: list[dict[str, Any]] = []


def _load_fixtures() -> None:
    global _groups
    with open(FIXTURES_PATH) as f:
        _groups = json.load(f)


_load_fixtures()


def _log_call(endpoint: str, request_body: dict[str, Any], response_body: Any) -> None:
    _audit_log.append({
        "endpoint": endpoint,
        "request_body": request_body,
        "response_body": response_body,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


GROUP_PREFIX = "FSGRP-"
MSG_PREFIX = "FSMSG-"
MEETING_PREFIX = "FSMTG-"
DOC_PREFIX = "FSDOC-"
ID_PAD = 4


def _allocate_id(prefix: str, existing_ids: list[str]) -> str:
    max_num = 0
    for eid in existing_ids:
        if eid.startswith(prefix):
            try:
                n = int(eid[len(prefix):])
                if n > max_num:
                    max_num = n
            except ValueError:
                pass
    return f"{prefix}{(max_num + 1):0{ID_PAD}d}"


def _all_message_ids() -> list[str]:
    ids: list[str] = []
    for g in _groups:
        for m in g.get("messages", []):
            # Fixtures key the message identifier under ``id``; server-allocated
            # messages use ``message_id``. Consider both so the allocator never
            # collides with an existing fixture message.
            ids.append(m.get("message_id") or m.get("id") or "")
    return ids


def _all_meeting_ids() -> list[str]:
    ids: list[str] = []
    for g in _groups:
        for m in g.get("meetings", []):
            ids.append(m.get("meeting_id", ""))
    return ids


def _all_doc_ids() -> list[str]:
    ids: list[str] = []
    for g in _groups:
        for d in g.get("docs", []):
            ids.append(d.get("doc_id", ""))
    return ids


def _find_group(group_id: str) -> dict[str, Any] | None:
    for g in _groups:
        if g.get("group_id") == group_id:
            return g
    return None


# --- Request models ---


class ListGroupsRequest(BaseModel):
    member: str | None = None
    include_archived: bool = False
    max_results: int = 20
    offset: int = 0


class GetGroupRequest(BaseModel):
    group_id: str
    include_messages: bool = True


class SendMessageRequest(BaseModel):
    group_id: str
    sender: str
    text: str
    mentions: list[str] = Field(default_factory=list)
    pinned: bool = False


class SearchMessagesRequest(BaseModel):
    query: str
    max_results: int = 10
    offset: int = 0


class ScheduleMeetingRequest(BaseModel):
    group_id: str
    title: str
    start_time: str
    duration_minutes: int = 30
    attendees: list[str] = Field(default_factory=list)


class EndMeetingRequest(BaseModel):
    meeting_id: str


class ListDocsRequest(BaseModel):
    group_id: str | None = None
    max_results: int = 20
    offset: int = 0


class CreateDocRequest(BaseModel):
    group_id: str
    title: str
    content: str = ""
    author: str


class UpdateDocRequest(BaseModel):
    doc_id: str
    content: str


# --- Endpoints ---


def _group_summary(g: dict[str, Any], include_messages: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "group_id": g.get("group_id"),
        "name": g.get("name"),
        "members": g.get("members", []),
        "owner": g.get("owner"),
        "announcement": g.get("announcement", ""),
        "archived": g.get("archived", False),
        "message_count": len(g.get("messages", [])),
        "meeting_count": len(g.get("meetings", [])),
        "doc_count": len(g.get("docs", [])),
        "last_updated": g.get("last_updated"),
    }
    if include_messages:
        out["messages"] = copy.deepcopy(g.get("messages", []))
        out["meetings"] = copy.deepcopy(g.get("meetings", []))
    return out


@app.post("/claw_feishu/groups")
def list_groups(req: ListGroupsRequest | None = None) -> dict[str, Any]:
    if req is None:
        req = ListGroupsRequest()
    filtered: list[dict[str, Any]] = []
    for g in _groups:
        if not req.include_archived and g.get("archived", False):
            continue
        if req.member is not None and req.member not in g.get("members", []):
            continue
        filtered.append(_group_summary(g))
    total = len(filtered)
    results = filtered[req.offset: req.offset + req.max_results]
    resp = {"groups": results, "total": total, "returned": len(results), "offset": req.offset}
    _log_call("/claw_feishu/groups", req.model_dump(), resp)
    return resp


@app.post("/claw_feishu/groups/get")
def get_group(req: GetGroupRequest) -> dict[str, Any]:
    g = _find_group(req.group_id)
    if g is None:
        resp = {"error": f"Group {req.group_id} not found"}
        _log_call("/claw_feishu/groups/get", req.model_dump(), resp)
        return resp
    resp = _group_summary(g, include_messages=req.include_messages)
    _log_call("/claw_feishu/groups/get", req.model_dump(), resp)
    return resp


@app.post("/claw_feishu/messages/send")
def send_message(req: SendMessageRequest) -> dict[str, Any]:
    g = _find_group(req.group_id)
    if g is None:
        resp = {"error": f"Group {req.group_id} not found"}
        _log_call("/claw_feishu/messages/send", req.model_dump(), resp)
        return resp
    new_id = _allocate_id(MSG_PREFIX, _all_message_ids())
    now = frozen_now().isoformat()
    msg = {
        "message_id": new_id,
        "sender": req.sender,
        "text": req.text,
        "mentions": list(req.mentions),
        "pinned": req.pinned,
        "timestamp": now,
    }
    g.setdefault("messages", []).append(msg)
    g["last_updated"] = now
    record = {
        "message_id": new_id,
        "group_id": req.group_id,
        "sender": req.sender,
        "mentions": list(req.mentions),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _sent_messages.append(record)
    resp = {"status": "sent", "message": copy.deepcopy(msg)}
    _log_call("/claw_feishu/messages/send", req.model_dump(), resp)
    return resp


@app.post("/claw_feishu/messages/search")
def search_messages(req: SearchMessagesRequest) -> dict[str, Any]:
    if not req.query.strip():
        resp = {"matches": [], "total": 0, "returned": 0, "offset": req.offset}
        _log_call("/claw_feishu/messages/search", req.model_dump(), resp)
        return resp
    q = req.query.lower()
    matches: list[dict[str, Any]] = []
    for g in _groups:
        if g.get("archived", False):
            continue
        for m in g.get("messages", []):
            text = (m.get("text") or "").lower()
            if q in text:
                matches.append({
                    "group_id": g.get("group_id"),
                    "group_name": g.get("name"),
                    "message_id": m.get("message_id") or m.get("id"),
                    "sender": m.get("sender"),
                    "text": m.get("text"),
                    "timestamp": m.get("timestamp"),
                })
    total = len(matches)
    results = matches[req.offset: req.offset + req.max_results]
    resp = {"matches": results, "total": total, "returned": len(results), "offset": req.offset}
    _log_call("/claw_feishu/messages/search", req.model_dump(), resp)
    return resp


@app.post("/claw_feishu/meetings/schedule")
def schedule_meeting(req: ScheduleMeetingRequest) -> dict[str, Any]:
    g = _find_group(req.group_id)
    if g is None:
        resp = {"error": f"Group {req.group_id} not found"}
        _log_call("/claw_feishu/meetings/schedule", req.model_dump(), resp)
        return resp
    new_id = _allocate_id(MEETING_PREFIX, _all_meeting_ids())
    now = frozen_now().isoformat()
    meeting = {
        "meeting_id": new_id,
        "title": req.title,
        "start_time": req.start_time,
        "duration_minutes": req.duration_minutes,
        "attendees": list(req.attendees),
        "status": "scheduled",
        "minutes": "",
        "created_at": now,
    }
    g.setdefault("meetings", []).append(meeting)
    g["last_updated"] = now
    record = {
        "meeting_id": new_id,
        "group_id": req.group_id,
        "title": req.title,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _scheduled_meetings.append(record)
    resp = {"status": "scheduled", "meeting": copy.deepcopy(meeting)}
    _log_call("/claw_feishu/meetings/schedule", req.model_dump(), resp)
    return resp


@app.post("/claw_feishu/meetings/end")
def end_meeting(req: EndMeetingRequest) -> dict[str, Any]:
    for g in _groups:
        for m in g.get("meetings", []):
            if m.get("meeting_id") == req.meeting_id:
                if m.get("status") == "ended":
                    resp = {"error": f"Meeting {req.meeting_id} already ended"}
                    _log_call("/claw_feishu/meetings/end", req.model_dump(), resp)
                    return resp
                m["status"] = "ended"
                m["ended_at"] = frozen_now().isoformat()
                g["last_updated"] = frozen_now().isoformat()
                record = {
                    "meeting_id": req.meeting_id,
                    "group_id": g.get("group_id"),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                _ended_meetings.append(record)
                resp = {"status": "ended", "meeting_id": req.meeting_id, "group_id": g.get("group_id")}
                _log_call("/claw_feishu/meetings/end", req.model_dump(), resp)
                return resp
    resp = {"error": f"Meeting {req.meeting_id} not found"}
    _log_call("/claw_feishu/meetings/end", req.model_dump(), resp)
    return resp


@app.post("/claw_feishu/docs")
def list_docs(req: ListDocsRequest | None = None) -> dict[str, Any]:
    if req is None:
        req = ListDocsRequest()
    matches: list[dict[str, Any]] = []
    for g in _groups:
        if g.get("archived", False):
            continue
        if req.group_id is not None and g.get("group_id") != req.group_id:
            continue
        for d in g.get("docs", []):
            matches.append({
                "doc_id": d.get("doc_id"),
                "group_id": g.get("group_id"),
                "title": d.get("title"),
                "author": d.get("author"),
                "last_updated": d.get("last_updated"),
            })
    total = len(matches)
    results = matches[req.offset: req.offset + req.max_results]
    resp = {"docs": results, "total": total, "returned": len(results), "offset": req.offset}
    _log_call("/claw_feishu/docs", req.model_dump(), resp)
    return resp


@app.post("/claw_feishu/docs/create")
def create_doc(req: CreateDocRequest) -> dict[str, Any]:
    g = _find_group(req.group_id)
    if g is None:
        resp = {"error": f"Group {req.group_id} not found"}
        _log_call("/claw_feishu/docs/create", req.model_dump(), resp)
        return resp
    new_id = _allocate_id(DOC_PREFIX, _all_doc_ids())
    now = frozen_now().isoformat()
    doc = {
        "doc_id": new_id,
        "title": req.title,
        "content": req.content,
        "author": req.author,
        "created_at": now,
        "last_updated": now,
    }
    g.setdefault("docs", []).append(doc)
    g["last_updated"] = now
    record = {
        "doc_id": new_id,
        "group_id": req.group_id,
        "title": req.title,
        "author": req.author,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _created_docs.append(record)
    resp = {"status": "created", "doc": copy.deepcopy(doc)}
    _log_call("/claw_feishu/docs/create", req.model_dump(), resp)
    return resp


@app.post("/claw_feishu/docs/update")
def update_doc(req: UpdateDocRequest) -> dict[str, Any]:
    for g in _groups:
        for d in g.get("docs", []):
            if d.get("doc_id") == req.doc_id:
                now = frozen_now().isoformat()
                d["content"] = req.content
                d["last_updated"] = now
                g["last_updated"] = now
                record = {
                    "doc_id": req.doc_id,
                    "group_id": g.get("group_id"),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                _updated_docs.append(record)
                resp = {"status": "updated", "doc_id": req.doc_id, "group_id": g.get("group_id")}
                _log_call("/claw_feishu/docs/update", req.model_dump(), resp)
                return resp
    resp = {"error": f"Doc {req.doc_id} not found"}
    _log_call("/claw_feishu/docs/update", req.model_dump(), resp)
    return resp


@app.get("/claw_feishu/audit")
def get_audit() -> dict[str, Any]:
    return {
        "calls": _audit_log,
        "sent_messages": _sent_messages,
        "scheduled_meetings": _scheduled_meetings,
        "ended_meetings": _ended_meetings,
        "created_docs": _created_docs,
        "updated_docs": _updated_docs,
    }


@app.post("/claw_feishu/reset")
def reset_state() -> dict[str, str]:
    global _audit_log, _sent_messages, _scheduled_meetings, _ended_meetings, _created_docs, _updated_docs
    _audit_log = []
    _sent_messages = []
    _scheduled_meetings = []
    _ended_meetings = []
    _created_docs = []
    _updated_docs = []
    _load_fixtures()
    return {"status": "reset"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "9126")))
