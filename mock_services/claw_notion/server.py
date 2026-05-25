"""Mock Claw-Notion API service (page + block + database) for agent evaluation (FastAPI on port 9121).

Model:
- A page has page_id, title, parent_page_id (tree), database_id (optional),
  properties (structured key-value), blocks (ordered list), archived flag.
- append_blocks appends to the blocks list; update_page_properties only edits the
  properties dict; archive_page flips the archived flag.
- query_database filters pages by database_id + equality on property keys.
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

app = FastAPI(title="Mock Claw-Notion API")

from mock_services._base import add_error_injection, frozen_now
add_error_injection(app)

FIXTURES_PATH = Path(os.environ.get(
    "CLAW_NOTION_FIXTURES",
    str(Path(__file__).resolve().parent.parent.parent / "new_tasks" / "_service_smoke" / "fixtures" / "claw_notion" / "pages.json"),
))

_pages: list[dict[str, Any]] = []
_audit_log: list[dict[str, Any]] = []
_created: list[dict[str, Any]] = []
_updated: list[dict[str, Any]] = []
_appended: list[dict[str, Any]] = []
_archived: list[dict[str, Any]] = []


def _load_fixtures() -> None:
    global _pages
    with open(FIXTURES_PATH) as f:
        _pages = json.load(f)


_load_fixtures()


def _log_call(endpoint: str, request_body: dict[str, Any], response_body: Any) -> None:
    _audit_log.append({
        "endpoint": endpoint,
        "request_body": request_body,
        "response_body": response_body,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


ID_PREFIX = "NPAG-"
ID_PAD = 4


def _allocate_page_id() -> str:
    max_num = 0
    for p in _pages:
        pid = p.get("page_id", "")
        if pid.startswith(ID_PREFIX):
            try:
                n = int(pid[len(ID_PREFIX):])
                if n > max_num:
                    max_num = n
            except ValueError:
                pass
    return f"{ID_PREFIX}{(max_num + 1):0{ID_PAD}d}"


# --- Request models ---


class ListPagesRequest(BaseModel):
    parent_page_id: str | None = None
    database_id: str | None = None
    include_archived: bool = False
    max_results: int = 20
    offset: int = 0


class GetPageRequest(BaseModel):
    page_id: str


class CreatePageRequest(BaseModel):
    title: str
    parent_page_id: str | None = None
    database_id: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)
    blocks: list[dict[str, Any]] = Field(default_factory=list)


class UpdatePagePropertiesRequest(BaseModel):
    page_id: str
    properties: dict[str, Any]


class AppendBlocksRequest(BaseModel):
    page_id: str
    blocks: list[dict[str, Any]]


class ArchivePageRequest(BaseModel):
    page_id: str


class SearchRequest(BaseModel):
    query: str
    max_results: int = 10
    offset: int = 0


class QueryDatabaseRequest(BaseModel):
    database_id: str
    property_filter: dict[str, Any] = Field(default_factory=dict)
    max_results: int = 20
    offset: int = 0


# --- Endpoints ---


def _summary(page: dict[str, Any]) -> dict[str, Any]:
    return {
        "page_id": page.get("page_id"),
        "title": page.get("title"),
        "parent_page_id": page.get("parent_page_id"),
        "database_id": page.get("database_id"),
        "archived": page.get("archived", False),
        "last_updated": page.get("last_updated"),
    }


@app.post("/claw_notion/pages")
def list_pages(req: ListPagesRequest | None = None) -> dict[str, Any]:
    if req is None:
        req = ListPagesRequest()
    filtered = []
    for page in _pages:
        if not req.include_archived and page.get("archived", False):
            continue
        if req.parent_page_id is not None and page.get("parent_page_id") != req.parent_page_id:
            continue
        if req.database_id is not None and page.get("database_id") != req.database_id:
            continue
        filtered.append(_summary(page))
    total = len(filtered)
    results = filtered[req.offset: req.offset + req.max_results]
    resp = {"pages": results, "total": total, "returned": len(results), "offset": req.offset}
    _log_call("/claw_notion/pages", req.model_dump(), resp)
    return resp


@app.post("/claw_notion/pages/get")
def get_page(req: GetPageRequest) -> dict[str, Any]:
    for page in _pages:
        if page.get("page_id") == req.page_id:
            resp = copy.deepcopy(page)
            _log_call("/claw_notion/pages/get", req.model_dump(), resp)
            return resp
    resp = {"error": f"Page {req.page_id} not found"}
    _log_call("/claw_notion/pages/get", req.model_dump(), resp)
    return resp


@app.post("/claw_notion/pages/create")
def create_page(req: CreatePageRequest) -> dict[str, Any]:
    now = frozen_now().isoformat()
    new_id = _allocate_page_id()
    page = {
        "page_id": new_id,
        "title": req.title,
        "parent_page_id": req.parent_page_id,
        "database_id": req.database_id,
        "properties": dict(req.properties),
        "blocks": list(req.blocks),
        "archived": False,
        "created_at": now,
        "last_updated": now,
    }
    _pages.append(page)
    record = {"page_id": new_id, "title": req.title, "timestamp": datetime.now(timezone.utc).isoformat()}
    _created.append(record)
    resp = {"status": "created", "page": copy.deepcopy(page)}
    _log_call("/claw_notion/pages/create", req.model_dump(), resp)
    return resp


@app.post("/claw_notion/pages/update_properties")
def update_page_properties(req: UpdatePagePropertiesRequest) -> dict[str, Any]:
    for page in _pages:
        if page.get("page_id") == req.page_id:
            props = page.setdefault("properties", {})
            props.update(req.properties)
            page["last_updated"] = frozen_now().isoformat()
            record = {
                "page_id": req.page_id,
                "property_keys": list(req.properties.keys()),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            _updated.append(record)
            resp = {"status": "updated", "page_id": req.page_id, "properties": dict(props)}
            _log_call("/claw_notion/pages/update_properties", req.model_dump(), resp)
            return resp
    resp = {"error": f"Page {req.page_id} not found"}
    _log_call("/claw_notion/pages/update_properties", req.model_dump(), resp)
    return resp


@app.post("/claw_notion/pages/append_blocks")
def append_blocks(req: AppendBlocksRequest) -> dict[str, Any]:
    for page in _pages:
        if page.get("page_id") == req.page_id:
            blocks = page.setdefault("blocks", [])
            blocks.extend(req.blocks)
            page["last_updated"] = frozen_now().isoformat()
            record = {
                "page_id": req.page_id,
                "block_count": len(req.blocks),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            _appended.append(record)
            resp = {"status": "appended", "page_id": req.page_id, "total_blocks": len(blocks)}
            _log_call("/claw_notion/pages/append_blocks", req.model_dump(), resp)
            return resp
    resp = {"error": f"Page {req.page_id} not found"}
    _log_call("/claw_notion/pages/append_blocks", req.model_dump(), resp)
    return resp


@app.post("/claw_notion/pages/archive")
def archive_page(req: ArchivePageRequest) -> dict[str, Any]:
    for page in _pages:
        if page.get("page_id") == req.page_id:
            page["archived"] = True
            page["last_updated"] = frozen_now().isoformat()
            record = {"page_id": req.page_id, "timestamp": datetime.now(timezone.utc).isoformat()}
            _archived.append(record)
            resp = {"status": "archived", "page_id": req.page_id}
            _log_call("/claw_notion/pages/archive", req.model_dump(), resp)
            return resp
    resp = {"error": f"Page {req.page_id} not found"}
    _log_call("/claw_notion/pages/archive", req.model_dump(), resp)
    return resp


def _text_of_blocks(blocks: list[dict[str, Any]]) -> str:
    parts = []
    for b in blocks:
        t = b.get("text", "")
        if isinstance(t, str):
            parts.append(t)
    return " ".join(parts)


@app.post("/claw_notion/search")
def search_pages(req: SearchRequest) -> dict[str, Any]:
    query_words = req.query.lower().split()
    results: list[dict[str, Any]] = []
    for page in _pages:
        if page.get("archived", False):
            continue
        prop_text = " ".join(str(v) for v in (page.get("properties") or {}).values())
        searchable = (
            (page.get("title") or "") + " " + prop_text + " " + _text_of_blocks(page.get("blocks") or [])
        ).lower()
        if all(w in searchable for w in query_words):
            results.append(_summary(page))
    total = len(results)
    results = results[req.offset: req.offset + req.max_results]
    resp = {"pages": results, "total": total, "returned": len(results), "offset": req.offset}
    _log_call("/claw_notion/search", req.model_dump(), resp)
    return resp


@app.post("/claw_notion/database/query")
def query_database(req: QueryDatabaseRequest) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for page in _pages:
        if page.get("archived", False):
            continue
        if page.get("database_id") != req.database_id:
            continue
        props = page.get("properties") or {}
        if all(props.get(k) == v for k, v in req.property_filter.items()):
            results.append(_summary(page))
    total = len(results)
    results = results[req.offset: req.offset + req.max_results]
    resp = {"pages": results, "total": total, "returned": len(results), "offset": req.offset}
    _log_call("/claw_notion/database/query", req.model_dump(), resp)
    return resp


@app.get("/claw_notion/audit")
def get_audit() -> dict[str, Any]:
    return {
        "calls": _audit_log,
        "created": _created,
        "updated": _updated,
        "appended": _appended,
        "archived": _archived,
    }


@app.post("/claw_notion/reset")
def reset_state() -> dict[str, str]:
    global _audit_log, _created, _updated, _appended, _archived
    _audit_log = []
    _created = []
    _updated = []
    _appended = []
    _archived = []
    _load_fixtures()
    return {"status": "reset"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "9114")))
