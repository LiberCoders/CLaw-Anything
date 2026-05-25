"""Mock Sheet API service (spreadsheet workbooks) for agent evaluation (FastAPI on port 9133)."""

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

app = FastAPI(title="Mock Sheet API")

from mock_services._base import add_error_injection, frozen_now
add_error_injection(app)

FIXTURES_PATH = Path(os.environ.get(
    "SHEET_FIXTURES",
    str(Path(__file__).resolve().parent.parent.parent / "new_tasks" / "_service_smoke" / "fixtures" / "sheet" / "workbooks.json"),
))

# In-memory state
_workbooks: list[dict[str, Any]] = []
_audit_log: list[dict[str, Any]] = []
_updates: list[dict[str, Any]] = []
_inserts: list[dict[str, Any]] = []
_added_sheets: list[dict[str, Any]] = []
_renamed: list[dict[str, Any]] = []
_created_workbooks: list[dict[str, Any]] = []
_deleted: list[dict[str, Any]] = []


def _load_fixtures() -> None:
    global _workbooks
    with open(FIXTURES_PATH) as f:
        _workbooks = json.load(f)


_load_fixtures()


def _log_call(endpoint: str, request_body: dict[str, Any], response_body: Any) -> None:
    _audit_log.append({
        "endpoint": endpoint,
        "request_body": request_body,
        "response_body": response_body,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


def _allocate_workbook_id() -> str:
    max_num = 0
    for wb in _workbooks:
        wid = wb.get("workbook_id", "")
        m = re.match(r"^WB-(\d+)$", wid)
        if m:
            max_num = max(max_num, int(m.group(1)))
    return f"WB-{max_num + 1:04d}"


def _find_workbook(workbook_id: str) -> dict[str, Any] | None:
    for wb in _workbooks:
        if wb.get("workbook_id") == workbook_id:
            return wb
    return None


def _find_sheet(workbook: dict[str, Any], sheet_name: str) -> dict[str, Any] | None:
    for s in workbook.get("sheets", []):
        if s.get("name") == sheet_name:
            return s
    return None


# --- Cell reference helpers ---

_CELL_RE = re.compile(r"^([A-Z]+)(\d+)$")
_RANGE_RE = re.compile(r"^([A-Z]+\d+):([A-Z]+\d+)$")


def _col_to_idx(col: str) -> int:
    idx = 0
    for ch in col:
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


def _idx_to_col(idx: int) -> str:
    s = ""
    n = idx + 1
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(ord("A") + r) + s
    return s


def _parse_cell(cell: str) -> tuple[int, int] | None:
    m = _CELL_RE.match(cell)
    if not m:
        return None
    col_idx = _col_to_idx(m.group(1))
    row_idx = int(m.group(2))
    if row_idx < 1:
        return None
    return col_idx, row_idx


def _parse_range(rng: str) -> tuple[int, int, int, int] | None:
    m = _RANGE_RE.match(rng)
    if not m:
        return None
    a = _parse_cell(m.group(1))
    b = _parse_cell(m.group(2))
    if a is None or b is None:
        return None
    col_a, row_a = a
    col_b, row_b = b
    c_lo, c_hi = sorted([col_a, col_b])
    r_lo, r_hi = sorted([row_a, row_b])
    return c_lo, r_lo, c_hi, r_hi


def _sheet_summary(wb: dict[str, Any]) -> dict[str, Any]:
    return {
        "workbook_id": wb.get("workbook_id"),
        "name": wb.get("name"),
        "owner": wb.get("owner"),
        "sheet_names": [s.get("name") for s in wb.get("sheets", [])],
        "last_updated": wb.get("last_updated"),
    }


# --- Request models ---


class ListWorkbooksRequest(BaseModel):
    owner: str | None = None


class OpenWorkbookRequest(BaseModel):
    workbook_id: str


class GetRangeRequest(BaseModel):
    workbook_id: str
    sheet: str
    range: str


class SetCellRequest(BaseModel):
    workbook_id: str
    sheet: str
    cell: str
    value: Any


class SetRangeRequest(BaseModel):
    workbook_id: str
    sheet: str
    start_cell: str
    values: list[list[Any]]


class InsertRowRequest(BaseModel):
    workbook_id: str
    sheet: str
    after_row: int
    values: list[Any] = Field(default_factory=list)


class CreateWorkbookRequest(BaseModel):
    name: str
    owner: str
    initial_sheets: list[dict[str, Any]] = Field(default_factory=list)


class AddSheetRequest(BaseModel):
    workbook_id: str
    name: str


class RenameSheetRequest(BaseModel):
    workbook_id: str
    old_name: str
    new_name: str


class ComputeRequest(BaseModel):
    workbook_id: str
    sheet: str
    range: str
    op: str  # SUM | AVG | COUNT | MAX | MIN


class DeleteWorkbookRequest(BaseModel):
    workbook_id: str


class DeleteSheetRequest(BaseModel):
    workbook_id: str
    sheet: str


# --- Endpoints ---


@app.post("/sheet/workbooks")
def list_workbooks(req: ListWorkbooksRequest | None = None) -> dict[str, Any]:
    if req is None:
        req = ListWorkbooksRequest()
    results = []
    for wb in _workbooks:
        if req.owner is not None and wb.get("owner") != req.owner:
            continue
        results.append(_sheet_summary(wb))
    resp = {"workbooks": results, "total": len(results)}
    _log_call("/sheet/workbooks", req.model_dump(), resp)
    return resp


@app.post("/sheet/open")
def open_workbook(req: OpenWorkbookRequest) -> dict[str, Any]:
    wb = _find_workbook(req.workbook_id)
    if wb is None:
        resp = {"error": f"Workbook {req.workbook_id} not found"}
        _log_call("/sheet/open", req.model_dump(), resp)
        return resp
    resp = copy.deepcopy(wb)
    _log_call("/sheet/open", req.model_dump(), resp)
    return resp


@app.post("/sheet/get_range")
def get_range(req: GetRangeRequest) -> dict[str, Any]:
    wb = _find_workbook(req.workbook_id)
    if wb is None:
        resp = {"error": f"Workbook {req.workbook_id} not found"}
        _log_call("/sheet/get_range", req.model_dump(), resp)
        return resp
    sheet = _find_sheet(wb, req.sheet)
    if sheet is None:
        resp = {"error": f"Sheet {req.sheet!r} not found in {req.workbook_id}"}
        _log_call("/sheet/get_range", req.model_dump(), resp)
        return resp
    parsed = _parse_range(req.range)
    if parsed is None:
        resp = {"error": f"Invalid range {req.range!r}"}
        _log_call("/sheet/get_range", req.model_dump(), resp)
        return resp
    c_lo, r_lo, c_hi, r_hi = parsed
    cells_dict = sheet.get("cells", {}) or {}
    values: list[list[Any]] = []
    subset: dict[str, Any] = {}
    for row in range(r_lo, r_hi + 1):
        row_vals: list[Any] = []
        for col in range(c_lo, c_hi + 1):
            key = f"{_idx_to_col(col)}{row}"
            v = cells_dict.get(key)
            row_vals.append(v)
            if key in cells_dict:
                subset[key] = v
        values.append(row_vals)
    resp = {"range": req.range, "values": values, "cells": subset}
    _log_call("/sheet/get_range", req.model_dump(), resp)
    return resp


def _set_one_cell(sheet: dict[str, Any], workbook_id: str, cell: str, new_value: Any) -> dict[str, Any]:
    cells = sheet.setdefault("cells", {})
    previous = cells.get(cell)
    cells[cell] = new_value
    update_record = {
        "workbook_id": workbook_id,
        "sheet": sheet.get("name"),
        "cell": cell,
        "previous_value": previous,
        "new_value": new_value,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _updates.append(update_record)
    return update_record


@app.post("/sheet/set_cell")
def set_cell(req: SetCellRequest) -> dict[str, Any]:
    wb = _find_workbook(req.workbook_id)
    if wb is None:
        resp = {"error": f"Workbook {req.workbook_id} not found"}
        _log_call("/sheet/set_cell", req.model_dump(), resp)
        return resp
    sheet = _find_sheet(wb, req.sheet)
    if sheet is None:
        resp = {"error": f"Sheet {req.sheet!r} not found in {req.workbook_id}"}
        _log_call("/sheet/set_cell", req.model_dump(), resp)
        return resp
    if _parse_cell(req.cell) is None:
        resp = {"error": f"Invalid cell {req.cell!r}"}
        _log_call("/sheet/set_cell", req.model_dump(), resp)
        return resp
    rec = _set_one_cell(sheet, req.workbook_id, req.cell, req.value)
    wb["last_updated"] = frozen_now().strftime("%Y-%m-%d")
    resp = {"ok": True, "previous_value": rec["previous_value"], "new_value": rec["new_value"]}
    _log_call("/sheet/set_cell", req.model_dump(), resp)
    return resp


@app.post("/sheet/set_range")
def set_range(req: SetRangeRequest) -> dict[str, Any]:
    wb = _find_workbook(req.workbook_id)
    if wb is None:
        resp = {"error": f"Workbook {req.workbook_id} not found"}
        _log_call("/sheet/set_range", req.model_dump(), resp)
        return resp
    sheet = _find_sheet(wb, req.sheet)
    if sheet is None:
        resp = {"error": f"Sheet {req.sheet!r} not found in {req.workbook_id}"}
        _log_call("/sheet/set_range", req.model_dump(), resp)
        return resp
    parsed = _parse_cell(req.start_cell)
    if parsed is None:
        resp = {"error": f"Invalid start_cell {req.start_cell!r}"}
        _log_call("/sheet/set_range", req.model_dump(), resp)
        return resp
    col_start, row_start = parsed
    written = 0
    for r_off, row_vals in enumerate(req.values):
        for c_off, val in enumerate(row_vals):
            key = f"{_idx_to_col(col_start + c_off)}{row_start + r_off}"
            _set_one_cell(sheet, req.workbook_id, key, val)
            written += 1
    wb["last_updated"] = frozen_now().strftime("%Y-%m-%d")
    resp = {"ok": True, "cells_updated": written}
    _log_call("/sheet/set_range", req.model_dump(), resp)
    return resp


@app.post("/sheet/insert_row")
def insert_row(req: InsertRowRequest) -> dict[str, Any]:
    wb = _find_workbook(req.workbook_id)
    if wb is None:
        resp = {"error": f"Workbook {req.workbook_id} not found"}
        _log_call("/sheet/insert_row", req.model_dump(), resp)
        return resp
    sheet = _find_sheet(wb, req.sheet)
    if sheet is None:
        resp = {"error": f"Sheet {req.sheet!r} not found in {req.workbook_id}"}
        _log_call("/sheet/insert_row", req.model_dump(), resp)
        return resp
    if req.after_row < 0:
        resp = {"error": f"after_row must be >= 0, got {req.after_row}"}
        _log_call("/sheet/insert_row", req.model_dump(), resp)
        return resp
    cells = sheet.setdefault("cells", {})
    new_row = req.after_row + 1
    # Shift existing rows >= new_row down by 1
    shifted: dict[str, Any] = {}
    to_remove: list[str] = []
    for key, val in list(cells.items()):
        m = _CELL_RE.match(key)
        if not m:
            continue
        col_part = m.group(1)
        row_num = int(m.group(2))
        if row_num >= new_row:
            new_key = f"{col_part}{row_num + 1}"
            shifted[new_key] = val
            to_remove.append(key)
    for key in to_remove:
        del cells[key]
    cells.update(shifted)
    # Write new row values starting at column A
    for c_off, val in enumerate(req.values):
        key = f"{_idx_to_col(c_off)}{new_row}"
        cells[key] = val
    _inserts.append({
        "workbook_id": req.workbook_id,
        "sheet": req.sheet,
        "row": new_row,
        "values": list(req.values),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    wb["last_updated"] = frozen_now().strftime("%Y-%m-%d")
    resp = {"ok": True, "inserted_at_row": new_row}
    _log_call("/sheet/insert_row", req.model_dump(), resp)
    return resp


@app.post("/sheet/create_workbook")
def create_workbook(req: CreateWorkbookRequest) -> dict[str, Any]:
    new_id = _allocate_workbook_id()
    today = frozen_now().strftime("%Y-%m-%d")
    sheets: list[dict[str, Any]] = []
    if req.initial_sheets:
        for s in req.initial_sheets:
            sheets.append({
                "name": s.get("name", "Sheet1"),
                "cells": dict(s.get("cells", {}) or {}),
            })
    else:
        sheets.append({"name": "Sheet1", "cells": {}})
    wb = {
        "workbook_id": new_id,
        "name": req.name,
        "owner": req.owner,
        "created_at": today,
        "last_updated": today,
        "sheets": sheets,
    }
    _workbooks.append(wb)
    _created_workbooks.append({
        "workbook_id": new_id,
        "name": req.name,
        "owner": req.owner,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    resp = {"ok": True, "workbook_id": new_id, "sheet_names": [s["name"] for s in sheets]}
    _log_call("/sheet/create_workbook", req.model_dump(), resp)
    return resp


@app.post("/sheet/add_sheet")
def add_sheet(req: AddSheetRequest) -> dict[str, Any]:
    wb = _find_workbook(req.workbook_id)
    if wb is None:
        resp = {"error": f"Workbook {req.workbook_id} not found"}
        _log_call("/sheet/add_sheet", req.model_dump(), resp)
        return resp
    if _find_sheet(wb, req.name) is not None:
        resp = {"error": f"Sheet {req.name!r} already exists in {req.workbook_id}"}
        _log_call("/sheet/add_sheet", req.model_dump(), resp)
        return resp
    wb.setdefault("sheets", []).append({"name": req.name, "cells": {}})
    _added_sheets.append({
        "workbook_id": req.workbook_id,
        "sheet": req.name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    wb["last_updated"] = frozen_now().strftime("%Y-%m-%d")
    resp = {"ok": True, "sheet_name": req.name}
    _log_call("/sheet/add_sheet", req.model_dump(), resp)
    return resp


@app.post("/sheet/rename_sheet")
def rename_sheet(req: RenameSheetRequest) -> dict[str, Any]:
    wb = _find_workbook(req.workbook_id)
    if wb is None:
        resp = {"error": f"Workbook {req.workbook_id} not found"}
        _log_call("/sheet/rename_sheet", req.model_dump(), resp)
        return resp
    sheet = _find_sheet(wb, req.old_name)
    if sheet is None:
        resp = {"error": f"Sheet {req.old_name!r} not found in {req.workbook_id}"}
        _log_call("/sheet/rename_sheet", req.model_dump(), resp)
        return resp
    if _find_sheet(wb, req.new_name) is not None:
        resp = {"error": f"Sheet {req.new_name!r} already exists in {req.workbook_id}"}
        _log_call("/sheet/rename_sheet", req.model_dump(), resp)
        return resp
    sheet["name"] = req.new_name
    _renamed.append({
        "workbook_id": req.workbook_id,
        "old_name": req.old_name,
        "new_name": req.new_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    wb["last_updated"] = frozen_now().strftime("%Y-%m-%d")
    resp = {"ok": True}
    _log_call("/sheet/rename_sheet", req.model_dump(), resp)
    return resp


@app.post("/sheet/compute")
def compute(req: ComputeRequest) -> dict[str, Any]:
    op = req.op.upper()
    if op not in {"SUM", "AVG", "COUNT", "MAX", "MIN"}:
        resp = {"error": f"Unsupported op {req.op!r}; choose from SUM/AVG/COUNT/MAX/MIN"}
        _log_call("/sheet/compute", req.model_dump(), resp)
        return resp
    wb = _find_workbook(req.workbook_id)
    if wb is None:
        resp = {"error": f"Workbook {req.workbook_id} not found"}
        _log_call("/sheet/compute", req.model_dump(), resp)
        return resp
    sheet = _find_sheet(wb, req.sheet)
    if sheet is None:
        resp = {"error": f"Sheet {req.sheet!r} not found in {req.workbook_id}"}
        _log_call("/sheet/compute", req.model_dump(), resp)
        return resp
    parsed = _parse_range(req.range)
    if parsed is None:
        resp = {"error": f"Invalid range {req.range!r}"}
        _log_call("/sheet/compute", req.model_dump(), resp)
        return resp
    c_lo, r_lo, c_hi, r_hi = parsed
    cells_dict = sheet.get("cells", {}) or {}
    numeric: list[float] = []
    for row in range(r_lo, r_hi + 1):
        for col in range(c_lo, c_hi + 1):
            key = f"{_idx_to_col(col)}{row}"
            v = cells_dict.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                numeric.append(float(v))
    warning = None
    if op == "COUNT":
        result: Any = len(numeric)
    elif not numeric:
        result = None
        warning = "empty numeric range"
    elif op == "SUM":
        result = sum(numeric)
    elif op == "AVG":
        result = sum(numeric) / len(numeric)
    elif op == "MAX":
        result = max(numeric)
    elif op == "MIN":
        result = min(numeric)
    resp: dict[str, Any] = {"result": result, "count": len(numeric), "op": op, "range": req.range}
    if warning:
        resp["warning"] = warning
    _log_call("/sheet/compute", req.model_dump(), resp)
    return resp


@app.post("/sheet/delete_workbook")
def delete_workbook(req: DeleteWorkbookRequest) -> dict[str, Any]:
    wb = _find_workbook(req.workbook_id)
    if wb is None:
        resp = {"error": f"Workbook {req.workbook_id} not found"}
        _log_call("/sheet/delete_workbook", req.model_dump(), resp)
        return resp
    _workbooks.remove(wb)
    _deleted.append({
        "kind": "workbook",
        "workbook_id": req.workbook_id,
        "name": wb.get("name"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    resp = {"ok": True}
    _log_call("/sheet/delete_workbook", req.model_dump(), resp)
    return resp


@app.post("/sheet/delete_sheet")
def delete_sheet(req: DeleteSheetRequest) -> dict[str, Any]:
    wb = _find_workbook(req.workbook_id)
    if wb is None:
        resp = {"error": f"Workbook {req.workbook_id} not found"}
        _log_call("/sheet/delete_sheet", req.model_dump(), resp)
        return resp
    sheet = _find_sheet(wb, req.sheet)
    if sheet is None:
        resp = {"error": f"Sheet {req.sheet!r} not found in {req.workbook_id}"}
        _log_call("/sheet/delete_sheet", req.model_dump(), resp)
        return resp
    wb["sheets"].remove(sheet)
    _deleted.append({
        "kind": "sheet",
        "workbook_id": req.workbook_id,
        "sheet": req.sheet,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    resp = {"ok": True}
    _log_call("/sheet/delete_sheet", req.model_dump(), resp)
    return resp


@app.get("/sheet/audit")
def get_audit() -> dict[str, Any]:
    return {
        "calls": _audit_log,
        "updates": _updates,
        "inserts": _inserts,
        "added_sheets": _added_sheets,
        "renamed": _renamed,
        "created_workbooks": _created_workbooks,
        "deleted": _deleted,
    }


@app.post("/sheet/reset")
def reset_state() -> dict[str, str]:
    global _audit_log, _updates, _inserts, _added_sheets, _renamed, _created_workbooks, _deleted
    _audit_log = []
    _updates = []
    _inserts = []
    _added_sheets = []
    _renamed = []
    _created_workbooks = []
    _deleted = []
    _load_fixtures()
    return {"status": "reset"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "9133")))
