"""Mock Scheduler API service for agent evaluation (FastAPI on port 9112).

Manages cron/scheduled jobs with execution history tracking.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="Mock Scheduler API")

from mock_services._base import add_error_injection, frozen_now

add_error_injection(app)

FIXTURES_PATH = Path(os.environ.get(
    "SCHEDULER_FIXTURES",
    str(Path(__file__).resolve().parent.parent.parent / "tasks" / "T41zh_scheduled_task_management" / "fixtures" / "scheduler" / "jobs.json"),
))

_jobs: list[dict[str, Any]] = []
_audit_log: list[dict[str, Any]] = []
_created_jobs: list[dict[str, Any]] = []
_updated_jobs: list[dict[str, Any]] = []
_deleted_jobs: list[dict[str, Any]] = []


def _parse_cron_field(spec: str, lo: int, hi: int) -> set[int] | None:
    """Expand one standard-cron field into the set of matching integers.

    Supports ``*``, ``*/n``, ``a-b``, ``a-b/n``, ``a,b,c`` and bare values.
    Returns None on anything unparseable (caller then skips computation).
    """
    allowed: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            return None
        step = 1
        if "/" in part:
            base, _, step_s = part.partition("/")
            try:
                step = int(step_s)
            except ValueError:
                return None
            if step <= 0:
                return None
        else:
            base = part
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a_s, _, b_s = base.partition("-")
            try:
                start, end = int(a_s), int(b_s)
            except ValueError:
                return None
        else:
            try:
                start = end = int(base)
            except ValueError:
                return None
        if start > end or start < lo or end > hi:
            return None
        allowed.update(range(start, end + 1, step))
    return allowed or None


def _cron_next(cron_expr: str, anchor: datetime) -> str | None:
    """Best-effort next fire time for a 5-field cron at/after ``anchor``.

    Used only to backfill ``next_run`` for fixtures that never ship it. Returns
    an ISO date-minute string, or None if the expression can't be parsed or no
    match is found within ~13 months.
    """
    if not cron_expr or not cron_expr.strip():
        return None
    fields = cron_expr.split()
    if len(fields) != 5:
        return None
    minute = _parse_cron_field(fields[0], 0, 59)
    hour = _parse_cron_field(fields[1], 0, 23)
    dom = _parse_cron_field(fields[2], 1, 31)
    month = _parse_cron_field(fields[3], 1, 12)
    dow = _parse_cron_field(fields[4], 0, 7)
    if None in (minute, hour, dom, month, dow):
        return None
    dow = {0 if d == 7 else d for d in dow}  # cron: 7 and 0 both mean Sunday
    dom_restricted = fields[2].strip() != "*"
    dow_restricted = fields[4].strip() != "*"
    # Start at the next whole minute.
    t = anchor.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(367 * 24 * 60):
        if t.month in month and t.hour in hour and t.minute in minute:
            d_ok = t.day in dom
            w_ok = (t.weekday() + 1) % 7 in dow  # python Mon=0 -> cron Sun=0
            if dom_restricted and dow_restricted:
                day_match = d_ok or w_ok
            elif dom_restricted:
                day_match = d_ok
            elif dow_restricted:
                day_match = w_ok
            else:
                day_match = True
            if day_match:
                return t.strftime("%Y-%m-%dT%H:%M:00")
        t += timedelta(minutes=1)
    return None


def _backfill_next_run() -> None:
    """Populate next_run from cron_expression for fixtures that lack it.

    No fixture ships next_run, so /scheduler/jobs always reported it as null.
    Anchor on the task's frozen execution date; if EXECUTION_DATE is unset
    (e.g. a non-date-frozen smoke run) leave next_run absent rather than crash.
    """
    try:
        anchor = frozen_now()
    except RuntimeError:
        return
    for j in _jobs:
        if j.get("next_run"):
            continue
        if not j.get("enabled", True):
            continue
        nxt = _cron_next(j.get("cron_expression", ""), anchor)
        if nxt is not None:
            j["next_run"] = nxt


def _load_fixtures() -> None:
    global _jobs
    with open(FIXTURES_PATH) as f:
        _jobs = json.load(f)
    _backfill_next_run()


_load_fixtures()


def _log_call(endpoint: str, request_body: dict[str, Any], response_body: Any) -> None:
    _audit_log.append({
        "endpoint": endpoint,
        "request_body": request_body,
        "response_body": response_body,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


class ListJobsRequest(BaseModel):
    status: str | None = None
    enabled: bool | None = None
    tag: str | None = None


class GetJobRequest(BaseModel):
    job_id: str


class CreateJobRequest(BaseModel):
    name: str
    cron_expression: str
    action: str
    enabled: bool = True
    tags: list[str] | None = None
    created_by: str | None = None


class UpdateJobRequest(BaseModel):
    job_id: str
    enabled: bool | None = None
    cron_expression: str | None = None
    name: str | None = None
    action: str | None = None
    tags: list[str] | None = None


class DeleteJobRequest(BaseModel):
    job_id: str


class JobHistoryRequest(BaseModel):
    job_id: str
    limit: int = 10


@app.post("/scheduler/jobs")
def list_jobs(req: ListJobsRequest | None = None) -> dict[str, Any]:
    if req is None:
        req = ListJobsRequest()
    results = []
    for j in _jobs:
        if req.status and j.get("last_status") != req.status:
            continue
        if req.enabled is not None and j.get("enabled") != req.enabled:
            continue
        if req.tag and req.tag not in j.get("tags", []):
            continue
        results.append({
            "job_id": j["job_id"],
            "name": j["name"],
            "cron_expression": j["cron_expression"],
            "enabled": j["enabled"],
            "last_status": j.get("last_status"),
            "last_run": j.get("last_run"),
            "next_run": j.get("next_run"),
            "tags": j.get("tags", []),
        })
    resp = {"jobs": results, "total": len(results)}
    _log_call("/scheduler/jobs", req.model_dump(), resp)
    return resp


@app.post("/scheduler/jobs/get")
def get_job(req: GetJobRequest) -> dict[str, Any]:
    for j in _jobs:
        if j["job_id"] == req.job_id:
            resp = copy.deepcopy(j)
            _log_call("/scheduler/jobs/get", req.model_dump(), resp)
            return resp
    resp = {"error": f"Job {req.job_id} not found"}
    _log_call("/scheduler/jobs/get", req.model_dump(), resp)
    return resp


@app.post("/scheduler/jobs/create")
def create_job(req: CreateJobRequest) -> dict[str, Any]:
    new_id = f"JOB-{len(_jobs) + 1:03d}"
    job = {
        "job_id": new_id,
        "name": req.name,
        "cron_expression": req.cron_expression,
        "action": req.action,
        "enabled": req.enabled,
        "last_run": None,
        "next_run": None,
        "last_status": None,
        "created_by": req.created_by,
        "tags": req.tags or [],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "execution_history": [],
    }
    _jobs.append(job)
    _created_jobs.append(copy.deepcopy(job))
    resp = {"status": "created", "job": copy.deepcopy(job)}
    _log_call("/scheduler/jobs/create", req.model_dump(), resp)
    return resp


@app.post("/scheduler/jobs/update")
def update_job(req: UpdateJobRequest) -> dict[str, Any]:
    for j in _jobs:
        if j["job_id"] == req.job_id:
            if req.enabled is not None:
                j["enabled"] = req.enabled
            if req.cron_expression is not None:
                j["cron_expression"] = req.cron_expression
            if req.name is not None:
                j["name"] = req.name
            if req.action is not None:
                j["action"] = req.action
            if req.tags is not None:
                j["tags"] = req.tags
            updated = copy.deepcopy(j)
            _updated_jobs.append(updated)
            resp = {"status": "updated", "job": updated}
            _log_call("/scheduler/jobs/update", req.model_dump(), resp)
            return resp
    resp = {"error": f"Job {req.job_id} not found"}
    _log_call("/scheduler/jobs/update", req.model_dump(), resp)
    return resp


@app.post("/scheduler/jobs/delete")
def delete_job(req: DeleteJobRequest) -> dict[str, Any]:
    for i, j in enumerate(_jobs):
        if j["job_id"] == req.job_id:
            removed = _jobs.pop(i)
            _deleted_jobs.append(removed)
            resp = {"status": "deleted", "job": removed}
            _log_call("/scheduler/jobs/delete", req.model_dump(), resp)
            return resp
    resp = {"error": f"Job {req.job_id} not found"}
    _log_call("/scheduler/jobs/delete", req.model_dump(), resp)
    return resp


@app.post("/scheduler/jobs/history")
def job_history(req: JobHistoryRequest) -> dict[str, Any]:
    for j in _jobs:
        if j["job_id"] == req.job_id:
            history = j.get("execution_history", [])
            limited = history[:req.limit]
            resp = {"job_id": req.job_id, "history": limited, "total": len(history)}
            _log_call("/scheduler/jobs/history", req.model_dump(), resp)
            return resp
    resp = {"error": f"Job {req.job_id} not found"}
    _log_call("/scheduler/jobs/history", req.model_dump(), resp)
    return resp


@app.get("/scheduler/audit")
def get_audit() -> dict[str, Any]:
    return {
        "calls": _audit_log,
        "created_jobs": _created_jobs,
        "updated_jobs": _updated_jobs,
        "deleted_jobs": _deleted_jobs,
    }


@app.post("/scheduler/reset")
def reset_state() -> dict[str, str]:
    global _audit_log, _created_jobs, _updated_jobs, _deleted_jobs
    _audit_log = []
    _created_jobs = []
    _updated_jobs = []
    _deleted_jobs = []
    _load_fixtures()
    return {"status": "reset"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "9112")))
