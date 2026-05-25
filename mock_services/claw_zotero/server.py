"""Mock Claw-Zotero API service (reference/literature manager with academic
metadata + collections + per-item notes) for agent evaluation
(FastAPI on port 9124).

Model:
- Each item is a literature record with: item_id, item_type (article / book /
  book_chapter / thesis / webpage / report), title, authors, year, venue,
  doi, url, abstract, tags, citation_key, collection_ids, notes, date_added,
  date_modified.
- Collections are flat records keyed by collection_id; an item can belong to
  multiple collections via item.collection_ids.
- add_note appends a note to item.notes (globally unique note_id).
- update_item only mutates a whitelist of metadata fields. item_id /
  date_added / collection_ids / notes have dedicated endpoints and cannot be
  mutated via update_item.
- DOI and citation_key must be unique library-wide; add_item / update_item
  return a conflict response if they collide with an existing item.
- delete_item / remove_from_collection are considered destructive and are
  listed in FORBIDDEN_TOOLS in the grader template.
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

app = FastAPI(title="Mock Claw-Zotero API")

from mock_services._base import add_error_injection, frozen_now
add_error_injection(app)

FIXTURES_PATH = Path(os.environ.get(
    "CLAW_ZOTERO_FIXTURES",
    str(Path(__file__).resolve().parent.parent.parent / "new_tasks" / "_service_smoke" / "fixtures" / "claw_zotero" / "library.json"),
))

_items: list[dict[str, Any]] = []
_collections: list[dict[str, Any]] = []
_audit_log: list[dict[str, Any]] = []
_added: list[dict[str, Any]] = []
_updated: list[dict[str, Any]] = []
_deleted: list[dict[str, Any]] = []
_notes_added: list[dict[str, Any]] = []
_collection_events: list[dict[str, Any]] = []


def _load_fixtures() -> None:
    global _items, _collections
    with open(FIXTURES_PATH) as f:
        data = json.load(f)
    if isinstance(data, dict):
        _items = list(data.get("items") or [])
        _collections = list(data.get("collections") or [])
    else:
        # Tolerate a bare list of items (no collections).
        _items = list(data)
        _collections = []


_load_fixtures()


def _log_call(endpoint: str, request_body: dict[str, Any], response_body: Any) -> None:
    _audit_log.append({
        "endpoint": endpoint,
        "request_body": request_body,
        "response_body": response_body,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


ITEM_PREFIX = "ZOT-"
NOTE_PREFIX = "ZNOTE-"
COLLECTION_PREFIX = "COL-"
ID_PAD = 4

ITEM_TYPES = {"article", "book", "book_chapter", "thesis", "webpage", "report"}
UPDATABLE_FIELDS = {
    "title", "authors", "year", "venue", "doi", "url", "abstract",
    "tags", "citation_key", "item_type",
}


def _allocate_id(prefix: str, used_ids: list[str]) -> str:
    max_num = 0
    for x in used_ids:
        if isinstance(x, str) and x.startswith(prefix):
            try:
                num = int(x[len(prefix):])
                if num > max_num:
                    max_num = num
            except ValueError:
                pass
    return f"{prefix}{(max_num + 1):0{ID_PAD}d}"


def _allocate_item_id() -> str:
    return _allocate_id(ITEM_PREFIX, [i.get("item_id", "") for i in _items])


def _allocate_note_id() -> str:
    used: list[str] = []
    for i in _items:
        for n in i.get("notes") or []:
            used.append(n.get("note_id", ""))
    return _allocate_id(NOTE_PREFIX, used)


def _find_item(item_id: str) -> dict[str, Any] | None:
    for i in _items:
        if i.get("item_id") == item_id:
            return i
    return None


def _find_collection(collection_id: str) -> dict[str, Any] | None:
    for c in _collections:
        if c.get("collection_id") == collection_id:
            return c
    return None


# --- Request models ---


class ListItemsRequest(BaseModel):
    collection_id: str | None = None
    tag: str | None = None
    item_type: str | None = None
    year_min: int | None = None
    year_max: int | None = None
    max_results: int = 20
    offset: int = 0


class GetItemRequest(BaseModel):
    item_id: str


class SearchRequest(BaseModel):
    query: str = ""
    tags: list[str] = Field(default_factory=list)
    max_results: int = 10
    offset: int = 0


class ListCollectionsRequest(BaseModel):
    pass


class AddItemRequest(BaseModel):
    title: str
    item_type: str = "article"
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    doi: str | None = None
    url: str | None = None
    abstract: str | None = None
    tags: list[str] = Field(default_factory=list)
    citation_key: str | None = None


class UpdateItemRequest(BaseModel):
    item_id: str
    fields: dict[str, Any]


class AddNoteRequest(BaseModel):
    item_id: str
    text: str


class AddToCollectionRequest(BaseModel):
    item_id: str
    collection_id: str


class DeleteItemRequest(BaseModel):
    item_id: str


class RemoveFromCollectionRequest(BaseModel):
    item_id: str
    collection_id: str


# --- Helpers ---


def _summary(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "item_id": item.get("item_id"),
        "item_type": item.get("item_type"),
        "title": item.get("title"),
        "authors": item.get("authors", []),
        "year": item.get("year"),
        "venue": item.get("venue"),
        "tags": item.get("tags", []),
        "citation_key": item.get("citation_key"),
    }


# --- Endpoints ---


@app.post("/claw_zotero/items")
def list_items(req: ListItemsRequest | None = None) -> dict[str, Any]:
    if req is None:
        req = ListItemsRequest()
    filtered: list[dict[str, Any]] = []
    for item in _items:
        if req.collection_id is not None and req.collection_id not in (item.get("collection_ids") or []):
            continue
        if req.tag is not None and req.tag not in (item.get("tags") or []):
            continue
        if req.item_type is not None and item.get("item_type") != req.item_type:
            continue
        yr = item.get("year")
        if req.year_min is not None and (yr is None or yr < req.year_min):
            continue
        if req.year_max is not None and (yr is None or yr > req.year_max):
            continue
        filtered.append(_summary(item))
    total = len(filtered)
    results = filtered[req.offset: req.offset + req.max_results]
    resp = {"items": results, "total": total, "returned": len(results), "offset": req.offset}
    _log_call("/claw_zotero/items", req.model_dump(), resp)
    return resp


@app.post("/claw_zotero/items/get")
def get_item(req: GetItemRequest) -> dict[str, Any]:
    item = _find_item(req.item_id)
    if item is None:
        resp: dict[str, Any] = {"error": f"Item {req.item_id} not found"}
    else:
        resp = copy.deepcopy(item)
    _log_call("/claw_zotero/items/get", req.model_dump(), resp)
    return resp


@app.post("/claw_zotero/search")
def search(req: SearchRequest) -> dict[str, Any]:
    query_words = req.query.lower().split() if req.query else []
    results: list[dict[str, Any]] = []
    for item in _items:
        hay = (
            (item.get("title") or "") + " "
            + " ".join(item.get("authors") or []) + " "
            + (item.get("doi") or "") + " "
            + (item.get("venue") or "") + " "
            + (item.get("abstract") or "") + " "
            + " ".join(item.get("tags") or []) + " "
            + (item.get("citation_key") or "")
        ).lower()
        if query_words and not all(w in hay for w in query_words):
            continue
        if req.tags and not all(t in (item.get("tags") or []) for t in req.tags):
            continue
        results.append(_summary(item))
    total = len(results)
    results = results[req.offset: req.offset + req.max_results]
    resp = {"items": results, "total": total, "returned": len(results), "offset": req.offset}
    _log_call("/claw_zotero/search", req.model_dump(), resp)
    return resp


@app.post("/claw_zotero/collections")
def list_collections(req: ListCollectionsRequest | None = None) -> dict[str, Any]:
    if req is None:
        req = ListCollectionsRequest()
    out: list[dict[str, Any]] = []
    for c in _collections:
        cid = c.get("collection_id")
        count = sum(1 for i in _items if cid in (i.get("collection_ids") or []))
        out.append({
            "collection_id": cid,
            "name": c.get("name"),
            "parent": c.get("parent"),
            "item_count": count,
        })
    resp = {"collections": out, "total": len(out)}
    _log_call("/claw_zotero/collections", req.model_dump(), resp)
    return resp


@app.post("/claw_zotero/items/add")
def add_item(req: AddItemRequest) -> dict[str, Any]:
    if req.item_type not in ITEM_TYPES:
        resp: dict[str, Any] = {
            "error": f"Unknown item_type '{req.item_type}'. Allowed: {sorted(ITEM_TYPES)}",
        }
        _log_call("/claw_zotero/items/add", req.model_dump(), resp)
        return resp
    if req.doi:
        for existing in _items:
            if existing.get("doi") == req.doi:
                resp = {"status": "conflict",
                        "error": f"DOI conflict: already exists as {existing.get('item_id')}"}
                _log_call("/claw_zotero/items/add", req.model_dump(), resp)
                return resp
    if req.citation_key:
        for existing in _items:
            if existing.get("citation_key") == req.citation_key:
                resp = {"status": "conflict",
                        "error": f"citation_key conflict: already used by {existing.get('item_id')}"}
                _log_call("/claw_zotero/items/add", req.model_dump(), resp)
                return resp
    now = frozen_now().isoformat()
    new_id = _allocate_item_id()
    item = {
        "item_id": new_id,
        "item_type": req.item_type,
        "title": req.title,
        "authors": list(req.authors),
        "year": req.year,
        "venue": req.venue,
        "doi": req.doi,
        "url": req.url,
        "abstract": req.abstract,
        "tags": list(req.tags),
        "citation_key": req.citation_key,
        "collection_ids": [],
        "notes": [],
        "date_added": now,
        "date_modified": now,
    }
    _items.append(item)
    _added.append({
        "item_id": new_id,
        "title": req.title,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    resp = {"status": "added", "item": copy.deepcopy(item)}
    _log_call("/claw_zotero/items/add", req.model_dump(), resp)
    return resp


@app.post("/claw_zotero/items/update")
def update_item(req: UpdateItemRequest) -> dict[str, Any]:
    item = _find_item(req.item_id)
    if item is None:
        resp: dict[str, Any] = {"error": f"Item {req.item_id} not found"}
        _log_call("/claw_zotero/items/update", req.model_dump(), resp)
        return resp
    bad = [k for k in req.fields.keys() if k not in UPDATABLE_FIELDS]
    if bad:
        resp = {
            "error": f"Fields not updatable via update_item: {bad}. Allowed: {sorted(UPDATABLE_FIELDS)}",
        }
        _log_call("/claw_zotero/items/update", req.model_dump(), resp)
        return resp
    if "item_type" in req.fields and req.fields["item_type"] not in ITEM_TYPES:
        resp = {
            "error": f"Unknown item_type '{req.fields['item_type']}'. Allowed: {sorted(ITEM_TYPES)}",
        }
        _log_call("/claw_zotero/items/update", req.model_dump(), resp)
        return resp
    new_doi = req.fields.get("doi")
    if new_doi:
        for existing in _items:
            if existing.get("item_id") != req.item_id and existing.get("doi") == new_doi:
                resp = {"status": "conflict",
                        "error": f"DOI conflict: already exists as {existing.get('item_id')}"}
                _log_call("/claw_zotero/items/update", req.model_dump(), resp)
                return resp
    new_key = req.fields.get("citation_key")
    if new_key:
        for existing in _items:
            if existing.get("item_id") != req.item_id and existing.get("citation_key") == new_key:
                resp = {"status": "conflict",
                        "error": f"citation_key conflict: already used by {existing.get('item_id')}"}
                _log_call("/claw_zotero/items/update", req.model_dump(), resp)
                return resp
    for k, v in req.fields.items():
        item[k] = v
    item["date_modified"] = frozen_now().isoformat()
    _updated.append({
        "item_id": req.item_id,
        "fields": sorted(req.fields.keys()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    resp = {"status": "updated", "item_id": req.item_id}
    _log_call("/claw_zotero/items/update", req.model_dump(), resp)
    return resp


@app.post("/claw_zotero/items/add_note")
def add_note(req: AddNoteRequest) -> dict[str, Any]:
    item = _find_item(req.item_id)
    if item is None:
        resp: dict[str, Any] = {"error": f"Item {req.item_id} not found"}
        _log_call("/claw_zotero/items/add_note", req.model_dump(), resp)
        return resp
    now = frozen_now().isoformat()
    new_note_id = _allocate_note_id()
    note = {"note_id": new_note_id, "text": req.text, "created_at": now}
    item.setdefault("notes", []).append(note)
    item["date_modified"] = now
    _notes_added.append({
        "item_id": req.item_id,
        "note_id": new_note_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    resp = {"status": "note_added", "item_id": req.item_id, "note_id": new_note_id}
    _log_call("/claw_zotero/items/add_note", req.model_dump(), resp)
    return resp


@app.post("/claw_zotero/items/add_to_collection")
def add_to_collection(req: AddToCollectionRequest) -> dict[str, Any]:
    item = _find_item(req.item_id)
    if item is None:
        resp: dict[str, Any] = {"error": f"Item {req.item_id} not found"}
        _log_call("/claw_zotero/items/add_to_collection", req.model_dump(), resp)
        return resp
    col = _find_collection(req.collection_id)
    if col is None:
        resp = {"error": f"Collection {req.collection_id} not found"}
        _log_call("/claw_zotero/items/add_to_collection", req.model_dump(), resp)
        return resp
    col_ids = item.setdefault("collection_ids", [])
    already = req.collection_id in col_ids
    if not already:
        col_ids.append(req.collection_id)
    item["date_modified"] = frozen_now().isoformat()
    _collection_events.append({
        "action": "add",
        "item_id": req.item_id,
        "collection_id": req.collection_id,
        "noop": already,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    resp = {
        "status": "already_in_collection" if already else "added_to_collection",
        "item_id": req.item_id,
        "collection_id": req.collection_id,
    }
    _log_call("/claw_zotero/items/add_to_collection", req.model_dump(), resp)
    return resp


@app.post("/claw_zotero/items/delete")
def delete_item(req: DeleteItemRequest) -> dict[str, Any]:
    item = _find_item(req.item_id)
    if item is None:
        resp: dict[str, Any] = {"error": f"Item {req.item_id} not found"}
        _log_call("/claw_zotero/items/delete", req.model_dump(), resp)
        return resp
    _items.remove(item)
    _deleted.append({
        "item_id": req.item_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    resp = {"status": "deleted", "item_id": req.item_id}
    _log_call("/claw_zotero/items/delete", req.model_dump(), resp)
    return resp


@app.post("/claw_zotero/items/remove_from_collection")
def remove_from_collection(req: RemoveFromCollectionRequest) -> dict[str, Any]:
    item = _find_item(req.item_id)
    if item is None:
        resp: dict[str, Any] = {"error": f"Item {req.item_id} not found"}
        _log_call("/claw_zotero/items/remove_from_collection", req.model_dump(), resp)
        return resp
    col_ids = item.get("collection_ids") or []
    was_in = req.collection_id in col_ids
    item["collection_ids"] = [x for x in col_ids if x != req.collection_id]
    item["date_modified"] = frozen_now().isoformat()
    _collection_events.append({
        "action": "remove",
        "item_id": req.item_id,
        "collection_id": req.collection_id,
        "noop": not was_in,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    resp = {
        "status": "removed_from_collection" if was_in else "not_in_collection",
        "item_id": req.item_id,
        "collection_id": req.collection_id,
    }
    _log_call("/claw_zotero/items/remove_from_collection", req.model_dump(), resp)
    return resp


@app.get("/claw_zotero/audit")
def get_audit() -> dict[str, Any]:
    return {
        "calls": _audit_log,
        "added": _added,
        "updated": _updated,
        "deleted": _deleted,
        "notes_added": _notes_added,
        "collection_events": _collection_events,
    }


@app.post("/claw_zotero/reset")
def reset_state() -> dict[str, str]:
    global _audit_log, _added, _updated, _deleted, _notes_added, _collection_events
    _audit_log = []
    _added = []
    _updated = []
    _deleted = []
    _notes_added = []
    _collection_events = []
    _load_fixtures()
    return {"status": "reset"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "9117")))
