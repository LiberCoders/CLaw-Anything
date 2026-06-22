"""Regression tests for sheet-service fixture normalization.

Fixtures in the wild are row-oriented in several incompatible shapes (the LLM
generator emits ``sheet_name``/``headers``/``rows``; hand-authored benchmark
tasks use ``name``/``columns``/``rows``), and NONE populate the cell-addressed
``name``/``cells`` model that the set_cell/get_range/compute endpoints read.
Before normalization every named-sheet read/write failed with
"Sheet '<name>' not found" / empty ranges (the WB-2 "Active FOIA Requests" bug).
These tests pin the load-time normalization that maps any of those shapes onto
``name`` + ``cells``.
"""

from __future__ import annotations

from mock_services.sheet.server import _normalize_sheet


def test_generator_schema_sheet_name_headers_rows():
    """LLM generator shape: sheet_name + headers + list-of-dict rows."""
    s = _normalize_sheet({
        "sheet_name": "Active FOIA Requests",
        "headers": ["Request ID", "Agency"],
        "rows": [
            {"Request ID": "#2024-1187", "Agency": "Procurement Division"},
            {"Request ID": "#2024-1201", "Agency": "City Clerk's Office"},
        ],
    })
    assert s["name"] == "Active FOIA Requests"
    # Header row at row 1, data from row 2.
    assert s["cells"]["A1"] == "Request ID"
    assert s["cells"]["B1"] == "Agency"
    assert s["cells"]["A2"] == "#2024-1187"
    assert s["cells"]["B3"] == "City Clerk's Office"


def test_benchmark_schema_name_columns_rows():
    """Hand-authored benchmark shape: name + columns + list-of-dict rows."""
    s = _normalize_sheet({
        "name": "Supplier Comparison Matrix",
        "columns": ["SKU", "Unit Cost"],
        "rows": [{"SKU": "SKU-101", "Unit Cost": 8.5}],
    })
    assert s["name"] == "Supplier Comparison Matrix"
    assert s["cells"]["A1"] == "SKU"
    assert s["cells"]["B1"] == "Unit Cost"
    assert s["cells"]["A2"] == "SKU-101"
    assert s["cells"]["B2"] == 8.5


def test_headers_derived_from_row_keys_when_absent():
    """No headers/columns list → derive column order from row dict keys."""
    s = _normalize_sheet({
        "sheet_name": "X",
        "rows": [{"Foo": 1, "Bar": 2}],
    })
    assert s["cells"]["A1"] == "Foo"
    assert s["cells"]["B1"] == "Bar"
    assert s["cells"]["A2"] == 1
    assert s["cells"]["B2"] == 2


def test_list_of_list_rows():
    """Rows may be positional lists rather than dicts."""
    s = _normalize_sheet({
        "name": "Y",
        "headers": ["A", "B"],
        "rows": [["x", "y"]],
    })
    assert s["cells"]["A2"] == "x"
    assert s["cells"]["B2"] == "y"


def test_existing_cells_respected():
    """A sheet already in cell-addressed form is left untouched."""
    original = {"name": "Z", "cells": {"A1": "kept"}}
    s = _normalize_sheet(dict(original))
    assert s["cells"] == {"A1": "kept"}


def test_sheet_name_aliased_to_name_even_when_empty_rows():
    """sheet_name must surface as name so _find_sheet matches it (was None)."""
    s = _normalize_sheet({"sheet_name": "Empty Sheet", "headers": [], "rows": []})
    assert s["name"] == "Empty Sheet"
    assert s["cells"] == {}
