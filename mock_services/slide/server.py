"""Mock Slide API service (presentation decks) for agent evaluation (FastAPI on port 9134)."""

from __future__ import annotations

import json
import copy
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from fastapi import FastAPI
from pydantic import BaseModel, Field

app = FastAPI(title="Mock Slide API")

from mock_services._base import add_error_injection, frozen_now
add_error_injection(app)

FIXTURES_PATH = Path(os.environ.get(
    "SLIDE_FIXTURES",
    str(Path(__file__).resolve().parent.parent.parent / "new_tasks" / "_service_smoke" / "fixtures" / "slide" / "decks.json"),
))

_VALID_LAYOUTS = {"title", "title_content", "section", "blank"}
_VALID_ELEMENT_TYPES = {"text", "bullets", "image"}

# In-memory state
_decks: list[dict[str, Any]] = []
_audit_log: list[dict[str, Any]] = []
_updates: list[dict[str, Any]] = []
_added_elements: list[dict[str, Any]] = []
_added_slides: list[dict[str, Any]] = []
_reordered: list[dict[str, Any]] = []
_created_decks: list[dict[str, Any]] = []
_deleted: list[dict[str, Any]] = []


def _load_fixtures() -> None:
    global _decks
    with open(FIXTURES_PATH) as f:
        _decks = json.load(f)


_load_fixtures()


