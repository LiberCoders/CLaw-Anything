"""Mock Facebook-style social feed service for agent evaluation (FastAPI on port 9128)."""

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

app = FastAPI(title="Mock Facebook Feed API")

from mock_services._base import add_error_injection
add_error_injection(app)

FIXTURES_PATH = Path(os.environ.get(
    "CLAW_FACEBOOK_FIXTURES",
    str(Path(__file__).resolve().parent.parent.parent / "new_tasks" / "_service_smoke" / "fixtures" / "claw_facebook" / "posts.json"),
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


# --- Request models ---


class BrowsePostsRequest(BaseModel):
    tag: str | None = None
    author: str | None = None
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


@app.post("/claw_facebook/browse")
def browse_posts(req: BrowsePostsRequest | None = None) -> dict[str, Any]:
    if req is None:
        req = BrowsePostsRequest()

    results = []
    for post in _posts:
        if req.tag and req.tag not in post.get("tags", []):
            continue
        if req.author and post.get("author") != req.author:
            continue
        if req.liked is not None and post.get("liked") != req.liked:
            continue
        if req.saved is not None and post.get("saved") != req.saved:
            continue
        if req.reposted is not None and post.get("reposted") != req.reposted:
            continue
        results.append({
            "post_id": post["post_id"],
            "author": post["author"],
            "content": post["content"][:200],
            "tags": post.get("tags", []),
            # Fixtures (and create_post above) use "timestamp"/"likes"; tolerate
            # both the legacy and current field names so browse never 500s.
            "posted_at": post.get("posted_at") or post.get("timestamp"),
            "likes_count": post.get("likes_count", post.get("likes", 0)),
            "reposts_count": post.get("reposts_count", post.get("reposts", 0)),
            "liked": post.get("liked", False),
            "saved": post.get("saved", False),
            "reposted": post.get("reposted", False),
        })

    if req.sort_by == "hot":
        results.sort(key=lambda p: p["likes_count"], reverse=True)
    else:
        results.sort(key=lambda p: p["posted_at"] or "", reverse=True)

    total = len(results)
    paged = results[req.offset: req.offset + req.max_results]
    resp = {"posts": paged, "total": total, "returned": len(paged), "offset": req.offset}
    _log_call("/claw_facebook/browse", req.model_dump(), resp)
    return resp


@app.post("/claw_facebook/get_post")
def get_post(req: GetPostRequest) -> dict[str, Any]:
    for post in _posts:
        if post["post_id"] == req.post_id:
            resp = copy.deepcopy(post)
            _log_call("/claw_facebook/get_post", req.model_dump(), resp)
            return resp

    resp = {"error": f"Post {req.post_id} not found"}
    _log_call("/claw_facebook/get_post", req.model_dump(), resp)
    return resp


@app.post("/claw_facebook/search")
def search_posts(req: SearchPostsRequest) -> dict[str, Any]:
    query_lower = req.query.lower()
    results = []
    for post in _posts:
        searchable = (post.get("content", "") + " " + post.get("author", "") + " " + " ".join(post.get("tags", []))).lower()
        if query_lower in searchable:
            results.append({
                "post_id": post["post_id"],
                "author": post["author"],
                "content": post["content"][:200],
                "tags": post.get("tags", []),
                "posted_at": post.get("posted_at") or post.get("timestamp"),
                "likes_count": post.get("likes_count", post.get("likes", 0)),
                "liked": post.get("liked", False),
                "saved": post.get("saved", False),
                "reposted": post.get("reposted", False),
            })

    total = len(results)
    paged = results[req.offset: req.offset + req.max_results]
    resp = {"posts": paged, "total": total, "returned": len(paged)}
    _log_call("/claw_facebook/search", req.model_dump(), resp)
    return resp


@app.post("/claw_facebook/like")
def like_post(req: LikePostRequest) -> dict[str, Any]:
    for post in _posts:
        if post["post_id"] == req.post_id:
            if post["liked"]:
                resp = {"status": "already_liked", "post_id": req.post_id}
                _log_call("/claw_facebook/like", req.model_dump(), resp)
                return resp
            post["liked"] = True
            post["likes_count"] = post.get("likes_count", 0) + 1
            _liked.append({"post_id": req.post_id, "timestamp": datetime.now(timezone.utc).isoformat()})
            resp = {"status": "liked", "post_id": req.post_id}
            _log_call("/claw_facebook/like", req.model_dump(), resp)
            return resp

    resp = {"error": f"Post {req.post_id} not found"}
    _log_call("/claw_facebook/like", req.model_dump(), resp)
    return resp


@app.post("/claw_facebook/save")
def save_post(req: SavePostRequest) -> dict[str, Any]:
    for post in _posts:
        if post["post_id"] == req.post_id:
            if post["saved"]:
                resp = {"status": "already_saved", "post_id": req.post_id}
                _log_call("/claw_facebook/save", req.model_dump(), resp)
                return resp
            post["saved"] = True
            _saved.append({"post_id": req.post_id, "timestamp": datetime.now(timezone.utc).isoformat()})
            resp = {"status": "saved", "post_id": req.post_id}
            _log_call("/claw_facebook/save", req.model_dump(), resp)
            return resp

    resp = {"error": f"Post {req.post_id} not found"}
    _log_call("/claw_facebook/save", req.model_dump(), resp)
    return resp


@app.post("/claw_facebook/repost")
def repost_post(req: RepostRequest) -> dict[str, Any]:
    for post in _posts:
        if post["post_id"] == req.post_id:
            if post["reposted"]:
                resp = {"status": "already_reposted", "post_id": req.post_id}
                _log_call("/claw_facebook/repost", req.model_dump(), resp)
                return resp
            post["reposted"] = True
            post["repost_comment"] = req.repost_comment
            post["reposts_count"] = post.get("reposts_count", 0) + 1
            _reposted.append({
                "post_id": req.post_id,
                "repost_comment": req.repost_comment,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            resp = {"status": "reposted", "post_id": req.post_id, "repost_comment": req.repost_comment}
            _log_call("/claw_facebook/repost", req.model_dump(), resp)
            return resp

    resp = {"error": f"Post {req.post_id} not found"}
    _log_call("/claw_facebook/repost", req.model_dump(), resp)
    return resp


@app.get("/claw_facebook/audit")
def get_audit() -> dict[str, Any]:
    return {
        "calls": _audit_log,
        "liked": _liked,
        "saved": _saved,
        "reposted": _reposted,
    }


@app.post("/claw_facebook/reset")
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
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "9128")))
