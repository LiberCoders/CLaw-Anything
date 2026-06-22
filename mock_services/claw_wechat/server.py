"""Mock WeChat-style service: 1:1/group chat (claw_wechat_chat) + Moments feed (claw_wechat_moments)."""

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

app = FastAPI(title="Mock WeChat API")

from mock_services._base import add_error_injection, frozen_now
add_error_injection(app)

_BASE = Path(__file__).resolve().parent.parent.parent
_SMOKE = _BASE / "new_tasks" / "_service_smoke" / "fixtures" / "claw_wechat"

CHAT_FIXTURES_PATH = Path(os.environ.get(
    "CLAW_WECHAT_CHAT_FIXTURES",
    str(_SMOKE / "chats.json"),
))
MOMENTS_FIXTURES_PATH = Path(os.environ.get(
    "CLAW_WECHAT_MOMENTS_FIXTURES",
    str(_SMOKE / "moments.json"),
))

# In-memory state
_chats: list[dict[str, Any]] = []
_moments: list[dict[str, Any]] = []
_audit_log: list[dict[str, Any]] = []
_sent_messages: list[dict[str, Any]] = []
_recalled_messages: list[dict[str, Any]] = []
_mutes: list[dict[str, Any]] = []
_likes: list[dict[str, Any]] = []
_comments: list[dict[str, Any]] = []
_posted_moments: list[dict[str, Any]] = []


def _load_fixtures() -> None:
    global _chats, _moments
    with open(CHAT_FIXTURES_PATH) as f:
        _chats = json.load(f)
    with open(MOMENTS_FIXTURES_PATH) as f:
        _moments = json.load(f)


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
    type: str = "all"          # "all" | "dm" | "group"
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
# Moments request models
# ---------------------------------------------------------------------------

class BrowseMomentsRequest(BaseModel):
    max_results: int = 20
    offset: int = 0
    author: str | None = None   # filter by author


class SearchMomentsRequest(BaseModel):
    query: str
    max_results: int = 20


class LikeMomentRequest(BaseModel):
    post_id: str
    like: bool = True           # True = like, False = unlike


class CommentMomentRequest(BaseModel):
    post_id: str
    content: str


class PostMomentRequest(BaseModel):
    content: str
    images: list[str] = []
    visibility: str = "friends"


# ---------------------------------------------------------------------------
# Chat endpoints
# ---------------------------------------------------------------------------

@app.post("/claw_wechat/chats/list")
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
            "is_muted": chat.get("is_muted", chat.get("muted", False)),
            "last_message_at": chat.get("last_message_at") or chat.get("last_updated"),
            "message_count": len(chat.get("messages", [])),
        })
    total = len(results)
    paged = results[req.offset: req.offset + req.max_results]
    resp = {"chats": paged, "total": total, "returned": len(paged), "offset": req.offset}
    _log_call("/claw_wechat/chats/list", req.model_dump(), resp)
    return resp


@app.post("/claw_wechat/chats/get")
def get_chat(req: GetChatRequest) -> dict[str, Any]:
    for chat in _chats:
        if chat["chat_id"] == req.chat_id:
            result = copy.deepcopy(chat)
            result["messages"] = result.get("messages", [])[-req.max_messages:]
            resp = result
            _log_call("/claw_wechat/chats/get", req.model_dump(), resp)
            return resp
    resp = {"error": f"Chat {req.chat_id} not found"}
    _log_call("/claw_wechat/chats/get", req.model_dump(), resp)
    return resp


@app.post("/claw_wechat/chats/search")
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
    _log_call("/claw_wechat/chats/search", req.model_dump(), resp)
    return resp


@app.post("/claw_wechat/chats/send")
def send_message(req: SendMessageRequest) -> dict[str, Any]:
    for chat in _chats:
        if chat["chat_id"] != req.chat_id:
            continue
        msgs = chat.setdefault("messages", [])
        msg_id = f"WXMSG-{len(msgs) + 1:04d}"
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
        _log_call("/claw_wechat/chats/send", req.model_dump(), resp)
        return resp
    resp = {"error": f"Chat {req.chat_id} not found"}
    _log_call("/claw_wechat/chats/send", req.model_dump(), resp)
    return resp


@app.post("/claw_wechat/chats/recall")
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
                _log_call("/claw_wechat/chats/recall", req.model_dump(), resp)
                return resp
        resp = {"error": f"Message {req.message_id} not found"}
        _log_call("/claw_wechat/chats/recall", req.model_dump(), resp)
        return resp
    resp = {"error": f"Chat {req.chat_id} not found"}
    _log_call("/claw_wechat/chats/recall", req.model_dump(), resp)
    return resp


