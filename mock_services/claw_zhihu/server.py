"""Mock Zhihu-style Q&A knowledge community service for agent evaluation (FastAPI on port 9129)."""

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

app = FastAPI(title="Mock Zhihu Q&A API")

from mock_services._base import add_error_injection
add_error_injection(app)

FIXTURES_PATH = Path(os.environ.get(
    "CLAW_ZHIHU_FIXTURES",
    str(Path(__file__).resolve().parent.parent.parent / "new_tasks" / "_service_smoke" / "fixtures" / "claw_zhihu" / "questions.json"),
))

# In-memory state
_questions: list[dict[str, Any]] = []
_audit_log: list[dict[str, Any]] = []
_upvoted: list[dict[str, Any]] = []
_collected: list[dict[str, Any]] = []
_followed: list[dict[str, Any]] = []
_posted_answers: list[dict[str, Any]] = []


def _load_fixtures() -> None:
    global _questions
    with open(FIXTURES_PATH) as f:
        _questions = json.load(f)


_load_fixtures()


def _log_call(endpoint: str, request_body: dict[str, Any], response_body: Any) -> None:
    _audit_log.append({
        "endpoint": endpoint,
        "request_body": request_body,
        "response_body": response_body,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


def _find_answer(answer_id: str) -> tuple[dict | None, dict | None]:
    """Return (question, answer) for the given answer_id, or (None, None) if not found."""
    for q in _questions:
        for a in q.get("answers", []):
            if a["answer_id"] == answer_id:
                return q, a
    return None, None


def _next_answer_id() -> str:
    max_idx = 0
    for q in _questions:
        for a in q.get("answers", []):
            aid = a.get("answer_id", "")
            if aid.startswith("ZHA-"):
                try:
                    idx = int(aid[4:])
                    if idx > max_idx:
                        max_idx = idx
                except ValueError:
                    pass
    return f"ZHA-{max_idx + 1:04d}"


# --- Request models ---


class SearchQuestionsRequest(BaseModel):
    query: str | None = None
    topic: str | None = None
    sort_by: str = "recent"  # "recent" | "hot"
    max_results: int = 20
    offset: int = 0


class GetQuestionRequest(BaseModel):
    question_id: str


class UpvoteAnswerRequest(BaseModel):
    answer_id: str


class CollectAnswerRequest(BaseModel):
    answer_id: str


class FollowQuestionRequest(BaseModel):
    question_id: str


class PostAnswerRequest(BaseModel):
    question_id: str
    author: str
    content: str


# --- Endpoints ---


@app.post("/claw_zhihu/search_questions")
def search_questions(req: SearchQuestionsRequest | None = None) -> dict[str, Any]:
    if req is None:
        req = SearchQuestionsRequest()

    results = []
    for q in _questions:
        if req.topic and req.topic not in q.get("topic", []):
            continue
        if req.query:
            searchable = (q.get("title", "") + " " + " ".join(q.get("topic", []))).lower()
            if req.query.lower() not in searchable:
                continue
        results.append({
            "question_id": q.get("question_id"),
            "title": q.get("title", ""),
            "topic": q.get("topic", []),
            "author": q.get("author", ""),
            "created_at": q.get("created_at", ""),
            # Generated question fixtures carry follow_count, not a per-viewer
            # ``followed`` flag (that only exists on activity-log records) — a
            # hard q["followed"] index 500'd the whole listing. Default to False
            # and surface follow_count when present.
            "answer_count": q.get("answer_count", 0),
            "follow_count": q.get("follow_count"),
            "followed": q.get("followed", False),
        })

    if req.sort_by == "hot":
        results.sort(key=lambda x: x.get("answer_count") or 0, reverse=True)
    else:
        results.sort(key=lambda x: x.get("created_at") or "", reverse=True)

    total = len(results)
    paged = results[req.offset: req.offset + req.max_results]
    resp = {"questions": paged, "total": total, "returned": len(paged), "offset": req.offset}
    _log_call("/claw_zhihu/search_questions", req.model_dump(), resp)
    return resp


@app.post("/claw_zhihu/get_question")
def get_question(req: GetQuestionRequest) -> dict[str, Any]:
    for q in _questions:
        if q["question_id"] == req.question_id:
            resp = copy.deepcopy(q)
            _log_call("/claw_zhihu/get_question", req.model_dump(), resp)
            return resp

    resp = {"error": f"Question {req.question_id} not found"}
    _log_call("/claw_zhihu/get_question", req.model_dump(), resp)
    return resp


@app.post("/claw_zhihu/upvote_answer")
def upvote_answer(req: UpvoteAnswerRequest) -> dict[str, Any]:
    _, answer = _find_answer(req.answer_id)
    if answer is None:
        resp = {"error": f"Answer {req.answer_id} not found"}
        _log_call("/claw_zhihu/upvote_answer", req.model_dump(), resp)
        return resp

    if answer["upvoted"]:
        resp = {"status": "already_upvoted", "answer_id": req.answer_id}
        _log_call("/claw_zhihu/upvote_answer", req.model_dump(), resp)
        return resp

    answer["upvoted"] = True
    answer["upvotes"] += 1
    _upvoted.append({"answer_id": req.answer_id, "timestamp": datetime.now(timezone.utc).isoformat()})
    resp = {"status": "upvoted", "answer_id": req.answer_id, "upvotes": answer["upvotes"]}
    _log_call("/claw_zhihu/upvote_answer", req.model_dump(), resp)
    return resp


@app.post("/claw_zhihu/collect_answer")
def collect_answer(req: CollectAnswerRequest) -> dict[str, Any]:
    _, answer = _find_answer(req.answer_id)
    if answer is None:
        resp = {"error": f"Answer {req.answer_id} not found"}
        _log_call("/claw_zhihu/collect_answer", req.model_dump(), resp)
        return resp

    if answer["collected"]:
        resp = {"status": "already_collected", "answer_id": req.answer_id}
        _log_call("/claw_zhihu/collect_answer", req.model_dump(), resp)
        return resp

    answer["collected"] = True
    _collected.append({"answer_id": req.answer_id, "timestamp": datetime.now(timezone.utc).isoformat()})
    resp = {"status": "collected", "answer_id": req.answer_id}
    _log_call("/claw_zhihu/collect_answer", req.model_dump(), resp)
    return resp


@app.post("/claw_zhihu/follow_question")
def follow_question(req: FollowQuestionRequest) -> dict[str, Any]:
    for q in _questions:
        if q["question_id"] == req.question_id:
            if q.get("followed"):
                resp = {"status": "already_followed", "question_id": req.question_id}
                _log_call("/claw_zhihu/follow_question", req.model_dump(), resp)
                return resp
            q["followed"] = True
            _followed.append({"question_id": req.question_id, "timestamp": datetime.now(timezone.utc).isoformat()})
            resp = {"status": "followed", "question_id": req.question_id}
            _log_call("/claw_zhihu/follow_question", req.model_dump(), resp)
            return resp

    resp = {"error": f"Question {req.question_id} not found"}
    _log_call("/claw_zhihu/follow_question", req.model_dump(), resp)
    return resp


@app.post("/claw_zhihu/post_answer")
def post_answer(req: PostAnswerRequest) -> dict[str, Any]:
    for q in _questions:
        if q["question_id"] == req.question_id:
            new_answer = {
                "answer_id": _next_answer_id(),
                "author": req.author,
                "content": req.content,
                "upvotes": 0,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "upvoted": False,
                "collected": False,
            }
            q["answers"].append(new_answer)
            q["answer_count"] += 1
            q["has_my_answer"] = True
            _posted_answers.append({"answer_id": new_answer["answer_id"], "question_id": req.question_id, "timestamp": new_answer["created_at"]})
            resp = {"status": "posted", "answer_id": new_answer["answer_id"], "question_id": req.question_id}
            _log_call("/claw_zhihu/post_answer", req.model_dump(), resp)
            return resp

    resp = {"error": f"Question {req.question_id} not found"}
    _log_call("/claw_zhihu/post_answer", req.model_dump(), resp)
    return resp


@app.get("/claw_zhihu/audit")
def get_audit() -> dict[str, Any]:
    return {
        "calls": _audit_log,
        "upvoted": _upvoted,
        "collected": _collected,
        "followed": _followed,
        "posted_answers": _posted_answers,
    }


@app.post("/claw_zhihu/reset")
def reset_state() -> dict[str, str]:
    global _audit_log, _upvoted, _collected, _followed, _posted_answers
    _audit_log = []
    _upvoted = []
    _collected = []
    _followed = []
    _posted_answers = []
    _load_fixtures()
    return {"status": "reset"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "9129")))
