"""Mock Claw-Obsidian API service (markdown vault + bidirectional links + tags)
for agent evaluation (FastAPI on port 9122).

Model:
- Each note is a markdown document with: title, folder, tags, content,
  outlinks (ids of notes it links to), inlinks (ids of notes linking to it).
- add_link keeps outlinks/inlinks symmetric across both endpoints.
- delete_note cascades the cleanup: removes the deleted id from the inlinks
  of every note it pointed to, and from the outlinks of every note that
  pointed to it.
- append_to_note only appends markdown text at the end of content; update_note
  replaces content wholesale (considered destructive → listed in FORBIDDEN_TOOLS).
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

app = FastAPI(title="Mock Claw-Obsidian API")

from mock_services._base import add_error_injection, frozen_now
add_error_injection(app)

FIXTURES_PATH = Path(os.environ.get(
    "CLAW_OBSIDIAN_FIXTURES",
    str(Path(__file__).resolve().parent.parent.parent / "new_tasks" / "_service_smoke" / "fixtures" / "claw_obsidian" / "vault.json"),
))

_notes: list[dict[str, Any]] = []
_audit_log: list[dict[str, Any]] = []
_created: list[dict[str, Any]] = []
_updated: list[dict[str, Any]] = []
_appended: list[dict[str, Any]] = []
_deleted: list[dict[str, Any]] = []
_linked: list[dict[str, Any]] = []
_unlinked: list[dict[str, Any]] = []


def _load_fixtures() -> None:
    global _notes
    with open(FIXTURES_PATH) as f:
        _notes = json.load(f)


_load_fixtures()


def _log_call(endpoint: str, request_body: dict[str, Any], response_body: Any) -> None:
    _audit_log.append({
        "endpoint": endpoint,
        "request_body": request_body,
        "response_body": response_body,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


ID_PREFIX = "OBSN-"
ID_PAD = 4


def _allocate_note_id() -> str:
    max_num = 0
    for n in _notes:
        nid = n.get("note_id", "")
        if nid.startswith(ID_PREFIX):
            try:
                num = int(nid[len(ID_PREFIX):])
                if num > max_num:
                    max_num = num
            except ValueError:
                pass
    return f"{ID_PREFIX}{(max_num + 1):0{ID_PAD}d}"


def _find_note(note_id: str) -> dict[str, Any] | None:
    for n in _notes:
        if n.get("note_id") == note_id:
            return n
    return None


# --- Request models ---


class ListNotesRequest(BaseModel):
    folder: str | None = None
    tag: str | None = None
    max_results: int = 20
    offset: int = 0


class GetNoteRequest(BaseModel):
    note_id: str


class CreateNoteRequest(BaseModel):
    title: str
    folder: str | None = None
    tags: list[str] = Field(default_factory=list)
    content: str = ""


class UpdateNoteRequest(BaseModel):
    note_id: str
    content: str


class AppendToNoteRequest(BaseModel):
    note_id: str
    text: str


class DeleteNoteRequest(BaseModel):
    note_id: str


class SearchRequest(BaseModel):
    query: str = ""
    tags: list[str] = Field(default_factory=list)
    max_results: int = 10
    offset: int = 0


class AddLinkRequest(BaseModel):
    from_note_id: str
    to_note_id: str


class RemoveLinkRequest(BaseModel):
    from_note_id: str
    to_note_id: str


class GetBacklinksRequest(BaseModel):
    note_id: str


# --- Endpoints ---


def _summary(note: dict[str, Any]) -> dict[str, Any]:
    return {
        "note_id": note.get("note_id"),
        "title": note.get("title"),
        "folder": note.get("folder"),
        "tags": note.get("tags", []),
        "last_updated": note.get("last_updated"),
    }


@app.post("/claw_obsidian/notes")
def list_notes(req: ListNotesRequest | None = None) -> dict[str, Any]:
    if req is None:
        req = ListNotesRequest()
    filtered = []
    for note in _notes:
        if req.folder is not None and note.get("folder") != req.folder:
            continue
        if req.tag is not None and req.tag not in (note.get("tags") or []):
            continue
        filtered.append(_summary(note))
    total = len(filtered)
    results = filtered[req.offset: req.offset + req.max_results]
    resp = {"notes": results, "total": total, "returned": len(results), "offset": req.offset}
    _log_call("/claw_obsidian/notes", req.model_dump(), resp)
    return resp


@app.post("/claw_obsidian/notes/get")
def get_note(req: GetNoteRequest) -> dict[str, Any]:
    note = _find_note(req.note_id)
    if note is None:
        resp = {"error": f"Note {req.note_id} not found"}
    else:
        resp = copy.deepcopy(note)
    _log_call("/claw_obsidian/notes/get", req.model_dump(), resp)
    return resp


@app.post("/claw_obsidian/notes/create")
def create_note(req: CreateNoteRequest) -> dict[str, Any]:
    now = frozen_now().isoformat()
    new_id = _allocate_note_id()
    note = {
        "note_id": new_id,
        "title": req.title,
        "folder": req.folder,
        "tags": list(req.tags),
        "content": req.content,
        "outlinks": [],
        "inlinks": [],
        "created_at": now,
        "last_updated": now,
    }
    _notes.append(note)
    record = {"note_id": new_id, "title": req.title, "timestamp": datetime.now(timezone.utc).isoformat()}
    _created.append(record)
    resp = {"status": "created", "note": copy.deepcopy(note)}
    _log_call("/claw_obsidian/notes/create", req.model_dump(), resp)
    return resp


@app.post("/claw_obsidian/notes/update")
def update_note(req: UpdateNoteRequest) -> dict[str, Any]:
    note = _find_note(req.note_id)
    if note is None:
        resp = {"error": f"Note {req.note_id} not found"}
        _log_call("/claw_obsidian/notes/update", req.model_dump(), resp)
        return resp
    note["content"] = req.content
    note["last_updated"] = frozen_now().isoformat()
    _updated.append({"note_id": req.note_id, "timestamp": datetime.now(timezone.utc).isoformat()})
    resp = {"status": "updated", "note_id": req.note_id}
    _log_call("/claw_obsidian/notes/update", req.model_dump(), resp)
    return resp


@app.post("/claw_obsidian/notes/append")
def append_to_note(req: AppendToNoteRequest) -> dict[str, Any]:
    note = _find_note(req.note_id)
    if note is None:
        resp = {"error": f"Note {req.note_id} not found"}
        _log_call("/claw_obsidian/notes/append", req.model_dump(), resp)
        return resp
    current = note.get("content", "")
    sep = "" if current.endswith("\n") or current == "" else "\n"
    note["content"] = current + sep + req.text
    note["last_updated"] = frozen_now().isoformat()
    _appended.append({
        "note_id": req.note_id,
        "appended_chars": len(req.text),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    resp = {"status": "appended", "note_id": req.note_id, "total_chars": len(note["content"])}
    _log_call("/claw_obsidian/notes/append", req.model_dump(), resp)
    return resp


@app.post("/claw_obsidian/notes/delete")
def delete_note(req: DeleteNoteRequest) -> dict[str, Any]:
    note = _find_note(req.note_id)
    if note is None:
        resp = {"error": f"Note {req.note_id} not found"}
        _log_call("/claw_obsidian/notes/delete", req.model_dump(), resp)
        return resp

    # Cascade cleanup: scrub this note's id from all its neighbours.
    out_ids = list(note.get("outlinks") or [])
    in_ids = list(note.get("inlinks") or [])
    for peer_id in out_ids:
        peer = _find_note(peer_id)
        if peer is not None:
            ins = peer.get("inlinks") or []
            peer["inlinks"] = [x for x in ins if x != req.note_id]
    for peer_id in in_ids:
        peer = _find_note(peer_id)
        if peer is not None:
            outs = peer.get("outlinks") or []
            peer["outlinks"] = [x for x in outs if x != req.note_id]

    _notes.remove(note)
    _deleted.append({"note_id": req.note_id, "timestamp": datetime.now(timezone.utc).isoformat()})
    resp = {"status": "deleted", "note_id": req.note_id}
    _log_call("/claw_obsidian/notes/delete", req.model_dump(), resp)
    return resp


@app.post("/claw_obsidian/search")
def search_notes(req: SearchRequest) -> dict[str, Any]:
    query_words = req.query.lower().split() if req.query else []
    results: list[dict[str, Any]] = []
    for note in _notes:
        searchable = (
            (note.get("title") or "") + " "
            + " ".join(note.get("tags") or []) + " "
            + (note.get("content") or "")
        ).lower()
        if query_words and not all(w in searchable for w in query_words):
            continue
        if req.tags and not all(t in (note.get("tags") or []) for t in req.tags):
            continue
        results.append(_summary(note))
    total = len(results)
    results = results[req.offset: req.offset + req.max_results]
    resp = {"notes": results, "total": total, "returned": len(results), "offset": req.offset}
    _log_call("/claw_obsidian/search", req.model_dump(), resp)
    return resp


@app.post("/claw_obsidian/links/add")
def add_link(req: AddLinkRequest) -> dict[str, Any]:
    src = _find_note(req.from_note_id)
    dst = _find_note(req.to_note_id)
    if src is None or dst is None:
        missing = req.from_note_id if src is None else req.to_note_id
        resp = {"error": f"Note {missing} not found"}
        _log_call("/claw_obsidian/links/add", req.model_dump(), resp)
        return resp
    outs = src.setdefault("outlinks", [])
    ins = dst.setdefault("inlinks", [])
    if req.to_note_id not in outs:
        outs.append(req.to_note_id)
    if req.from_note_id not in ins:
        ins.append(req.from_note_id)
    now = frozen_now().isoformat()
    src["last_updated"] = now
    dst["last_updated"] = now
    _linked.append({
        "from_note_id": req.from_note_id,
        "to_note_id": req.to_note_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    resp = {"status": "linked", "from_note_id": req.from_note_id, "to_note_id": req.to_note_id}
    _log_call("/claw_obsidian/links/add", req.model_dump(), resp)
    return resp


@app.post("/claw_obsidian/links/remove")
def remove_link(req: RemoveLinkRequest) -> dict[str, Any]:
    src = _find_note(req.from_note_id)
    dst = _find_note(req.to_note_id)
    if src is None or dst is None:
        missing = req.from_note_id if src is None else req.to_note_id
        resp = {"error": f"Note {missing} not found"}
        _log_call("/claw_obsidian/links/remove", req.model_dump(), resp)
        return resp
    src["outlinks"] = [x for x in (src.get("outlinks") or []) if x != req.to_note_id]
    dst["inlinks"] = [x for x in (dst.get("inlinks") or []) if x != req.from_note_id]
    now = frozen_now().isoformat()
    src["last_updated"] = now
    dst["last_updated"] = now
    _unlinked.append({
        "from_note_id": req.from_note_id,
        "to_note_id": req.to_note_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    resp = {"status": "unlinked", "from_note_id": req.from_note_id, "to_note_id": req.to_note_id}
    _log_call("/claw_obsidian/links/remove", req.model_dump(), resp)
    return resp


@app.post("/claw_obsidian/backlinks")
def get_backlinks(req: GetBacklinksRequest) -> dict[str, Any]:
    note = _find_note(req.note_id)
    if note is None:
        resp = {"error": f"Note {req.note_id} not found"}
        _log_call("/claw_obsidian/backlinks", req.model_dump(), resp)
        return resp
    backlinks = list(note.get("inlinks") or [])
    resp = {"note_id": req.note_id, "backlinks": backlinks, "count": len(backlinks)}
    _log_call("/claw_obsidian/backlinks", req.model_dump(), resp)
    return resp


@app.get("/claw_obsidian/audit")
def get_audit() -> dict[str, Any]:
    return {
        "calls": _audit_log,
        "created": _created,
        "updated": _updated,
        "appended": _appended,
        "deleted": _deleted,
        "linked": _linked,
        "unlinked": _unlinked,
    }


@app.post("/claw_obsidian/reset")
def reset_state() -> dict[str, str]:
    global _audit_log, _created, _updated, _appended, _deleted, _linked, _unlinked
    _audit_log = []
    _created = []
    _updated = []
    _appended = []
    _deleted = []
    _linked = []
    _unlinked = []
    _load_fixtures()
    return {"status": "reset"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "9115")))
