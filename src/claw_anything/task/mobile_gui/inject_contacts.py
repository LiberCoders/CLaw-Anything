"""Android ContactsProvider direct injection helpers.

Adapted from dailybench-gui/inject/inject_contacts.py.
Writes contacts directly into the system ContactsProvider via
`adb shell content insert`, bypassing Google Contacts onboarding.

Only the core injection functions are kept; CLI, VCF parser, and bundled
DEFAULT_CONTACTS are intentionally omitted.

Set _ADB_BIN before calling any function to use a non-default adb binary.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time

_ADB_BIN = "adb"

CONTACTS_PROVIDER_PKG = "com.android.providers.contacts"
RAW_CONTACTS_URI = "content://com.android.contacts/raw_contacts"
DATA_URI = "content://com.android.contacts/data"
CONTACTS_URI = "content://com.android.contacts/contacts"

MIME_NAME  = "vnd.android.cursor.item/name"
MIME_PHONE = "vnd.android.cursor.item/phone_v2"
MIME_EMAIL = "vnd.android.cursor.item/email_v2"
MIME_ORG   = "vnd.android.cursor.item/organization"
MIME_NOTE  = "vnd.android.cursor.item/note"
MIME_ADDR  = "vnd.android.cursor.item/postal-address_v2"


def _adb(device: str | None, *args: str) -> subprocess.CompletedProcess:
    cmd = [_ADB_BIN] + (["-s", device] if device else []) + list(args)
    return subprocess.run(cmd, capture_output=True, text=True)


def _adb_shell(device: str | None, *args: str) -> subprocess.CompletedProcess:
    return _adb(device, "shell", *args)


def clear_contacts(device: str | None = None) -> None:
    r = _adb_shell(device, "pm", "clear", CONTACTS_PROVIDER_PKG)
    if r.returncode == 0:
        print("  [OK] cleared all contacts")
    else:
        print(f"  [WARN] pm clear: {r.stderr.strip()}", file=sys.stderr)
    time.sleep(1)


def _max_raw_id(device: str | None) -> int:
    r = _adb_shell(device, "content", "query", "--uri", RAW_CONTACTS_URI, "--projection", "_id")
    if r.returncode != 0:
        return 0
    ids = [int(m.group(1)) for m in re.finditer(r"_id=(\d+)", r.stdout)]
    return max(ids) if ids else 0


def _insert_raw(device: str | None) -> int:
    before = _max_raw_id(device)
    r = _adb_shell(
        device, "content", "insert",
        "--uri", RAW_CONTACTS_URI,
        "--bind", "account_type:s:null",
        "--bind", "account_name:s:null",
    )
    if r.returncode != 0:
        raise RuntimeError(f"insert raw_contacts failed: {r.stderr}")
    after = _max_raw_id(device)
    if after <= before:
        raise RuntimeError("raw_contacts insert produced no new row")
    return after


def _insert_data(device: str | None, raw_id: int, mimetype: str, **fields) -> None:
    bind = [
        "--bind", f"raw_contact_id:i:{raw_id}",
        "--bind", f"mimetype:s:{mimetype}",
    ]
    for k, v in fields.items():
        if v is None or v == "":
            continue
        v_safe = str(v).replace("'", "'\\''")
        bind += ["--bind", f"{k}:s:'{v_safe}'"]
    r = _adb_shell(device, "content", "insert", "--uri", DATA_URI, *bind)
    if r.returncode != 0:
        print(f"  [WARN] insert {mimetype} failed: {r.stderr.strip()}", file=sys.stderr)


def _insert_contact(device: str | None, c: dict) -> int:
    rid = _insert_raw(device)
    parts = c["name"].split()
    given = parts[0] if parts else ""
    family = parts[-1] if len(parts) > 1 else ""
    _insert_data(device, rid, MIME_NAME, data1=c["name"], data2=given, data3=family)
    if c.get("phone"):
        _insert_data(device, rid, MIME_PHONE, data1=c["phone"], data2="2")
    if c.get("email"):
        _insert_data(device, rid, MIME_EMAIL, data1=c["email"], data2="1")
    if c.get("org") or c.get("title"):
        _insert_data(device, rid, MIME_ORG, data1=c.get("org", ""), data4=c.get("title", ""))
    if c.get("note"):
        _insert_data(device, rid, MIME_NOTE, data1=c["note"])
    if c.get("address"):
        _insert_data(device, rid, MIME_ADDR, data1=c["address"])
    return rid


def inject_contacts(contacts: list[dict], device: str | None, clear: bool) -> bool:
    if clear:
        clear_contacts(device)
    ok_count = 0
    for c in contacts:
        if not c.get("name"):
            print(f"  [SKIP] missing name: {c}")
            continue
        try:
            rid = _insert_contact(device, c)
            print(f"  [OK] {c['name']:<16s} phone={c.get('phone', '')!s:<22s} raw_id={rid}")
            ok_count += 1
        except Exception as e:
            print(f"  [FAIL] {c.get('name', '?')}: {e}", file=sys.stderr)
    r = _adb_shell(device, "content", "query", "--uri", CONTACTS_URI, "--projection", "display_name")
    count = len(re.findall(r"display_name=", r.stdout))
    print(f"\nContacts provider now holds {count} display_name rows ({ok_count} inserted this run)")
    return ok_count > 0
