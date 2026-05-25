"""Mock LinkedIn-style job platform service for agent evaluation (FastAPI on port 9132)."""

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

app = FastAPI(title="Mock LinkedIn Job Platform API")

from mock_services._base import add_error_injection
add_error_injection(app)

FIXTURES_PATH = Path(os.environ.get(
    "CLAW_LINKEDIN_FIXTURES",
    str(Path(__file__).resolve().parent.parent.parent / "new_tasks" / "_service_smoke" / "fixtures" / "claw_linkedin" / "jobs.json"),
))

# In-memory state
_jobs: list[dict[str, Any]] = []
_audit_log: list[dict[str, Any]] = []
_saved: list[dict[str, Any]] = []
_applied: list[dict[str, Any]] = []
_followed: list[dict[str, Any]] = []


def _load_fixtures() -> None:
    global _jobs
    with open(FIXTURES_PATH) as f:
        _jobs = json.load(f)


_load_fixtures()


def _log_call(endpoint: str, request_body: dict[str, Any], response_body: Any) -> None:
    _audit_log.append({
        "endpoint": endpoint,
        "request_body": request_body,
        "response_body": response_body,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


# --- Request models ---


class BrowseJobsRequest(BaseModel):
    company: str | None = None
    skills: list[str] | None = None
    location: str | None = None
    experience_level: str | None = None
    job_type: str | None = None
    saved: bool | None = None
    applied: bool | None = None
    sort_by: str = "recent"  # "recent" | "hot"
    max_results: int = 20
    offset: int = 0


class GetJobRequest(BaseModel):
    job_id: str


class SearchJobsRequest(BaseModel):
    query: str
    max_results: int = 20
    offset: int = 0


class SaveJobRequest(BaseModel):
    job_id: str


class ApplyJobRequest(BaseModel):
    job_id: str
    cover_letter: str | None = None


class FollowCompanyRequest(BaseModel):
    company: str


# --- Endpoints ---


@app.post("/claw_linkedin/browse")
def browse_jobs(req: BrowseJobsRequest | None = None) -> dict[str, Any]:
    if req is None:
        req = BrowseJobsRequest()

    results = []
    for job in _jobs:
        if req.company and job.get("company") != req.company:
            continue
        if req.skills and not any(s in job.get("skills", []) for s in req.skills):
            continue
        if req.location and req.location.lower() not in job.get("location", "").lower():
            continue
        if req.experience_level and job.get("experience_level") != req.experience_level:
            continue
        if req.job_type and job.get("job_type") != req.job_type:
            continue
        if req.saved is not None and job.get("saved") != req.saved:
            continue
        if req.applied is not None and job.get("applied") != req.applied:
            continue
        results.append({
            "job_id": job["job_id"],
            "company": job["company"],
            "title": job["title"],
            "location": job["location"],
            "skills": job["skills"],
            "experience_level": job["experience_level"],
            "job_type": job["job_type"],
            "posted_at": job["posted_at"],
            "applicants_count": job["applicants_count"],
            "salary_range": job.get("salary_range"),
            "saved": job["saved"],
            "applied": job["applied"],
        })

    if req.sort_by == "hot":
        results.sort(key=lambda j: j["applicants_count"], reverse=True)
    else:
        results.sort(key=lambda j: j["posted_at"], reverse=True)

    total = len(results)
    paged = results[req.offset: req.offset + req.max_results]
    resp = {"jobs": paged, "total": total, "returned": len(paged), "offset": req.offset}
    _log_call("/claw_linkedin/browse", req.model_dump(), resp)
    return resp


@app.post("/claw_linkedin/get_job")
def get_job(req: GetJobRequest) -> dict[str, Any]:
    for job in _jobs:
        if job["job_id"] == req.job_id:
            resp = copy.deepcopy(job)
            _log_call("/claw_linkedin/get_job", req.model_dump(), resp)
            return resp

    resp = {"error": f"Job {req.job_id} not found"}
    _log_call("/claw_linkedin/get_job", req.model_dump(), resp)
    return resp


@app.post("/claw_linkedin/search")
def search_jobs(req: SearchJobsRequest) -> dict[str, Any]:
    query_lower = req.query.lower()
    results = []
    for job in _jobs:
        searchable = (
            job.get("title", "") + " "
            + job.get("company", "") + " "
            + job.get("description", "") + " "
            + job.get("location", "") + " "
            + " ".join(job.get("skills", []))
        ).lower()
        if query_lower in searchable:
            results.append({
                "job_id": job["job_id"],
                "company": job["company"],
                "title": job["title"],
                "location": job["location"],
                "skills": job["skills"],
                "experience_level": job["experience_level"],
                "job_type": job["job_type"],
                "posted_at": job["posted_at"],
                "applicants_count": job["applicants_count"],
                "salary_range": job.get("salary_range"),
                "saved": job["saved"],
                "applied": job["applied"],
            })

    total = len(results)
    paged = results[req.offset: req.offset + req.max_results]
    resp = {"jobs": paged, "total": total, "returned": len(paged)}
    _log_call("/claw_linkedin/search", req.model_dump(), resp)
    return resp


@app.post("/claw_linkedin/save")
def save_job(req: SaveJobRequest) -> dict[str, Any]:
    for job in _jobs:
        if job["job_id"] == req.job_id:
            if job["saved"]:
                resp = {"status": "already_saved", "job_id": req.job_id}
                _log_call("/claw_linkedin/save", req.model_dump(), resp)
                return resp
            job["saved"] = True
            _saved.append({"job_id": req.job_id, "timestamp": datetime.now(timezone.utc).isoformat()})
            resp = {"status": "saved", "job_id": req.job_id}
            _log_call("/claw_linkedin/save", req.model_dump(), resp)
            return resp

    resp = {"error": f"Job {req.job_id} not found"}
    _log_call("/claw_linkedin/save", req.model_dump(), resp)
    return resp


@app.post("/claw_linkedin/apply")
def apply_job(req: ApplyJobRequest) -> dict[str, Any]:
    for job in _jobs:
        if job["job_id"] == req.job_id:
            if job["applied"]:
                resp = {"status": "already_applied", "job_id": req.job_id}
                _log_call("/claw_linkedin/apply", req.model_dump(), resp)
                return resp
            job["applied"] = True
            job["applied_at"] = datetime.now(timezone.utc).isoformat()
            job["cover_letter"] = req.cover_letter
            job["applicants_count"] = job.get("applicants_count", 0) + 1
            _applied.append({
                "job_id": req.job_id,
                "cover_letter": req.cover_letter,
                "timestamp": job["applied_at"],
            })
            resp = {"status": "applied", "job_id": req.job_id, "cover_letter": req.cover_letter}
            _log_call("/claw_linkedin/apply", req.model_dump(), resp)
            return resp

    resp = {"error": f"Job {req.job_id} not found"}
    _log_call("/claw_linkedin/apply", req.model_dump(), resp)
    return resp


@app.post("/claw_linkedin/follow_company")
def follow_company(req: FollowCompanyRequest) -> dict[str, Any]:
    if any(f["company"] == req.company for f in _followed):
        resp = {"status": "already_following", "company": req.company}
        _log_call("/claw_linkedin/follow_company", req.model_dump(), resp)
        return resp
    _followed.append({"company": req.company, "timestamp": datetime.now(timezone.utc).isoformat()})
    resp = {"status": "following", "company": req.company}
    _log_call("/claw_linkedin/follow_company", req.model_dump(), resp)
    return resp


@app.get("/claw_linkedin/audit")
def get_audit() -> dict[str, Any]:
    return {
        "calls": _audit_log,
        "saved": _saved,
        "applied": _applied,
        "followed": _followed,
    }


@app.post("/claw_linkedin/reset")
def reset_state() -> dict[str, str]:
    global _audit_log, _saved, _applied, _followed
    _audit_log = []
    _saved = []
    _applied = []
    _followed = []
    _load_fixtures()
    return {"status": "reset"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "9132")))
