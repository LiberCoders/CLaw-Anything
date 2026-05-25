"""Schema enforcer: backfill missing fields in LLM-generated fixture records.

LLM occasionally drops fields declared in ``template/fixture_schemas.yaml``,
which then crashes mock services that index those fields with ``[]``. This
module backfills missing top-level fields with type-derived defaults, logs a
warning per record, and escalates to error level if a field is systematically
missing across a service (indicating prompt drift).

Top-level only — nested ``list[object]`` sub-records are not backfilled.
"""

from __future__ import annotations

import copy
import logging
from collections import Counter
from typing import Any

log = logging.getLogger(__name__)


_UNKNOWN_TYPE = object()  # sentinel: type string did not match any rule


def _default_for_type(type_str: str) -> Any:
    """Map a schema ``type`` string to a sensible Python default.

    Recognizes the type strings used in ``fixture_schemas.yaml`` and
    ``fixture_schemas_gui.yaml`` (including unions like ``string|null`` and
    ``boolean|string|integer``).

    Returns the ``_UNKNOWN_TYPE`` sentinel when the type string is empty or
    unrecognized; callers can then choose to fall back to a schema example.
    Nullable types (containing ``null``) return ``None`` intentionally — do
    not treat that as "unknown".
    """
    s = (type_str or "").strip().lower()
    if not s:
        return _UNKNOWN_TYPE
    if "null" in s:
        return None
    if s.startswith("list"):
        return []
    if "object" in s or "dict" in s:
        return {}
    if "string" in s:
        return ""
    if "integer" in s or s == "int":
        return 0
    if "number" in s or "float" in s:
        return 0.0
    if "boolean" in s or s == "bool":
        return False
    return _UNKNOWN_TYPE


def field_defaults(schema_def: dict) -> dict[str, Any]:
    """Build a {field_name: default_value} map from one service's schema.

    Priority: explicit ``default`` key > type-derived default > example value.
    """
    out: dict[str, Any] = {}
    for fname, fdef in (schema_def.get("fields") or {}).items():
        if not isinstance(fdef, dict):
            out[fname] = None
            continue
        if "default" in fdef:
            out[fname] = copy.deepcopy(fdef["default"])
            continue
        default = _default_for_type(fdef.get("type", ""))
        if default is _UNKNOWN_TYPE:
            # Type string was empty or unrecognized — fall back to the
            # schema's example if one is provided, else None.
            if "example" in fdef and fdef["example"] is not None:
                default = copy.deepcopy(fdef["example"])
            else:
                default = None
        out[fname] = default
    return out


def enforce_record(
    service: str,
    record: dict,
    schemas: dict,
) -> tuple[dict, list[str]]:
    """Backfill missing top-level fields in a single record.

    Returns ``(filled_copy, missing_field_names)``. Existing keys are never
    overwritten — even ``None`` values are kept as-is so legitimate LLM-emitted
    nulls survive.
    """
    schema_def = schemas.get(service)
    if not isinstance(schema_def, dict):
        return dict(record), []

    defaults = field_defaults(schema_def)
    filled = dict(record)
    missing: list[str] = []
    for fname, default in defaults.items():
        if fname not in filled:
            filled[fname] = copy.deepcopy(default)
            missing.append(fname)
    return filled, missing


def enforce_records(
    service: str,
    records: list[dict],
    schemas: dict,
    *,
    source: str,
) -> list[dict]:
    """Backfill missing fields across a batch of records for one service.

    Logs a per-record warning when fields are filled, plus an error-level
    summary if any field was missing in ≥50% of the batch (prompt drift).
    """
    if not records:
        return records

    schema_def = schemas.get(service)
    if not isinstance(schema_def, dict):
        return records

    id_field = schema_def.get("id_field", "id")
    filled_records: list[dict] = []
    missing_counter: Counter = Counter()

    for record in records:
        filled, missing = enforce_record(service, record, schemas)
        filled_records.append(filled)
        if missing:
            rid = record.get(id_field, "<no-id>")
            log.warning(
                "schema_enforcer[%s/%s]: record %s missing %s; backfilled defaults",
                source, service, rid, missing,
            )
            missing_counter.update(missing)

    total = len(records)
    threshold = max(1, (total + 1) // 2)  # ceil(total/2), ≥50%
    for field, n_missing in missing_counter.items():
        if n_missing >= threshold:
            log.error(
                "schema_enforcer[%s/%s]: LLM systematically missed field %r "
                "in %d/%d records (prompt drift suspected)",
                source, service, field, n_missing, total,
            )

    return filled_records


def enforce_records_by_service(
    records_by_service: dict[str, list[dict]],
    schemas: dict,
    *,
    source: str,
) -> dict[str, list[dict]]:
    """Convenience wrapper: enforce every service in a {service: [records]} map."""
    return {
        svc: enforce_records(svc, recs, schemas, source=source)
        for svc, recs in records_by_service.items()
    }