def _log_call(endpoint: str, request_body: dict[str, Any], response_body: Any) -> None:
    _audit_log.append({
        "endpoint": endpoint,
        "request_body": request_body,
        "response_body": response_body,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


def _allocate_deck_id() -> str:
    max_num = 0
    for deck in _decks:
        did = deck.get("deck_id", "")
        m = re.match(r"^DECK-(\d+)$", did)
        if m:
            max_num = max(max_num, int(m.group(1)))
    return f"DECK-{max_num + 1:04d}"


def _find_deck(deck_id: str) -> dict[str, Any] | None:
    for d in _decks:
        if d.get("deck_id") == deck_id:
            return d
    return None


def _slide_at(deck: dict[str, Any], slide_index: int) -> dict[str, Any] | None:
    """1-based slide_index lookup."""
    slides = deck.get("slides", [])
    if slide_index < 1 or slide_index > len(slides):
        return None
    return slides[slide_index - 1]


def _allocate_element_id(deck: dict[str, Any]) -> str:
    """Globally next E001/E002... within the deck (scans every slide)."""
    max_num = 0
    for s in deck.get("slides", []):
        for e in s.get("elements", []):
            eid = e.get("element_id", "")
            m = re.match(r"^E(\d+)$", eid)
            if m:
                max_num = max(max_num, int(m.group(1)))
    return f"E{max_num + 1:03d}"


def _find_element(slide: dict[str, Any], element_id: str) -> dict[str, Any] | None:
    for e in slide.get("elements", []):
        if e.get("element_id") == element_id:
            return e
    return None


def _deck_summary(deck: dict[str, Any]) -> dict[str, Any]:
    return {
        "deck_id": deck.get("deck_id"),
        "name": deck.get("name"),
        "owner": deck.get("owner"),
        "slide_count": len(deck.get("slides", [])),
        "last_updated": deck.get("last_updated"),
    }


def _slide_summary(slide: dict[str, Any], idx: int) -> dict[str, Any]:
    return {
        "slide_index": idx,
        "title": slide.get("title"),
        "layout": slide.get("layout"),
        "element_count": len(slide.get("elements", [])),
    }


# --- Request models ---


class ListDecksRequest(BaseModel):
    owner: str | None = None


class OpenDeckRequest(BaseModel):
    deck_id: str


class GetSlideRequest(BaseModel):
    deck_id: str
    slide_index: int


class SetSlideContentRequest(BaseModel):
    deck_id: str
    slide_index: int
    title: str | None = None
    notes: str | None = None
    layout: str | None = None


class AddElementRequest(BaseModel):
    deck_id: str
    slide_index: int
    type: str
    content: Any


class UpdateElementRequest(BaseModel):
    deck_id: str
    slide_index: int
    element_id: str
    content: Any


class AddSlideRequest(BaseModel):
    deck_id: str
    after_index: int
    title: str | None = None
    layout: str = "blank"
    notes: str | None = None


class DuplicateSlideRequest(BaseModel):
    deck_id: str
    slide_index: int


class ReorderSlideRequest(BaseModel):
    deck_id: str
    from_index: int
    to_index: int


class CreateDeckRequest(BaseModel):
    name: str
    owner: str
    initial_slides: list[dict[str, Any]] = Field(default_factory=list)


class DeleteDeckRequest(BaseModel):
    deck_id: str


class DeleteSlideRequest(BaseModel):
    deck_id: str
    slide_index: int


# --- Endpoints ---


@app.post("/slide/decks")
def list_decks(req: ListDecksRequest | None = None) -> dict[str, Any]:
    if req is None:
        req = ListDecksRequest()
    results = []
    for d in _decks:
        if req.owner is not None and d.get("owner") != req.owner:
            continue
        results.append(_deck_summary(d))
    resp = {"decks": results, "total": len(results)}
    _log_call("/slide/decks", req.model_dump(), resp)
    return resp


@app.post("/slide/open")
def open_deck(req: OpenDeckRequest) -> dict[str, Any]:
    deck = _find_deck(req.deck_id)
    if deck is None:
        resp = {"error": f"Deck {req.deck_id} not found"}
        _log_call("/slide/open", req.model_dump(), resp)
        return resp
    slides_meta = [_slide_summary(s, i + 1) for i, s in enumerate(deck.get("slides", []))]
    resp = {
        "deck_id": deck.get("deck_id"),
        "name": deck.get("name"),
        "owner": deck.get("owner"),
        "created_at": deck.get("created_at"),
        "last_updated": deck.get("last_updated"),
        "slides": slides_meta,
    }
    _log_call("/slide/open", req.model_dump(), resp)
    return resp


@app.post("/slide/get_slide")
def get_slide(req: GetSlideRequest) -> dict[str, Any]:
    deck = _find_deck(req.deck_id)
    if deck is None:
        resp = {"error": f"Deck {req.deck_id} not found"}
        _log_call("/slide/get_slide", req.model_dump(), resp)
        return resp
    slide = _slide_at(deck, req.slide_index)
    if slide is None:
        resp = {"error": f"Slide index {req.slide_index} out of range for {req.deck_id}"}
        _log_call("/slide/get_slide", req.model_dump(), resp)
        return resp
    resp = {
        "slide_index": req.slide_index,
        "title": slide.get("title"),
        "layout": slide.get("layout"),
        "notes": slide.get("notes"),
        "elements": copy.deepcopy(slide.get("elements", [])),
    }
    _log_call("/slide/get_slide", req.model_dump(), resp)
    return resp


@app.post("/slide/set_slide_content")
def set_slide_content(req: SetSlideContentRequest) -> dict[str, Any]:
    deck = _find_deck(req.deck_id)
    if deck is None:
        resp = {"error": f"Deck {req.deck_id} not found"}
        _log_call("/slide/set_slide_content", req.model_dump(), resp)
        return resp
    slide = _slide_at(deck, req.slide_index)
    if slide is None:
        resp = {"error": f"Slide index {req.slide_index} out of range for {req.deck_id}"}
        _log_call("/slide/set_slide_content", req.model_dump(), resp)
        return resp
    if req.layout is not None and req.layout not in _VALID_LAYOUTS:
        resp = {"error": f"Invalid layout {req.layout!r}; choose from {sorted(_VALID_LAYOUTS)}"}
        _log_call("/slide/set_slide_content", req.model_dump(), resp)
        return resp
    if req.title is None and req.notes is None and req.layout is None:
        resp = {"error": "At least one of title / notes / layout must be provided"}
        _log_call("/slide/set_slide_content", req.model_dump(), resp)
        return resp
    changes: dict[str, Any] = {}
    if req.title is not None:
        changes["title"] = {"previous": slide.get("title"), "new": req.title}
        slide["title"] = req.title
    if req.notes is not None:
        changes["notes"] = {"previous": slide.get("notes"), "new": req.notes}
        slide["notes"] = req.notes
    if req.layout is not None:
        changes["layout"] = {"previous": slide.get("layout"), "new": req.layout}
        slide["layout"] = req.layout
    _updates.append({
        "deck_id": req.deck_id,
        "slide_index": req.slide_index,
        "changes": changes,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    deck["last_updated"] = frozen_now().strftime("%Y-%m-%d")
    resp = {"ok": True, "changes": changes}
    _log_call("/slide/set_slide_content", req.model_dump(), resp)
    return resp


@app.post("/slide/add_element")
def add_element(req: AddElementRequest) -> dict[str, Any]:
    deck = _find_deck(req.deck_id)
    if deck is None:
        resp = {"error": f"Deck {req.deck_id} not found"}
        _log_call("/slide/add_element", req.model_dump(), resp)
        return resp
    slide = _slide_at(deck, req.slide_index)
    if slide is None:
        resp = {"error": f"Slide index {req.slide_index} out of range for {req.deck_id}"}
        _log_call("/slide/add_element", req.model_dump(), resp)
        return resp
    if req.type not in _VALID_ELEMENT_TYPES:
        resp = {"error": f"Invalid element type {req.type!r}; choose from {sorted(_VALID_ELEMENT_TYPES)}"}
        _log_call("/slide/add_element", req.model_dump(), resp)
        return resp
    new_id = _allocate_element_id(deck)
    element = {"element_id": new_id, "type": req.type, "content": req.content}
    slide.setdefault("elements", []).append(element)
    _added_elements.append({
        "deck_id": req.deck_id,
        "slide_index": req.slide_index,
        "element_id": new_id,
        "type": req.type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    deck["last_updated"] = frozen_now().strftime("%Y-%m-%d")
    resp = {"ok": True, "element_id": new_id}
    _log_call("/slide/add_element", req.model_dump(), resp)
    return resp


@app.post("/slide/update_element")
def update_element(req: UpdateElementRequest) -> dict[str, Any]:
    deck = _find_deck(req.deck_id)
    if deck is None:
        resp = {"error": f"Deck {req.deck_id} not found"}
        _log_call("/slide/update_element", req.model_dump(), resp)
        return resp
    slide = _slide_at(deck, req.slide_index)
    if slide is None:
        resp = {"error": f"Slide index {req.slide_index} out of range for {req.deck_id}"}
        _log_call("/slide/update_element", req.model_dump(), resp)
        return resp
    element = _find_element(slide, req.element_id)
    if element is None:
        resp = {"error": f"Element {req.element_id} not found on slide {req.slide_index}"}
        _log_call("/slide/update_element", req.model_dump(), resp)
        return resp
    previous_content = copy.deepcopy(element.get("content"))
    element["content"] = req.content
    _updates.append({
        "deck_id": req.deck_id,
        "slide_index": req.slide_index,
        "element_id": req.element_id,
        "previous_content": previous_content,
        "new_content": req.content,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    deck["last_updated"] = frozen_now().strftime("%Y-%m-%d")
    resp = {"ok": True, "previous_content": previous_content}
    _log_call("/slide/update_element", req.model_dump(), resp)
    return resp


@app.post("/slide/add_slide")
def add_slide(req: AddSlideRequest) -> dict[str, Any]:
    deck = _find_deck(req.deck_id)
    if deck is None:
        resp = {"error": f"Deck {req.deck_id} not found"}
        _log_call("/slide/add_slide", req.model_dump(), resp)
        return resp
    slides = deck.setdefault("slides", [])
    if req.after_index < 0 or req.after_index > len(slides):
        resp = {"error": f"after_index must be in [0, {len(slides)}], got {req.after_index}"}
        _log_call("/slide/add_slide", req.model_dump(), resp)
        return resp
    if req.layout not in _VALID_LAYOUTS:
        resp = {"error": f"Invalid layout {req.layout!r}; choose from {sorted(_VALID_LAYOUTS)}"}
        _log_call("/slide/add_slide", req.model_dump(), resp)
        return resp
    new_slide = {
        "title": req.title,
        "layout": req.layout,
        "notes": req.notes,
        "elements": [],
    }
    new_index = req.after_index + 1
    slides.insert(req.after_index, new_slide)
    _added_slides.append({
        "deck_id": req.deck_id,
        "slide_index": new_index,
        "kind": "add",
        "title": req.title,
        "layout": req.layout,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    deck["last_updated"] = frozen_now().strftime("%Y-%m-%d")
    resp = {"ok": True, "slide_index": new_index, "total_slides": len(slides)}
    _log_call("/slide/add_slide", req.model_dump(), resp)
    return resp


@app.post("/slide/duplicate_slide")
def duplicate_slide(req: DuplicateSlideRequest) -> dict[str, Any]:
    deck = _find_deck(req.deck_id)
    if deck is None:
        resp = {"error": f"Deck {req.deck_id} not found"}
        _log_call("/slide/duplicate_slide", req.model_dump(), resp)
        return resp
    slides = deck.setdefault("slides", [])
    if req.slide_index < 1 or req.slide_index > len(slides):
        resp = {"error": f"Slide index {req.slide_index} out of range for {req.deck_id}"}
        _log_call("/slide/duplicate_slide", req.model_dump(), resp)
        return resp
    src = slides[req.slide_index - 1]
    duplicate = copy.deepcopy(src)
    new_index = req.slide_index + 1
    slides.insert(new_index - 1, duplicate)
    # Clear duplicate element ids so the allocator's max-scan doesn't see the
    # collisions, then re-id one at a time so subsequent allocations include
    # the previously assigned new ids.
    for e in duplicate.get("elements", []):
        e["element_id"] = ""
    duplicate_ids: list[str] = []
    for e in duplicate.get("elements", []):
        new_eid = _allocate_element_id(deck)
        e["element_id"] = new_eid
        duplicate_ids.append(new_eid)
    _added_slides.append({
        "deck_id": req.deck_id,
        "slide_index": new_index,
        "kind": "duplicate",
        "source_index": req.slide_index,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    deck["last_updated"] = frozen_now().strftime("%Y-%m-%d")
    resp = {"ok": True, "slide_index": new_index, "element_ids": duplicate_ids}
    _log_call("/slide/duplicate_slide", req.model_dump(), resp)
    return resp


@app.post("/slide/reorder_slide")
def reorder_slide(req: ReorderSlideRequest) -> dict[str, Any]:
    deck = _find_deck(req.deck_id)
    if deck is None:
        resp = {"error": f"Deck {req.deck_id} not found"}
        _log_call("/slide/reorder_slide", req.model_dump(), resp)
        return resp
    slides = deck.setdefault("slides", [])
    if req.from_index < 1 or req.from_index > len(slides):
        resp = {"error": f"from_index {req.from_index} out of range for {req.deck_id}"}
        _log_call("/slide/reorder_slide", req.model_dump(), resp)
        return resp
    if req.to_index < 1 or req.to_index > len(slides):
        resp = {"error": f"to_index {req.to_index} out of range for {req.deck_id}"}
        _log_call("/slide/reorder_slide", req.model_dump(), resp)
        return resp
    if req.from_index == req.to_index:
        resp = {"ok": True, "from_index": req.from_index, "to_index": req.to_index, "no_op": True}
        _log_call("/slide/reorder_slide", req.model_dump(), resp)
        return resp
    moved = slides.pop(req.from_index - 1)
    slides.insert(req.to_index - 1, moved)
    _reordered.append({
        "deck_id": req.deck_id,
        "from_index": req.from_index,
        "to_index": req.to_index,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    deck["last_updated"] = frozen_now().strftime("%Y-%m-%d")
    resp = {"ok": True, "from_index": req.from_index, "to_index": req.to_index}
    _log_call("/slide/reorder_slide", req.model_dump(), resp)
    return resp


@app.post("/slide/create_deck")
def create_deck(req: CreateDeckRequest) -> dict[str, Any]:
    new_id = _allocate_deck_id()
    today = frozen_now().strftime("%Y-%m-%d")
    slides: list[dict[str, Any]] = []
    if req.initial_slides:
        for s in req.initial_slides:
            layout = s.get("layout", "blank")
            if layout not in _VALID_LAYOUTS:
                resp = {"error": f"Invalid layout {layout!r}; choose from {sorted(_VALID_LAYOUTS)}"}
                _log_call("/slide/create_deck", req.model_dump(), resp)
                return resp
            slides.append({
                "title": s.get("title"),
                "layout": layout,
                "notes": s.get("notes"),
                "elements": list(s.get("elements", []) or []),
            })
    else:
        slides.append({"title": None, "layout": "blank", "notes": None, "elements": []})
    deck = {
        "deck_id": new_id,
        "name": req.name,
        "owner": req.owner,
        "created_at": today,
        "last_updated": today,
        "slides": slides,
    }
    _decks.append(deck)
    _created_decks.append({
        "deck_id": new_id,
        "name": req.name,
        "owner": req.owner,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    resp = {"ok": True, "deck_id": new_id, "slide_count": len(slides)}
    _log_call("/slide/create_deck", req.model_dump(), resp)
    return resp


@app.post("/slide/delete_deck")
def delete_deck(req: DeleteDeckRequest) -> dict[str, Any]:
    deck = _find_deck(req.deck_id)
    if deck is None:
        resp = {"error": f"Deck {req.deck_id} not found"}
        _log_call("/slide/delete_deck", req.model_dump(), resp)
        return resp
    _decks.remove(deck)
    _deleted.append({
        "kind": "deck",
        "deck_id": req.deck_id,
        "name": deck.get("name"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    resp = {"ok": True}
    _log_call("/slide/delete_deck", req.model_dump(), resp)
    return resp


@app.post("/slide/delete_slide")
def delete_slide(req: DeleteSlideRequest) -> dict[str, Any]:
    deck = _find_deck(req.deck_id)
    if deck is None:
        resp = {"error": f"Deck {req.deck_id} not found"}
        _log_call("/slide/delete_slide", req.model_dump(), resp)
        return resp
    slides = deck.get("slides", [])
    if req.slide_index < 1 or req.slide_index > len(slides):
        resp = {"error": f"Slide index {req.slide_index} out of range for {req.deck_id}"}
        _log_call("/slide/delete_slide", req.model_dump(), resp)
        return resp
    removed = slides.pop(req.slide_index - 1)
    _deleted.append({
        "kind": "slide",
        "deck_id": req.deck_id,
        "slide_index": req.slide_index,
        "title": removed.get("title"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    resp = {"ok": True}
    _log_call("/slide/delete_slide", req.model_dump(), resp)
    return resp


@app.get("/slide/audit")
def get_audit() -> dict[str, Any]:
    return {
        "calls": _audit_log,
        "updates": _updates,
        "added_elements": _added_elements,
        "added_slides": _added_slides,
        "reordered": _reordered,
        "created_decks": _created_decks,
        "deleted": _deleted,
    }


@app.post("/slide/reset")
def reset_state() -> dict[str, str]:
    global _audit_log, _updates, _added_elements, _added_slides, _reordered, _created_decks, _deleted
    _audit_log = []
    _updates = []
    _added_elements = []
    _added_slides = []
    _reordered = []
    _created_decks = []
    _deleted = []
    _load_fixtures()
    return {"status": "reset"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "9134")))
