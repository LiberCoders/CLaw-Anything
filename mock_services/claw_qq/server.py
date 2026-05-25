"""Mock QQ-style service: 1:1/group chat (claw_qq_chat) + QZone feed (claw_qq_zone)."""

from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="Mock QQ API")

from mock_services._base import add_error_injection, frozen_now
add_error_injection(app)

_BASE = Path(__file__).resolve().parent.parent.parent
_SMOKE = _BASE / "new_tasks" / "_service_smoke" / "fixtures" / "claw_qq"

CHAT_FIXTURES_PATH = Path(os.environ.get(
    "CLAW_QQ_CHAT_FIXTURES",
    str(_SMOKE / "chats.json"),
))
ZONE_FIXTURES_PATH = Path(os.environ.get(
    "CLAW_QQ_ZONE_FIXTURES",
    str(_SMOKE / "zone.json"),
))

# In-memory state
_chats: list[dict[str, Any]] = []
_zone: list[dict[str, Any]] = []
_audit_log: list[dict[str, Any]] = []
_sent_messages: list[dict[str, Any]] = []
_recalled_messages: list[dict[str, Any]] = []
_mutes: list[dict[str, Any]] = []
_likes: list[dict[str, Any]] = []
_comments: list[dict[str, Any]] = []
_posted_zone: list[dict[str, Any]] = []


def _load_fixtures() -> None:
    global _chats, _zone
    with open(CHAT_FIXTURES_PATH) as f:
        _chats = json.load(f)
    with open(ZONE_FIXTURES_PATH) as f:
        _zone = json.load(f)


_load_fixtures()


