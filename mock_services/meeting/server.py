"""Mock Meeting API service (Zoom-like lifecycle + recordings) for agent evaluation (FastAPI on port 9123).

State machine:
    scheduled --(start)--> started --(end)--> ended
    scheduled --(cancel)--> cancelled
    scheduled --(end)----> cancelled      # soft-cancel: ending a never-started meeting

Design notes:
- Recording is an artifact of the `end` endpoint on a `started` meeting only; soft-cancel
  path (end on scheduled) does NOT produce a recording.
- Both hard-cancel and soft-cancel feed the same `_cancelled` side-effect list so
  grader/safety can catch either path with one check.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from fastapi import FastAPI
from pydantic import BaseModel, Field

app = FastAPI(title="Mock Meeting API")

from mock_services._base import add_error_injection, frozen_now  # noqa: E402
add_error_injection(app)

FIXTURES_PATH = Path(os.environ.get(
    "MEETING_FIXTURES",
    str(Path(__file__).resolve().parent.parent.parent / "new_tasks" / "_service_smoke" / "fixtures" / "meeting" / "meetings.json"),
))

# In-memory state
_meetings: list[dict[str, Any]] = []
_audit_log: list[dict[str, Any]] = []
_created: list[dict[str, Any]] = []
_updated: list[dict[str, Any]] = []
_cancelled: list[dict[str, Any]] = []
_started: list[dict[str, Any]] = []
_ended: list[dict[str, Any]] = []
_added_participants: list[dict[str, Any]] = []
_removed_participants: list[dict[str, Any]] = []


def _load_fixtures() -> None:
    global _meetings
    with open(FIXTURES_PATH) as f:
        _meetings = json.load(f)


_load_fixtures()


def _log_call(endpoint: str, request_body: dict[str, Any], response_body: Any) -> None:
    _audit_log.append({
        "endpoint": endpoint,
        "request_body": request_body,
        "response_body": response_body,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


def _next_meeting_id() -> str:
    # Honor fixture IDs: derive next from max existing MTG-#### suffix.
    max_n = 7000
    for m in _meetings:
        mid = m.get("meeting_id", "")
        if mid.startswith("MTG-"):
            try:
                max_n = max(max_n, int(mid[4:]))
            except ValueError:
                pass
    return f"MTG-{max_n + 1}"


def _next_recording_id() -> str:
    max_n = 0
    for m in _meetings:
        for r in m.get("recordings") or []:
            rid = r.get("recording_id", "")
            if rid.startswith("REC-"):
                try:
                    max_n = max(max_n, int(rid[4:]))
                except ValueError:
                    pass
    return f"REC-{max_n + 1:04d}"


def _find(meeting_id: str) -> dict[str, Any] | None:
    for m in _meetings:
        if m.get("meeting_id") == meeting_id:
            return m
    return None


# --- Request models ---


class ListMeetingsRequest(BaseModel):
    status: str = "all"          # scheduled | started | ended | cancelled | all
    host: str | None = None
    start_date: str | None = None  # YYYY-MM-DD, inclusive
    end_date: str | None = None    # YYYY-MM-DD, inclusive


class GetMeetingRequest(BaseModel):
    meeting_id: str


class CreateMeetingRequest(BaseModel):
    topic: str
    host: str
    start_time: str
    duration_minutes: int = 60
    participants: list[dict[str, Any]] = Field(default_factory=list)
    join_url: str | None = None
    passcode: str | None = None


class UpdateMeetingRequest(BaseModel):
    meeting_id: str
    topic: str | None = None
    start_time: str | None = None
    duration_minutes: int | None = None


class MeetingIdOnlyRequest(BaseModel):
    meeting_id: str


class ParticipantAddRequest(BaseModel):
    meeting_id: str
    email: str
    role: str = "attendee"   # host | panelist | attendee


class ParticipantRemoveRequest(BaseModel):
    meeting_id: str
    email: str


class ListRecordingsRequest(BaseModel):
    meeting_id: str | None = None
    host: str | None = None
    start_date: str | None = None
    end_date: str | None = None


# --- Endpoints ---


@app.post("/meeting/list")
def list_meetings(req: ListMeetingsRequest | None = None) -> dict[str, Any]:
    if req is None:
        req = ListMeetingsRequest()
    results = []
    for m in _meetings:
        if req.status != "all" and m.get("status") != req.status:
            continue
        if req.host and req.host.lower() not in (m.get("host", "") or "").lower():
            continue
        if req.start_date or req.end_date:
            try:
                m_date = m.get("start_time", "")[:10]
                if req.start_date and m_date < req.start_date:
                    continue
                if req.end_date and m_date > req.end_date:
                    continue
            except Exception:
                continue
        results.append({
            "meeting_id": m["meeting_id"],
            "topic": m.get("topic", ""),
            "host": m.get("host", ""),
            "start_time": m.get("start_time", ""),
            "duration_minutes": m.get("duration_minutes"),
            "status": m.get("status", ""),
            "participant_count": len(m.get("participants") or []),
        })
    results.sort(key=lambda x: x.get("start_time") or "")
    resp = {"meetings": results, "total": len(results)}
    _log_call("/meeting/list", req.model_dump(), resp)
    return resp


@app.post("/meeting/get")
def get_meeting(req: GetMeetingRequest) -> dict[str, Any]:
    m = _find(req.meeting_id)
    if m is None:
        resp = {"error": f"Meeting {req.meeting_id} not found"}
    else:
        resp = copy.deepcopy(m)
    _log_call("/meeting/get", req.model_dump(), resp)
    return resp


@app.post("/meeting/create")
def create_meeting(req: CreateMeetingRequest) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    new_id = _next_meeting_id()
    meeting = {
        "meeting_id": new_id,
        "topic": req.topic,
        "host": req.host,
        "start_time": req.start_time,
        "duration_minutes": req.duration_minutes,
        "join_url": req.join_url or f"https://meet.internal/{new_id.lower()}",
        "passcode": req.passcode or f"{abs(hash(new_id)) % 1_000_000:06d}",
        "status": "scheduled",
        "participants": list(req.participants),
        "recordings": [],
        "created_at": now,
        "updated_at": now,
    }
    _meetings.append(meeting)
    _created.append(copy.deepcopy(meeting))
    resp = {"status": "created", "meeting": meeting}
    _log_call("/meeting/create", req.model_dump(), resp)
    return resp


@app.post("/meeting/update")
def update_meeting(req: UpdateMeetingRequest) -> dict[str, Any]:
    m = _find(req.meeting_id)
    if m is None:
        resp = {"error": f"Meeting {req.meeting_id} not found"}
        _log_call("/meeting/update", req.model_dump(), resp)
        return resp
    if m.get("status") in ("ended", "cancelled"):
        resp = {"error": f"Cannot update meeting in terminal state '{m.get('status')}'"}
        _log_call("/meeting/update", req.model_dump(), resp)
        return resp
    changed = {}
    if req.topic is not None:
        m["topic"] = req.topic
        changed["topic"] = req.topic
    if req.start_time is not None:
        m["start_time"] = req.start_time
        changed["start_time"] = req.start_time
    if req.duration_minutes is not None:
        m["duration_minutes"] = req.duration_minutes
        changed["duration_minutes"] = req.duration_minutes
    m["updated_at"] = datetime.now(timezone.utc).isoformat()
    _updated.append({"meeting_id": req.meeting_id, "changed": changed, "at": m["updated_at"]})
    resp = {"status": "updated", "meeting": copy.deepcopy(m)}
    _log_call("/meeting/update", req.model_dump(), resp)
    return resp


@app.post("/meeting/cancel")
def cancel_meeting(req: MeetingIdOnlyRequest) -> dict[str, Any]:
    m = _find(req.meeting_id)
    if m is None:
        resp = {"error": f"Meeting {req.meeting_id} not found"}
        _log_call("/meeting/cancel", req.model_dump(), resp)
        return resp
    status = m.get("status")
    if status in ("ended", "cancelled"):
        resp = {"error": f"Cannot cancel meeting in terminal state '{status}'"}
        _log_call("/meeting/cancel", req.model_dump(), resp)
        return resp
    m["status"] = "cancelled"
    m["updated_at"] = datetime.now(timezone.utc).isoformat()
    _cancelled.append({"meeting_id": req.meeting_id, "via": "cancel", "at": m["updated_at"]})
    resp = {"status": "cancelled", "meeting": copy.deepcopy(m)}
    _log_call("/meeting/cancel", req.model_dump(), resp)
    return resp


@app.post("/meeting/start")
def start_meeting(req: MeetingIdOnlyRequest) -> dict[str, Any]:
    m = _find(req.meeting_id)
    if m is None:
        resp = {"error": f"Meeting {req.meeting_id} not found"}
        _log_call("/meeting/start", req.model_dump(), resp)
        return resp
    if m.get("status") != "scheduled":
        resp = {"error": f"Cannot start meeting in state '{m.get('status')}' (must be 'scheduled')"}
        _log_call("/meeting/start", req.model_dump(), resp)
        return resp
    now = datetime.now(timezone.utc).isoformat()
    m["status"] = "started"
    m["started_at"] = now
    m["updated_at"] = now
    _started.append({"meeting_id": req.meeting_id, "at": now})
    resp = {"status": "started", "meeting": copy.deepcopy(m)}
    _log_call("/meeting/start", req.model_dump(), resp)
    return resp


@app.post("/meeting/end")
def end_meeting(req: MeetingIdOnlyRequest) -> dict[str, Any]:
    m = _find(req.meeting_id)
    if m is None:
        resp = {"error": f"Meeting {req.meeting_id} not found"}
        _log_call("/meeting/end", req.model_dump(), resp)
        return resp
    status = m.get("status")
    now = datetime.now(timezone.utc).isoformat()
    if status == "started":
        duration_seconds = max(
            1,
            int((datetime.now(timezone.utc) - datetime.fromisoformat(m.get("started_at", now))).total_seconds()),
        ) if m.get("started_at") else (m.get("duration_minutes") or 60) * 60
        recording = {
            "recording_id": _next_recording_id(),
            "meeting_id": req.meeting_id,
            "recorded_at": now,
            "duration_seconds": duration_seconds,
            "download_url": f"https://meet.internal/recordings/{req.meeting_id.lower()}.mp4",
        }
        m.setdefault("recordings", []).append(recording)
        m["status"] = "ended"
        m["ended_at"] = now
        m["updated_at"] = now
        _ended.append({"meeting_id": req.meeting_id, "at": now, "recording_id": recording["recording_id"]})
        resp = {"status": "ended", "meeting": copy.deepcopy(m), "recording": recording}
        _log_call("/meeting/end", req.model_dump(), resp)
        return resp
    if status == "scheduled":
        # Soft-cancel: ending a never-started meeting is semantically equivalent to cancelling it.
        # Goes into the same _cancelled list so grader/safety catch this path uniformly.
        m["status"] = "cancelled"
        m["updated_at"] = now
        _cancelled.append({"meeting_id": req.meeting_id, "via": "end_on_scheduled", "at": now})
        resp = {"status": "cancelled", "meeting": copy.deepcopy(m), "note": "end on scheduled meeting treated as cancel"}
        _log_call("/meeting/end", req.model_dump(), resp)
        return resp
    resp = {"error": f"Cannot end meeting in terminal state '{status}'"}
    _log_call("/meeting/end", req.model_dump(), resp)
    return resp


@app.post("/meeting/participants/add")
def add_participant(req: ParticipantAddRequest) -> dict[str, Any]:
    m = _find(req.meeting_id)
    if m is None:
        resp = {"error": f"Meeting {req.meeting_id} not found"}
        _log_call("/meeting/participants/add", req.model_dump(), resp)
        return resp
    participants = m.setdefault("participants", [])
    for p in participants:
        if p.get("email", "").lower() == req.email.lower():
            resp = {"error": f"Participant {req.email} already on meeting", "meeting_id": req.meeting_id}
            _log_call("/meeting/participants/add", req.model_dump(), resp)
            return resp
    entry = {"email": req.email, "role": req.role, "status": "invited"}
    participants.append(entry)
    m["updated_at"] = datetime.now(timezone.utc).isoformat()
    _added_participants.append({"meeting_id": req.meeting_id, **entry, "at": m["updated_at"]})
    resp = {"status": "added", "meeting_id": req.meeting_id, "participant": entry}
    _log_call("/meeting/participants/add", req.model_dump(), resp)
    return resp


@app.post("/meeting/participants/remove")
def remove_participant(req: ParticipantRemoveRequest) -> dict[str, Any]:
    m = _find(req.meeting_id)
    if m is None:
        resp = {"error": f"Meeting {req.meeting_id} not found"}
        _log_call("/meeting/participants/remove", req.model_dump(), resp)
        return resp
    participants = m.get("participants") or []
    for i, p in enumerate(participants):
        if p.get("email", "").lower() == req.email.lower():
            removed = participants.pop(i)
            m["updated_at"] = datetime.now(timezone.utc).isoformat()
            _removed_participants.append({"meeting_id": req.meeting_id, **removed, "at": m["updated_at"]})
            resp = {"status": "removed", "meeting_id": req.meeting_id, "participant": removed}
            _log_call("/meeting/participants/remove", req.model_dump(), resp)
            return resp
    resp = {"error": f"Participant {req.email} not found on meeting {req.meeting_id}"}
    _log_call("/meeting/participants/remove", req.model_dump(), resp)
    return resp


@app.post("/meeting/recordings")
def list_recordings(req: ListRecordingsRequest | None = None) -> dict[str, Any]:
    if req is None:
        req = ListRecordingsRequest()
    results = []
    for m in _meetings:
        if req.meeting_id and m.get("meeting_id") != req.meeting_id:
            continue
        if req.host and req.host.lower() not in (m.get("host", "") or "").lower():
            continue
        for r in m.get("recordings") or []:
            # Fixtures store the recording timestamp under a variety of keys
            # (uploaded_at / upload_time / created_at / start_time /
            # available_at); none use recorded_at. Fall back across them so the
            # date filter and sort don't silently drop every fixture recording,
            # and surface the resolved value as recorded_at for consumers.
            rec_ts = (
                r.get("recorded_at")
                or r.get("uploaded_at")
                or r.get("upload_time")
                or r.get("created_at")
                or r.get("start_time")
                or r.get("available_at")
                or ""
            )
            rec_date = rec_ts[:10]
            if req.start_date and rec_date < req.start_date:
                continue
            if req.end_date and rec_date > req.end_date:
                continue
            rec_out = copy.deepcopy(r)
            rec_out.setdefault("recorded_at", rec_ts)
            results.append({
                **rec_out,
                "topic": m.get("topic", ""),
                "host": m.get("host", ""),
            })
    results.sort(key=lambda x: x.get("recorded_at") or "")
    resp = {"recordings": results, "total": len(results)}
    _log_call("/meeting/recordings", req.model_dump(), resp)
    return resp


@app.get("/meeting/audit")
def get_audit() -> dict[str, Any]:
    return {
        "calls": _audit_log,
        "created": _created,
        "updated": _updated,
        "cancelled": _cancelled,
        "started": _started,
        "ended": _ended,
        "added_participants": _added_participants,
        "removed_participants": _removed_participants,
    }


@app.post("/meeting/reset")
def reset_state() -> dict[str, str]:
    global _audit_log, _created, _updated, _cancelled, _started, _ended, _added_participants, _removed_participants
    _audit_log = []
    _created = []
    _updated = []
    _cancelled = []
    _started = []
    _ended = []
    _added_participants = []
    _removed_participants = []
    _load_fixtures()
    return {"status": "reset"}


if __name__ == "__main__":
    import uvicorn

    # frozen_now is imported to share the _base contract; reference it to
    # satisfy linters even though the endpoints here use datetime.now() directly
    # (audit timestamps track real wall-clock order).
    _ = frozen_now
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "9116")))
