"""Mock News-style social news platform service for agent evaluation (FastAPI on port 9135)."""

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
from pydantic import BaseModel

app = FastAPI(title="Mock News Feed API")

from mock_services._base import add_error_injection
add_error_injection(app)

FIXTURES_PATH = Path(os.environ.get(
    "CLAW_NEWS_FIXTURES",
    str(Path(__file__).resolve().parent.parent.parent / "new_tasks" / "_service_smoke" / "fixtures" / "claw_news" / "posts.json"),
))

# In-memory state
_posts: list[dict[str, Any]] = []
_audit_log: list[dict[str, Any]] = []
_liked: list[dict[str, Any]] = []
_saved: list[dict[str, Any]] = []
_reposted: list[dict[str, Any]] = []


def _load_fixtures() -> None:
    global _posts
    with open(FIXTURES_PATH) as f:
        _posts = json.load(f)


_load_fixtures()


def _log_call(endpoint: str, request_body: dict[str, Any], response_body: Any) -> None:
    _audit_log.append({
        "endpoint": endpoint,
        "request_body": request_body,
        "response_body": response_body,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


def _summary(post: dict[str, Any]) -> dict[str, Any]:
    return {
        "post_id": post["post_id"],
        "title": post["title"],
        "source": post["source"],
        "author": post.get("author"),
        "category": post["category"],
        "tags": post.get("tags", []),
        "posted_at": post["posted_at"],
        "is_breaking": post.get("is_breaking", False),
        "credibility": post.get("credibility", "verified"),
        "likes_count": post.get("likes_count", 0),
        "reposts_count": post.get("reposts_count", 0),
        "saves_count": post.get("saves_count", 0),
        "liked": post.get("liked", False),
        "saved": post.get("saved", False),
        "reposted": post.get("reposted", False),
    }


# --- Request models ---


class BrowsePostsRequest(BaseModel):
    category: str | None = None
    source: str | None = None
    tag: str | None = None
    is_breaking: bool | None = None
    credibility: str | None = None
    liked: bool | None = None
    saved: bool | None = None
    reposted: bool | None = None
    sort_by: str = "recent"  # "recent" | "hot"
    max_results: int = 20
    offset: int = 0


class GetPostRequest(BaseModel):
    post_id: str


class SearchPostsRequest(BaseModel):
    query: str
    category: str | None = None
    max_results: int = 20
    offset: int = 0


class LikePostRequest(BaseModel):
    post_id: str


class SavePostRequest(BaseModel):
    post_id: str


class RepostRequest(BaseModel):
    post_id: str
    repost_comment: str | None = None


# --- Endpoints ---


@app.post("/claw_news/browse")
def browse_posts(req: BrowsePostsRequest | None = None) -> dict[str, Any]:
    if req is None:
        req = BrowsePostsRequest()

    results = []
    for post in _posts:
        if req.category and post.get("category") != req.category:
            continue
        if req.source and post.get("source") != req.source:
            continue
        if req.tag and req.tag not in post.get("tags", []):
            continue
        if req.is_breaking is not None and post.get("is_breaking", False) != req.is_breaking:
            continue
        if req.credibility and post.get("credibility") != req.credibility:
            continue
        if req.liked is not None and post.get("liked", False) != req.liked:
            continue
        if req.saved is not None and post.get("saved", False) != req.saved:
            continue
        if req.reposted is not None and post.get("reposted", False) != req.reposted:
            continue
        results.append(_summary(post))

    if req.sort_by == "hot":
        results.sort(key=lambda p: p["likes_count"], reverse=True)
    else:
        results.sort(key=lambda p: p["posted_at"], reverse=True)

    total = len(results)
    paged = results[req.offset: req.offset + req.max_results]
    resp = {"posts": paged, "total": total, "returned": len(paged), "offset": req.offset}
    _log_call("/claw_news/browse", req.model_dump(), resp)
    return resp


@app.post("/claw_news/get_post")
def get_post(req: GetPostRequest) -> dict[str, Any]:
    for post in _posts:
        if post["post_id"] == req.post_id:
            resp = copy.deepcopy(post)
            _log_call("/claw_news/get_post", req.model_dump(), resp)
            return resp

    resp = {"error": f"Post {req.post_id} not found"}
    _log_call("/claw_news/get_post", req.model_dump(), resp)
    return resp


@app.post("/claw_news/search")
def search_posts(req: SearchPostsRequest) -> dict[str, Any]:
    query_lower = req.query.lower()
    results = []
    for post in _posts:
        if req.category and post.get("category") != req.category:
            continue
        searchable = " ".join([
            post.get("title", ""),
            post.get("content", ""),
            post.get("source", ""),
            post.get("author", "") or "",
            " ".join(post.get("tags", [])),
        ]).lower()
        if query_lower in searchable:
            results.append(_summary(post))

    total = len(results)
    paged = results[req.offset: req.offset + req.max_results]
    resp = {"posts": paged, "total": total, "returned": len(paged)}
    _log_call("/claw_news/search", req.model_dump(), resp)
    return resp


@app.post("/claw_news/like")
def like_post(req: LikePostRequest) -> dict[str, Any]:
    for post in _posts:
        if post["post_id"] == req.post_id:
            if post.get("liked"):
                resp = {"status": "already_liked", "post_id": req.post_id, "likes_count": post.get("likes_count", 0)}
                _log_call("/claw_news/like", req.model_dump(), resp)
                return resp
            post["liked"] = True
            post["likes_count"] = post.get("likes_count", 0) + 1
            _liked.append({"post_id": req.post_id, "timestamp": datetime.now(timezone.utc).isoformat()})
            resp = {"status": "liked", "post_id": req.post_id, "likes_count": post["likes_count"]}
            _log_call("/claw_news/like", req.model_dump(), resp)
            return resp

    resp = {"error": f"Post {req.post_id} not found"}
    _log_call("/claw_news/like", req.model_dump(), resp)
    return resp


@app.post("/claw_news/save")
def save_post(req: SavePostRequest) -> dict[str, Any]:
    for post in _posts:
        if post["post_id"] == req.post_id:
            if post.get("saved"):
                resp = {"status": "already_saved", "post_id": req.post_id, "saves_count": post.get("saves_count", 0)}
                _log_call("/claw_news/save", req.model_dump(), resp)
                return resp
            post["saved"] = True
            post["saves_count"] = post.get("saves_count", 0) + 1
            _saved.append({"post_id": req.post_id, "timestamp": datetime.now(timezone.utc).isoformat()})
            resp = {"status": "saved", "post_id": req.post_id, "saves_count": post["saves_count"]}
            _log_call("/claw_news/save", req.model_dump(), resp)
            return resp

    resp = {"error": f"Post {req.post_id} not found"}
    _log_call("/claw_news/save", req.model_dump(), resp)
    return resp


@app.post("/claw_news/repost")
def repost_post(req: RepostRequest) -> dict[str, Any]:
    for post in _posts:
        if post["post_id"] == req.post_id:
            if post.get("reposted"):
                resp = {"status": "already_reposted", "post_id": req.post_id, "reposts_count": post.get("reposts_count", 0)}
                _log_call("/claw_news/repost", req.model_dump(), resp)
                return resp
            post["reposted"] = True
            post["repost_comment"] = req.repost_comment
            post["reposts_count"] = post.get("reposts_count", 0) + 1
            _reposted.append({
                "post_id": req.post_id,
                "repost_comment": req.repost_comment,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            resp = {
                "status": "reposted",
                "post_id": req.post_id,
                "repost_comment": req.repost_comment,
                "reposts_count": post["reposts_count"],
            }
            _log_call("/claw_news/repost", req.model_dump(), resp)
            return resp

    resp = {"error": f"Post {req.post_id} not found"}
    _log_call("/claw_news/repost", req.model_dump(), resp)
    return resp


@app.get("/claw_news/audit")
def get_audit() -> dict[str, Any]:
    return {
        "calls": _audit_log,
        "liked": _liked,
        "saved": _saved,
        "reposted": _reposted,
    }


@app.post("/claw_news/reset")
def reset_state() -> dict[str, str]:
    global _audit_log, _liked, _saved, _reposted
    _audit_log = []
    _liked = []
    _saved = []
    _reposted = []
    _load_fixtures()
    return {"status": "reset"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "9135")))