def _log_call(endpoint: str, request_body: dict[str, Any], response_body: Any) -> None:
    _audit_log.append({
        "endpoint": endpoint,
        "request_body": request_body,
        "response_body": response_body,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


# ---------------------------------------------------------------------------
# Chat request models
# ---------------------------------------------------------------------------

class ListChatsRequest(BaseModel):
    type: str = "all"          # "all" | "private" | "group"
    max_results: int = 20
    offset: int = 0


class GetChatRequest(BaseModel):
    chat_id: str
    max_messages: int = 50


class SearchMessagesRequest(BaseModel):
    query: str
    chat_id: str | None = None  # None = search all chats
    max_results: int = 20


class SendMessageRequest(BaseModel):
    chat_id: str
    content: str


class RecallMessageRequest(BaseModel):
    chat_id: str
    message_id: str


class MuteChatRequest(BaseModel):
    chat_id: str
    mute: bool = True           # True = mute (消息免打扰), False = unmute


# ---------------------------------------------------------------------------
# QZone request models
# ---------------------------------------------------------------------------

class BrowseZoneRequest(BaseModel):
    max_results: int = 20
    offset: int = 0
    author: str | None = None   # filter by author


class SearchZoneRequest(BaseModel):
    query: str
    max_results: int = 20


class LikeZonePostRequest(BaseModel):
    post_id: str
    like: bool = True           # True = like, False = unlike


class CommentZonePostRequest(BaseModel):
    post_id: str
    content: str


class PostZoneRequest(BaseModel):
    content: str
    images: list[str] = []
    mood: str = "none"          # "happy" | "excited" | "sad" | "none"
    visibility: str = "public"  # "public" | "friends" | "private"


# ---------------------------------------------------------------------------
# Chat endpoints
# ---------------------------------------------------------------------------

@app.post("/claw_qq/chats/list")
def list_chats(req: ListChatsRequest | None = None) -> dict[str, Any]:
    if req is None:
        req = ListChatsRequest()
    results = []
    for chat in _chats:
        if req.type != "all" and chat.get("type") != req.type:
            continue
        results.append({
            "chat_id": chat["chat_id"],
            "type": chat["type"],
            "name": chat["name"],
            "members": chat["members"],
            "is_muted": chat.get("is_muted", False),
            "last_message_at": chat.get("last_message_at"),
            "message_count": len(chat.get("messages", [])),
        })
    total = len(results)
    paged = results[req.offset: req.offset + req.max_results]
    resp = {"chats": paged, "total": total, "returned": len(paged), "offset": req.offset}
    _log_call("/claw_qq/chats/list", req.model_dump(), resp)
    return resp


@app.post("/claw_qq/chats/get")
def get_chat(req: GetChatRequest) -> dict[str, Any]:
    for chat in _chats:
        if chat["chat_id"] == req.chat_id:
            result = copy.deepcopy(chat)
            result["messages"] = result.get("messages", [])[-req.max_messages:]
            resp = result
            _log_call("/claw_qq/chats/get", req.model_dump(), resp)
            return resp
    resp = {"error": f"Chat {req.chat_id} not found"}
    _log_call("/claw_qq/chats/get", req.model_dump(), resp)
    return resp


@app.post("/claw_qq/chats/search")
def search_messages(req: SearchMessagesRequest) -> dict[str, Any]:
    hits = []
    query_lower = req.query.lower()
    for chat in _chats:
        if req.chat_id and chat["chat_id"] != req.chat_id:
            continue
        for msg in chat.get("messages", []):
            if msg.get("recalled"):
                continue
            if query_lower in msg.get("content", "").lower():
                hits.append({
                    "chat_id": chat["chat_id"],
                    "chat_name": chat["name"],
                    "message_id": msg["message_id"],
                    "sender": msg["sender"],
                    "content": msg["content"],
                    "timestamp": msg["timestamp"],
                })
    total = len(hits)
    paged = hits[:req.max_results]
    resp = {"results": paged, "total": total, "returned": len(paged)}
    _log_call("/claw_qq/chats/search", req.model_dump(), resp)
    return resp


@app.post("/claw_qq/chats/send")
def send_message(req: SendMessageRequest) -> dict[str, Any]:
    for chat in _chats:
        if chat["chat_id"] != req.chat_id:
            continue
        msgs = chat.setdefault("messages", [])
        msg_id = f"QQMSG-{len(msgs) + 1:04d}"
        msg = {
            "message_id": msg_id,
            "sender": "me",
            "content": req.content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "recalled": False,
        }
        msgs.append(msg)
        chat["last_message_at"] = msg["timestamp"]
        _sent_messages.append({"chat_id": req.chat_id, **msg})
        resp = {"status": "sent", "message": msg}
        _log_call("/claw_qq/chats/send", req.model_dump(), resp)
        return resp
    resp = {"error": f"Chat {req.chat_id} not found"}
    _log_call("/claw_qq/chats/send", req.model_dump(), resp)
    return resp


@app.post("/claw_qq/chats/recall")
def recall_message(req: RecallMessageRequest) -> dict[str, Any]:
    for chat in _chats:
        if chat["chat_id"] != req.chat_id:
            continue
        for msg in chat.get("messages", []):
            if msg["message_id"] == req.message_id:
                msg["recalled"] = True
                msg["content"] = "[recalled]"
                record = {"chat_id": req.chat_id, "message_id": req.message_id,
                          "timestamp": datetime.now(timezone.utc).isoformat()}
                _recalled_messages.append(record)
                resp = {"status": "recalled", "message_id": req.message_id}
                _log_call("/claw_qq/chats/recall", req.model_dump(), resp)
                return resp
        resp = {"error": f"Message {req.message_id} not found"}
        _log_call("/claw_qq/chats/recall", req.model_dump(), resp)
        return resp
    resp = {"error": f"Chat {req.chat_id} not found"}
    _log_call("/claw_qq/chats/recall", req.model_dump(), resp)
    return resp


@app.post("/claw_qq/chats/mute")
def mute_chat(req: MuteChatRequest) -> dict[str, Any]:
    for chat in _chats:
        if chat["chat_id"] != req.chat_id:
            continue
        chat["is_muted"] = req.mute
        record = {"chat_id": req.chat_id, "mute": req.mute,
                  "timestamp": datetime.now(timezone.utc).isoformat()}
        _mutes.append(record)
        resp = {"status": "ok", "chat_id": req.chat_id, "is_muted": req.mute}
        _log_call("/claw_qq/chats/mute", req.model_dump(), resp)
        return resp
    resp = {"error": f"Chat {req.chat_id} not found"}
    _log_call("/claw_qq/chats/mute", req.model_dump(), resp)
    return resp


# ---------------------------------------------------------------------------
# QZone endpoints
# ---------------------------------------------------------------------------

@app.post("/claw_qq/zone/browse")
def browse_zone(req: BrowseZoneRequest | None = None) -> dict[str, Any]:
    if req is None:
        req = BrowseZoneRequest()
    results = []
    for post in _zone:
        if req.author and post.get("author") != req.author:
            continue
        results.append({
            "post_id": post["post_id"],
            "author": post["author"],
            "content": post["content"],
            "images": post.get("images", []),
            "mood": post.get("mood", "none"),
            "posted_at": post["posted_at"],
            "visibility": post.get("visibility", "public"),
            "likes_count": len(post.get("likes", [])),
            "comments_count": len(post.get("comments", [])),
            "liked_by_me": post.get("liked_by_me", False),
            "commented_by_me": post.get("commented_by_me", False),
        })
    # sort newest first
    results.sort(key=lambda p: p["posted_at"], reverse=True)
    total = len(results)
    paged = results[req.offset: req.offset + req.max_results]
    resp = {"posts": paged, "total": total, "returned": len(paged), "offset": req.offset}
    _log_call("/claw_qq/zone/browse", req.model_dump(), resp)
    return resp


@app.post("/claw_qq/zone/search")
def search_zone(req: SearchZoneRequest) -> dict[str, Any]:
    query_lower = req.query.lower()
    hits = []
    for post in _zone:
        if query_lower in post.get("content", "").lower() or query_lower in post.get("author", "").lower():
            hits.append({
                "post_id": post["post_id"],
                "author": post["author"],
                "content": post["content"],
                "mood": post.get("mood", "none"),
                "posted_at": post["posted_at"],
                "likes_count": len(post.get("likes", [])),
            })
    resp = {"results": hits[:req.max_results], "total": len(hits)}
    _log_call("/claw_qq/zone/search", req.model_dump(), resp)
    return resp


@app.post("/claw_qq/zone/like")
def like_zone_post(req: LikeZonePostRequest) -> dict[str, Any]:
    for post in _zone:
        if post["post_id"] != req.post_id:
            continue
        likes: list = post.setdefault("likes", [])
        if req.like and "me" not in likes:
            likes.append("me")
            post["liked_by_me"] = True
        elif not req.like and "me" in likes:
            likes.remove("me")
            post["liked_by_me"] = False
        record = {"post_id": req.post_id, "like": req.like,
                  "timestamp": datetime.now(timezone.utc).isoformat()}
        _likes.append(record)
        resp = {"status": "ok", "post_id": req.post_id, "liked": req.like,
                "likes_count": len(likes)}
        _log_call("/claw_qq/zone/like", req.model_dump(), resp)
        return resp
    resp = {"error": f"Post {req.post_id} not found"}
    _log_call("/claw_qq/zone/like", req.model_dump(), resp)
    return resp


@app.post("/claw_qq/zone/comment")
def comment_zone_post(req: CommentZonePostRequest) -> dict[str, Any]:
    for post in _zone:
        if post["post_id"] != req.post_id:
            continue
        comments: list = post.setdefault("comments", [])
        cmt_id = f"QQCMT-{len(comments) + 1:04d}"
        cmt = {
            "comment_id": cmt_id,
            "author": "me",
            "content": req.content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        comments.append(cmt)
        post["commented_by_me"] = True
        _comments.append({"post_id": req.post_id, **cmt})
        resp = {"status": "commented", "comment": cmt}
        _log_call("/claw_qq/zone/comment", req.model_dump(), resp)
        return resp
    resp = {"error": f"Post {req.post_id} not found"}
    _log_call("/claw_qq/zone/comment", req.model_dump(), resp)
    return resp


@app.post("/claw_qq/zone/post")
def post_zone(req: PostZoneRequest) -> dict[str, Any]:
    post_id = f"QQZONE-{len(_zone) + 1:04d}"
    new_post = {
        "post_id": post_id,
        "author": "me",
        "content": req.content,
        "images": req.images,
        "mood": req.mood,
        "posted_at": frozen_now().isoformat(),
        "visibility": req.visibility,
        "likes": [],
        "comments": [],
        "liked_by_me": False,
        "commented_by_me": False,
    }
    _zone.append(new_post)
    _posted_zone.append({"post_id": post_id, "content": req.content,
                          "timestamp": datetime.now(timezone.utc).isoformat()})
    resp = {"status": "posted", "post": new_post}
    _log_call("/claw_qq/zone/post", req.model_dump(), resp)
    return resp


# ---------------------------------------------------------------------------
# Management
# ---------------------------------------------------------------------------

@app.get("/claw_qq/audit")
def get_audit() -> dict[str, Any]:
    return {
        "calls": _audit_log,
        "sent_messages": _sent_messages,
        "recalled_messages": _recalled_messages,
        "mutes": _mutes,
        "likes": _likes,
        "comments": _comments,
        "posted_zone": _posted_zone,
    }


@app.post("/claw_qq/reset")
def reset_state() -> dict[str, str]:
    global _audit_log, _sent_messages, _recalled_messages, _mutes, _likes, _comments, _posted_zone
    _audit_log = []
    _sent_messages = []
    _recalled_messages = []
    _mutes = []
    _likes = []
    _comments = []
    _posted_zone = []
    _load_fixtures()
    return {"status": "reset"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "9131")))