@app.post("/claw_wechat/chats/mute")
def mute_chat(req: MuteChatRequest) -> dict[str, Any]:
    for chat in _chats:
        if chat["chat_id"] != req.chat_id:
            continue
        chat["is_muted"] = req.mute
        record = {"chat_id": req.chat_id, "mute": req.mute,
                  "timestamp": datetime.now(timezone.utc).isoformat()}
        _mutes.append(record)
        resp = {"status": "ok", "chat_id": req.chat_id, "is_muted": req.mute}
        _log_call("/claw_wechat/chats/mute", req.model_dump(), resp)
        return resp
    resp = {"error": f"Chat {req.chat_id} not found"}
    _log_call("/claw_wechat/chats/mute", req.model_dump(), resp)
    return resp


# ---------------------------------------------------------------------------
# Moments endpoints
# ---------------------------------------------------------------------------

@app.post("/claw_wechat/moments/browse")
def browse_moments(req: BrowseMomentsRequest | None = None) -> dict[str, Any]:
    if req is None:
        req = BrowseMomentsRequest()
    results = []
    for post in _moments:
        if req.author and post.get("author") != req.author:
            continue
        results.append({
            "post_id": post["post_id"],
            "author": post["author"],
            "content": post["content"],
            "images": post.get("images", []),
            "posted_at": post["posted_at"],
            "visibility": post.get("visibility", "friends"),
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
    _log_call("/claw_wechat/moments/browse", req.model_dump(), resp)
    return resp


@app.post("/claw_wechat/moments/search")
def search_moments(req: SearchMomentsRequest) -> dict[str, Any]:
    query_lower = req.query.lower()
    hits = []
    for post in _moments:
        if query_lower in post.get("content", "").lower() or query_lower in post.get("author", "").lower():
            hits.append({
                "post_id": post["post_id"],
                "author": post["author"],
                "content": post["content"],
                "posted_at": post["posted_at"],
                "likes_count": len(post.get("likes", [])),
            })
    resp = {"results": hits[:req.max_results], "total": len(hits)}
    _log_call("/claw_wechat/moments/search", req.model_dump(), resp)
    return resp


@app.post("/claw_wechat/moments/like")
def like_moment(req: LikeMomentRequest) -> dict[str, Any]:
    for post in _moments:
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
        _log_call("/claw_wechat/moments/like", req.model_dump(), resp)
        return resp
    resp = {"error": f"Post {req.post_id} not found"}
    _log_call("/claw_wechat/moments/like", req.model_dump(), resp)
    return resp


@app.post("/claw_wechat/moments/comment")
def comment_moment(req: CommentMomentRequest) -> dict[str, Any]:
    for post in _moments:
        if post["post_id"] != req.post_id:
            continue
        comments: list = post.setdefault("comments", [])
        cmt_id = f"WXCMT-{len(comments) + 1:04d}"
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
        _log_call("/claw_wechat/moments/comment", req.model_dump(), resp)
        return resp
    resp = {"error": f"Post {req.post_id} not found"}
    _log_call("/claw_wechat/moments/comment", req.model_dump(), resp)
    return resp


@app.post("/claw_wechat/moments/post")
def post_moment(req: PostMomentRequest) -> dict[str, Any]:
    post_id = f"WXPYQ-{len(_moments) + 1:04d}"
    new_post = {
        "post_id": post_id,
        "author": "me",
        "content": req.content,
        "images": req.images,
        "posted_at": frozen_now().isoformat(),
        "visibility": req.visibility,
        "likes": [],
        "comments": [],
        "liked_by_me": False,
        "commented_by_me": False,
    }
    _moments.append(new_post)
    _posted_moments.append({"post_id": post_id, "content": req.content,
                             "timestamp": datetime.now(timezone.utc).isoformat()})
    resp = {"status": "posted", "post": new_post}
    _log_call("/claw_wechat/moments/post", req.model_dump(), resp)
    return resp


# ---------------------------------------------------------------------------
# Management
# ---------------------------------------------------------------------------

@app.get("/claw_wechat/audit")
def get_audit() -> dict[str, Any]:
    return {
        "calls": _audit_log,
        "sent_messages": _sent_messages,
        "recalled_messages": _recalled_messages,
        "mutes": _mutes,
        "likes": _likes,
        "comments": _comments,
        "posted_moments": _posted_moments,
    }


@app.post("/claw_wechat/reset")
def reset_state() -> dict[str, str]:
    global _audit_log, _sent_messages, _recalled_messages, _mutes, _likes, _comments, _posted_moments
    _audit_log = []
    _sent_messages = []
    _recalled_messages = []
    _mutes = []
    _likes = []
    _comments = []
    _posted_moments = []
    _load_fixtures()
    return {"status": "reset"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "9130")))
