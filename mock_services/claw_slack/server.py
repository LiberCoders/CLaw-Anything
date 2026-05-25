"""Mock Claw-Slack API service (channel + thread + reaction) for agent evaluation (FastAPI on port 9127).

Model:
- A channel is the first-class entity. Messages live inside channels.
- Messages with parent_message_id=null are main messages (visible in channel view).
- Messages with parent_message_id set are thread replies (one level deep; cannot reply to a reply).
- Reactions are per-message emoji dicts: {":emoji:": [user_ids]}.
- Pins are channel-level: channel.pinned_message_ids.
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

app = FastAPI(title="Mock Claw-Slack API")

from mock_services._base import add_error_injection, frozen_now
add_error_injection(app)

FIXTURES_PATH = Path(os.environ.get(
    "CLAW_SLACK_FIXTURES",
    str(Path(__file__).resolve().parent.parent.parent / "new_tasks" / "_service_smoke" / "fixtures" / "claw_slack" / "workspace.json"),
))

_channels: list[dict[str, Any]] = []
_audit_log: list[dict[str, Any]] = []
_posted_messages: list[dict[str, Any]] = []
_reactions_added: list[dict[str, Any]] = []
_reactions_removed: list[dict[str, Any]] = []
_pinned: list[dict[str, Any]] = []
_unpinned: list[dict[str, Any]] = []


def _load_fixtures() -> None:
    global _channels
    with open(FIXTURES_PATH) as f:
        _channels = json.load(f)


_load_fixtures()


def _log_call(endpoint: str, request_body: dict[str, Any], response_body: Any) -> None:
    _audit_log.append({
        "endpoint": endpoint,
        "request_body": request_body,
        "response_body": response_body,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


CHANNEL_PREFIX = "SLKC-"
MSG_PREFIX = "SLKM-"
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
    for ch in _channels:
        for m in ch.get("messages", []):
            ids.append(m.get("message_id", ""))
    return ids


def _find_channel(channel_id: str) -> dict[str, Any] | None:
    for ch in _channels:
        if ch.get("channel_id") == channel_id:
            return ch
    return None


def _find_message_in_channel(channel: dict[str, Any], message_id: str) -> dict[str, Any] | None:
    for m in channel.get("messages", []):
        if m.get("message_id") == message_id:
            return m
    return None


def _find_message_globally(message_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    for ch in _channels:
        for m in ch.get("messages", []):
            if m.get("message_id") == message_id:
                return ch, m
    return None, None


# --- Request models ---


class ListChannelsRequest(BaseModel):
    member: str | None = None
    is_private: bool | None = None
    include_archived: bool = False
    max_results: int = 20
    offset: int = 0


class GetChannelRequest(BaseModel):
    channel_id: str


class ListMessagesRequest(BaseModel):
    channel_id: str
    max_results: int = 20
    offset: int = 0


class GetThreadRequest(BaseModel):
    parent_message_id: str


class PostMessageRequest(BaseModel):
    channel_id: str
    sender: str
    text: str
    parent_message_id: str | None = None


class SearchMessagesRequest(BaseModel):
    query: str
    by_user: str | None = None
    after: str | None = None
    before: str | None = None
    max_results: int = 20
    offset: int = 0


class AddReactionRequest(BaseModel):
    message_id: str
    emoji: str
    user: str


class RemoveReactionRequest(BaseModel):
    message_id: str
    emoji: str
    user: str


class PinMessageRequest(BaseModel):
    channel_id: str
    message_id: str


class UnpinMessageRequest(BaseModel):
    channel_id: str
    message_id: str


# --- Helpers ---


def _channel_summary(ch: dict[str, Any]) -> dict[str, Any]:
    messages = ch.get("messages", [])
    main_messages = [m for m in messages if m.get("parent_message_id") is None]
    return {
        "channel_id": ch.get("channel_id"),
        "name": ch.get("name"),
        "is_private": ch.get("is_private", False),
        "topic": ch.get("topic", ""),
        "purpose": ch.get("purpose", ""),
        "members": ch.get("members", []),
        "archived": ch.get("archived", False),
        "message_count": len(main_messages),
        "pinned_count": len(ch.get("pinned_message_ids", [])),
        "last_updated": ch.get("last_updated"),
    }


# --- Endpoints ---


@app.post("/claw_slack/channels")
def list_channels(req: ListChannelsRequest | None = None) -> dict[str, Any]:
    if req is None:
        req = ListChannelsRequest()
    filtered: list[dict[str, Any]] = []
    for ch in _channels:
        if not req.include_archived and ch.get("archived", False):
            continue
        if req.member is not None and req.member not in ch.get("members", []):
            continue
        if req.is_private is not None and ch.get("is_private", False) != req.is_private:
            continue
        filtered.append(_channel_summary(ch))
    total = len(filtered)
    results = filtered[req.offset: req.offset + req.max_results]
    resp = {"channels": results, "total": total, "returned": len(results), "offset": req.offset}
    _log_call("/claw_slack/channels", req.model_dump(), resp)
    return resp


@app.post("/claw_slack/channels/get")
def get_channel(req: GetChannelRequest) -> dict[str, Any]:
    ch = _find_channel(req.channel_id)
    if ch is None:
        resp = {"error": f"Channel {req.channel_id} not found"}
        _log_call("/claw_slack/channels/get", req.model_dump(), resp)
        return resp
    main_messages = [m for m in ch.get("messages", []) if m.get("parent_message_id") is None]
    resp = {
        "channel_id": ch.get("channel_id"),
        "name": ch.get("name"),
        "is_private": ch.get("is_private", False),
        "topic": ch.get("topic", ""),
        "purpose": ch.get("purpose", ""),
        "members": ch.get("members", []),
        "pinned_message_ids": ch.get("pinned_message_ids", []),
        "archived": ch.get("archived", False),
        "created_at": ch.get("created_at"),
        "last_updated": ch.get("last_updated"),
        "message_count": len(main_messages),
    }
    _log_call("/claw_slack/channels/get", req.model_dump(), resp)
    return resp


@app.post("/claw_slack/messages/list")
def list_messages(req: ListMessagesRequest) -> dict[str, Any]:
    ch = _find_channel(req.channel_id)
    if ch is None:
        resp = {"error": f"Channel {req.channel_id} not found"}
        _log_call("/claw_slack/messages/list", req.model_dump(), resp)
        return resp
    all_messages = ch.get("messages", [])
    main_messages = [copy.deepcopy(m) for m in all_messages if m.get("parent_message_id") is None]
    for mm in main_messages:
        mid = mm.get("message_id")
        mm["reply_count"] = sum(1 for m in all_messages if m.get("parent_message_id") == mid)
    total = len(main_messages)
    results = main_messages[req.offset: req.offset + req.max_results]
    resp = {"messages": results, "total": total, "returned": len(results), "offset": req.offset}
    _log_call("/claw_slack/messages/list", req.model_dump(), resp)
    return resp


@app.post("/claw_slack/messages/thread")
def get_thread(req: GetThreadRequest) -> dict[str, Any]:
    ch_found, root_msg = _find_message_globally(req.parent_message_id)
    if root_msg is None:
        resp = {"error": f"Message {req.parent_message_id} not found"}
        _log_call("/claw_slack/messages/thread", req.model_dump(), resp)
        return resp
    if root_msg.get("parent_message_id") is not None:
        resp = {"error": "not a thread root"}
        _log_call("/claw_slack/messages/thread", req.model_dump(), resp)
        return resp
    replies = [
        copy.deepcopy(m) for m in ch_found.get("messages", [])
        if m.get("parent_message_id") == req.parent_message_id
    ]
    resp = {"root": copy.deepcopy(root_msg), "replies": replies, "reply_count": len(replies)}
    _log_call("/claw_slack/messages/thread", req.model_dump(), resp)
    return resp


@app.post("/claw_slack/messages/post")
def post_message(req: PostMessageRequest) -> dict[str, Any]:
    ch = _find_channel(req.channel_id)
    if ch is None:
        resp = {"error": f"Channel {req.channel_id} not found"}
        _log_call("/claw_slack/messages/post", req.model_dump(), resp)
        return resp
    if req.parent_message_id is not None:
        parent = _find_message_in_channel(ch, req.parent_message_id)
        if parent is None:
            resp = {"error": "parent message not found"}
            _log_call("/claw_slack/messages/post", req.model_dump(), resp)
            return resp
        if parent.get("parent_message_id") is not None:
            resp = {"error": "cannot reply to a thread reply; threads are one level deep"}
            _log_call("/claw_slack/messages/post", req.model_dump(), resp)
            return resp
    new_id = _allocate_id(MSG_PREFIX, _all_message_ids())
    now = frozen_now().isoformat()
    msg = {
        "message_id": new_id,
        "sender": req.sender,
        "text": req.text,
        "parent_message_id": req.parent_message_id,
        "reactions": {},
        "timestamp": now,
    }
    ch.setdefault("messages", []).append(msg)
    ch["last_updated"] = now
    record = {
        "message_id": new_id,
        "channel_id": req.channel_id,
        "sender": req.sender,
        "parent_message_id": req.parent_message_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _posted_messages.append(record)
    resp = {"status": "posted", "message": copy.deepcopy(msg)}
    _log_call("/claw_slack/messages/post", req.model_dump(), resp)
    return resp


@app.post("/claw_slack/messages/search")
def search_messages(req: SearchMessagesRequest) -> dict[str, Any]:
    if not req.query.strip():
        resp = {"matches": [], "total": 0, "returned": 0, "offset": req.offset}
        _log_call("/claw_slack/messages/search", req.model_dump(), resp)
        return resp
    q = req.query.lower()
    matches: list[dict[str, Any]] = []
    for ch in _channels:
        if ch.get("archived", False):
            continue
        for m in ch.get("messages", []):
            if q not in (m.get("text") or "").lower():
                continue
            if req.by_user is not None and m.get("sender") != req.by_user:
                continue
            ts = m.get("timestamp", "")
            if req.after is not None and ts < req.after:
                continue
            if req.before is not None and ts > req.before:
                continue
            matches.append({
                "channel_id": ch.get("channel_id"),
                "channel_name": ch.get("name"),
                "message_id": m.get("message_id"),
                "sender": m.get("sender"),
                "text": m.get("text"),
                "parent_message_id": m.get("parent_message_id"),
                "timestamp": m.get("timestamp"),
            })
    total = len(matches)
    results = matches[req.offset: req.offset + req.max_results]
    resp = {"matches": results, "total": total, "returned": len(results), "offset": req.offset}
    _log_call("/claw_slack/messages/search", req.model_dump(), resp)
    return resp


@app.post("/claw_slack/reactions/add")
def add_reaction(req: AddReactionRequest) -> dict[str, Any]:
    _ch, msg = _find_message_globally(req.message_id)
    if msg is None:
        resp = {"error": f"Message {req.message_id} not found"}
        _log_call("/claw_slack/reactions/add", req.model_dump(), resp)
        return resp
    reactions = msg.setdefault("reactions", {})
    users = reactions.setdefault(req.emoji, [])
    if req.user not in users:
        users.append(req.user)
    record = {
        "message_id": req.message_id,
        "emoji": req.emoji,
        "user": req.user,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _reactions_added.append(record)
    resp = {"status": "added", "message_id": req.message_id, "emoji": req.emoji, "user": req.user}
    _log_call("/claw_slack/reactions/add", req.model_dump(), resp)
    return resp


@app.post("/claw_slack/reactions/remove")
def remove_reaction(req: RemoveReactionRequest) -> dict[str, Any]:
    _ch, msg = _find_message_globally(req.message_id)
    if msg is None:
        resp = {"error": f"Message {req.message_id} not found"}
        _log_call("/claw_slack/reactions/remove", req.model_dump(), resp)
        return resp
    reactions = msg.get("reactions", {})
    users = reactions.get(req.emoji, [])
    if req.user not in users:
        resp = {"status": "noop"}
        _log_call("/claw_slack/reactions/remove", req.model_dump(), resp)
        return resp
    users.remove(req.user)
    if not users:
        reactions.pop(req.emoji, None)
    record = {
        "message_id": req.message_id,
        "emoji": req.emoji,
        "user": req.user,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _reactions_removed.append(record)
    resp = {"status": "removed", "message_id": req.message_id, "emoji": req.emoji, "user": req.user}
    _log_call("/claw_slack/reactions/remove", req.model_dump(), resp)
    return resp


@app.post("/claw_slack/pins/add")
def pin_message(req: PinMessageRequest) -> dict[str, Any]:
    ch = _find_channel(req.channel_id)
    if ch is None:
        resp = {"error": f"Channel {req.channel_id} not found"}
        _log_call("/claw_slack/pins/add", req.model_dump(), resp)
        return resp
    msg = _find_message_in_channel(ch, req.message_id)
    if msg is None:
        resp = {"error": f"Message {req.message_id} not found in channel {req.channel_id}"}
        _log_call("/claw_slack/pins/add", req.model_dump(), resp)
        return resp
    pinned_ids = ch.setdefault("pinned_message_ids", [])
    if req.message_id not in pinned_ids:
        pinned_ids.append(req.message_id)
        record = {
            "channel_id": req.channel_id,
            "message_id": req.message_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        _pinned.append(record)
    resp = {"status": "pinned", "channel_id": req.channel_id, "message_id": req.message_id}
    _log_call("/claw_slack/pins/add", req.model_dump(), resp)
    return resp


@app.post("/claw_slack/pins/remove")
def unpin_message(req: UnpinMessageRequest) -> dict[str, Any]:
    ch = _find_channel(req.channel_id)
    if ch is None:
        resp = {"error": f"Channel {req.channel_id} not found"}
        _log_call("/claw_slack/pins/remove", req.model_dump(), resp)
        return resp
    pinned_ids = ch.get("pinned_message_ids", [])
    if req.message_id not in pinned_ids:
        resp = {"status": "noop"}
        _log_call("/claw_slack/pins/remove", req.model_dump(), resp)
        return resp
    pinned_ids.remove(req.message_id)
    record = {
        "channel_id": req.channel_id,
        "message_id": req.message_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _unpinned.append(record)
    resp = {"status": "unpinned", "channel_id": req.channel_id, "message_id": req.message_id}
    _log_call("/claw_slack/pins/remove", req.model_dump(), resp)
    return resp


@app.get("/claw_slack/audit")
def get_audit() -> dict[str, Any]:
    return {
        "calls": _audit_log,
        "posted_messages": _posted_messages,
        "reactions_added": _reactions_added,
        "reactions_removed": _reactions_removed,
        "pinned": _pinned,
        "unpinned": _unpinned,
    }


@app.post("/claw_slack/reset")
def reset_state() -> dict[str, str]:
    global _audit_log, _posted_messages, _reactions_added, _reactions_removed, _pinned, _unpinned
    _audit_log = []
    _posted_messages = []
    _reactions_added = []
    _reactions_removed = []
    _pinned = []
    _unpinned = []
    _load_fixtures()
    return {"status": "reset"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "9127")))
