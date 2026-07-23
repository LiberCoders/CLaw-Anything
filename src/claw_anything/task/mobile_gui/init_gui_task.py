"""GUI task initialization: inject fixture data into Android emulator via adb.

Reads the task YAML (default: task.yaml), iterates inject steps, and
dispatches to per-type handlers.  Supported inject types:
  email             — Gmail clone state.json push
  calendar          — Fossify Calendar SQLite insert
  fossify_calendar  — alias for calendar
  contacts          — Android ContactsProvider direct insert
  dialer            — Android call log (content://call_log/calls) insert
  fossify_messages  — Fossify Messages SMS threads (content://sms) insert
  markor            — Markor markdown/text file push
  loop_habits  — Loop Habit Tracker SQLite insert
  my_expenses  — My Expenses SQLite insert
  opentracks   — OpenTracks SQLite insert
  clock        — Google Clock alarms via SQLite DB push
  fossify_notes — Fossify Notes text/checklist notes via SQLite DB push
  testmall     — TestMall shadow app state.json push
  mattermost   — Mattermost shadow app state.json push

Public API:
    init_gui_task(task_dir, device=None, adb_path=None, yaml_name="task.yaml") -> bool
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid as uuid_mod
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from . import inject_calendar as _cal
from . import inject_contacts as _con

# ── Remote app constants ──────────────────────────────────────────────────────

GMAIL_STATE_PATH = "/sdcard/Android/data/com.gmailclone/files/state.json"
GMAIL_PACKAGE    = "com.gmailclone"
GMAIL_ACTIVITY   = f"{GMAIL_PACKAGE}/.MainActivity"

DIALER_PACKAGE   = "com.google.android.dialer"
DIALER_ACTIVITY  = f"{DIALER_PACKAGE}/com.android.dialer.main.impl.MainActivity"
CALL_LOG_URI     = "content://call_log/calls"

FOSSIFY_MESSAGES_PACKAGE  = "org.fossify.messages"
FOSSIFY_MESSAGES_ACTIVITY = f"{FOSSIFY_MESSAGES_PACKAGE}/.activities.MainActivity"
SMS_URI                   = "content://sms"
# Candidate on-device locations of the TelephonyProvider SMS database, probed
# in order at runtime by ``_resolve_sms_db``. The credential-encrypted
# ``/data/data`` path is correct on the KVM MobileWorld image; redroid (and
# stock Android 13) keeps the provider in device-encrypted storage because it
# must run before first user unlock, so ``/data/data/<provider>`` does not even
# resolve there and the DB lives under ``/data/user_de/0/``.
SMS_DB_CANDIDATES         = (
    "/data/data/com.android.providers.telephony/databases/mmssms.db",
    "/data/user_de/0/com.android.providers.telephony/databases/mmssms.db",
)
SMS_DB_REMOTE             = SMS_DB_CANDIDATES[0]  # legacy default / fallback

MARKOR_PACKAGE   = "net.gsantner.markor"
MARKOR_ACTIVITY  = f"{MARKOR_PACKAGE}/.activity.MainActivity"
MARKOR_BASE_PATH = "/sdcard/Documents/markor"

LOOP_HABITS_PACKAGE   = "org.isoron.uhabits"
LOOP_HABITS_ACTIVITY  = f"{LOOP_HABITS_PACKAGE}/.activities.habits.list.ListHabitsActivity"
LOOP_HABITS_DB_REMOTE = f"/data/data/{LOOP_HABITS_PACKAGE}/databases/uhabits.db"

MY_EXPENSES_PACKAGE   = "org.totschnig.myexpenses"
MY_EXPENSES_ACTIVITY  = f"{MY_EXPENSES_PACKAGE}/.activity.MyExpenses"
MY_EXPENSES_DB_REMOTE = f"/data/data/{MY_EXPENSES_PACKAGE}/databases/data"
MY_EXPENSES_PREFS_REMOTE = (
    f"/data/data/{MY_EXPENSES_PACKAGE}/shared_prefs/"
    f"{MY_EXPENSES_PACKAGE}_preferences.xml"
)

CLOCK_PACKAGE   = "com.google.android.deskclock"
CLOCK_ACTIVITY  = f"{CLOCK_PACKAGE}/com.android.deskclock.DeskClock"
CLOCK_DB_REMOTE = f"/data/user_de/0/{CLOCK_PACKAGE}/databases/alarms.db"

FOSSIFY_NOTES_PACKAGE   = "org.fossify.notes"
FOSSIFY_NOTES_ACTIVITY  = f"{FOSSIFY_NOTES_PACKAGE}/.activities.MainActivity"
FOSSIFY_NOTES_DB_REMOTE = f"/data/data/{FOSSIFY_NOTES_PACKAGE}/databases/notes.db"

# Module-level adb binary (set by init_gui_task via _resolve_adb_bin).
_ADB = "adb"


# ── ADB helpers ───────────────────────────────────────────────────────────────

def _resolve_adb_bin(adb_path: str | None) -> str:
    if adb_path and Path(adb_path).is_file():
        return adb_path
    found = shutil.which("adb")
    if found:
        return found
    # fallback: platform-tools bundled alongside the repo
    local = Path(__file__).resolve().parents[4] / "platform-tools" / "adb"
    if local.is_file():
        return str(local)
    return "adb"


def _adb_prefix(device: str | None) -> list[str]:
    return [_ADB, "-s", device] if device else [_ADB]


def _run(cmd: list[str], root_required: bool = False, device: str | None = None) -> tuple[bool, str]:
    if root_required:
        check = subprocess.run(_adb_prefix(device) + ["shell", "whoami"], capture_output=True, text=True)
        if check.returncode == 0 and check.stdout.strip() != "root":
            subprocess.run(_adb_prefix(device) + ["root"], capture_output=True, text=True)
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0, (result.stdout + result.stderr).strip()


def adb_push(local: str, remote: str, device: str | None = None, root_required: bool = False) -> bool:
    ok, msg = _run(_adb_prefix(device) + ["push", local, remote],
                   root_required=root_required, device=device)
    if ok:
        print(f"  [OK] push {Path(local).name} → {remote}")
    else:
        print(f"  [FAIL] push {Path(local).name}: {msg}", file=sys.stderr)
    return ok


def adb_shell(cmd: str, device: str | None = None, root_required: bool = False) -> tuple[bool, str]:
    return _run(_adb_prefix(device) + ["shell", cmd], root_required=root_required, device=device)


def _adb_file_exists(remote: str, device: str | None = None, root_required: bool = False) -> tuple[bool, str]:
    return adb_shell(f"test -f {remote} && echo __EXISTS__", device=device, root_required=root_required)


def _package_installed(package: str, device: str | None = None) -> tuple[bool, str]:
    ok, out = adb_shell(f"pm path {package}", device=device)
    return ok and "package:" in out, out


def _start_activity(activity: str, device: str | None = None) -> tuple[bool, str]:
    return adb_shell(f"am start -W -n {activity}", device=device)


def _resolve_launcher_activity(package: str, device: str | None = None) -> str | None:
    ok, out = adb_shell(f"cmd package resolve-activity --brief {package}", device=device)
    if not ok:
        return None
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    for line in reversed(lines):
        if "/" in line and line.startswith(package):
            return line
    return None


def _start_package_launcher(
    package: str,
    *,
    device: str | None = None,
    fallback_activity: str | None = None,
) -> tuple[bool, str]:
    launcher = _resolve_launcher_activity(package, device=device)
    if launcher:
        return _start_activity(launcher, device=device)
    if fallback_activity:
        ok, out = _start_activity(fallback_activity, device=device)
        if ok:
            return ok, out
    return adb_shell(f"monkey -p {package} -c android.intent.category.LAUNCHER 1", device=device)


def _capture_screenshot(task_id: str, screenshots_dir: Path, name: str, device: str | None = None) -> None:
    shot_path = screenshots_dir / name
    result = subprocess.run(_adb_prefix(device) + ["exec-out", "screencap", "-p"], capture_output=True)
    if result.returncode == 0 and result.stdout:
        shot_path.write_bytes(result.stdout)
        print(f"[{task_id}] Screenshot saved → {shot_path}")
    else:
        print(f"[{task_id}] Screenshot failed: {name}", file=sys.stderr)


def _sanitize_screenshot_dir_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", (name or "").strip())
    return cleaned or "unknown"


def _ui_dump_root(device: str | None = None) -> tuple[ET.Element | None, str]:
    remote_xml = "/sdcard/ui_dump.xml"
    ok, out = adb_shell(f"uiautomator dump {remote_xml}", device=device)
    if not ok:
        return None, out
    with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tmp:
        tmp_xml = tmp.name
    try:
        ok, pull_msg = _run(_adb_prefix(device) + ["pull", remote_xml, tmp_xml], device=device)
        if not ok:
            return None, pull_msg
        return ET.parse(tmp_xml).getroot(), ""
    except Exception as exc:
        return None, str(exc)
    finally:
        os.unlink(tmp_xml)


def _bounds_center(bounds: str) -> tuple[int, int] | None:
    nums = list(map(int, re.findall(r"\d+", bounds)))
    if len(nums) != 4:
        return None
    return (nums[0] + nums[2]) // 2, (nums[1] + nums[3]) // 2


def _tap_point(x: int, y: int, device: str | None = None) -> bool:
    ok, _ = adb_shell(f"input tap {x} {y}", device=device)
    return ok


def _find_ui_node(
    root: ET.Element,
    *,
    text: str | None = None,
    text_contains: str | None = None,
    desc: str | None = None,
    desc_contains: str | None = None,
) -> ET.Element | None:
    for node in root.iter():
        node_text = (node.get("text") or "").strip()
        node_desc = (node.get("content-desc") or "").strip()
        if text is not None and node_text == text:
            return node
        if text_contains is not None and text_contains in node_text:
            return node
        if desc is not None and node_desc == desc:
            return node
        if desc_contains is not None and desc_contains in node_desc:
            return node
    return None


def _tap_ui_node(node: ET.Element, device: str | None = None) -> bool:
    center = _bounds_center(node.get("bounds", ""))
    if center is None:
        return False
    return _tap_point(center[0], center[1], device=device)


def _tap_ui_selector(
    *,
    device: str | None = None,
    text: str | None = None,
    text_contains: str | None = None,
    desc: str | None = None,
    desc_contains: str | None = None,
    timeout_s: float = 8.0,
    poll_s: float = 0.5,
) -> tuple[bool, str]:
    deadline = time.time() + timeout_s
    last_msg = "selector not found"
    while time.time() < deadline:
        root, msg = _ui_dump_root(device=device)
        if root is not None:
            node = _find_ui_node(
                root,
                text=text,
                text_contains=text_contains,
                desc=desc,
                desc_contains=desc_contains,
            )
            if node is not None:
                if _tap_ui_node(node, device=device):
                    return True, "tapped"
                return False, "found node but tap failed"
            last_msg = "selector not found"
        else:
            last_msg = msg
        time.sleep(poll_s)
    return False, last_msg


def _wait_for_ui_selector(
    *,
    device: str | None = None,
    text: str | None = None,
    text_contains: str | None = None,
    desc: str | None = None,
    desc_contains: str | None = None,
    timeout_s: float = 8.0,
    poll_s: float = 0.5,
) -> tuple[bool, str]:
    deadline = time.time() + timeout_s
    last_msg = "selector not found"
    while time.time() < deadline:
        root, msg = _ui_dump_root(device=device)
        if root is not None:
            node = _find_ui_node(
                root,
                text=text,
                text_contains=text_contains,
                desc=desc,
                desc_contains=desc_contains,
            )
            if node is not None:
                return True, "found"
            last_msg = "selector not found"
        else:
            last_msg = msg
        time.sleep(poll_s)
    return False, last_msg


# ── Format converters ─────────────────────────────────────────────────────────

def _iso_to_local(iso: str) -> str:
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(iso[:19] + (iso[19:] or "+00:00"), fmt)
            if dt.tzinfo is not None:
                dt = dt.astimezone(tz=None).replace(tzinfo=None)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return iso[:19].replace("T", " ")


def _iso_to_unix_ms(iso: str) -> int:
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(iso[:19] + (iso[19:] or "+00:00"), fmt)
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse timestamp: {iso}")


def _tz_offset_ms(iso: str) -> int:
    m = re.search(r'([+-])(\d{2}):(\d{2})$', iso)
    if not m:
        return 0
    sign = 1 if m.group(1) == '+' else -1
    return sign * (int(m.group(2)) * 3600 + int(m.group(3)) * 60) * 1000


def _sender_logo(email_addr: str) -> str:
    domain = email_addr.split("@")[-1].split(".")[0].lower() if "@" in email_addr else ""
    return domain[:8]


def _convert_inbox_to_state(inbox: list[dict], user_email: str) -> dict:
    mails = []
    for msg in inbox:
        sender_addr = msg.get("from", "")
        status = "read" if msg.get("is_read", True) else "unread"
        headers = {
            "subject": msg.get("subject", ""),
            "date": msg.get("date", ""),
            "from": sender_addr,
            "to": msg.get("to", user_email),
            "sender": sender_addr.split("@")[0] if "@" in sender_addr else sender_addr,
            "senderLogo": _sender_logo(sender_addr),
        }
        mail = {
            "headers": headers,
            "body": msg.get("body", ""),
            "status": status,
            "attachments": [],
            "category": "sent" if sender_addr == user_email else "inbox",
        }
        mails.append(mail)
    return {"username": user_email, "mails": mails}


def _convert_contacts(raw: list[dict]) -> list[dict]:
    return [
        {
            "name":    c.get("name", ""),
            "phone":   c.get("phone", ""),
            "email":   c.get("email", ""),
            "org":     c.get("department", c.get("org", "")),
            "title":   c.get("title", ""),
            "note":    c.get("note", ""),
            "address": c.get("location", c.get("address", "")),
        }
        for c in raw
    ]


# ── Inject handlers ───────────────────────────────────────────────────────────

def inject_email(step: dict, task_dir: Path, device: str | None) -> bool:
    fixture_path = task_dir / step.get("fixture", "fixtures/gmail/inbox.json")
    user_email   = step.get("user_email", "user@example.com")

    if not fixture_path.exists():
        print(f"  [FAIL] fixture not found: {fixture_path}", file=sys.stderr)
        return False

    with open(fixture_path, encoding="utf-8") as f:
        inbox = json.load(f)
    if not isinstance(inbox, list):
        print("  [FAIL] inbox.json must be a list", file=sys.stderr)
        return False

    state = _convert_inbox_to_state(inbox, user_email)
    adb_shell(f"rm -f {GMAIL_STATE_PATH}", device=device)
    print("  [OK] cleared existing email state")
    adb_shell(f"am force-stop {GMAIL_PACKAGE}", device=device)
    adb_shell(f"mkdir -p $(dirname {GMAIL_STATE_PATH})", device=device)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tmp:
        json.dump(state, tmp, ensure_ascii=False, indent=2)
        tmp_path = tmp.name
    try:
        ok = adb_push(tmp_path, GMAIL_STATE_PATH, device=device)
    finally:
        os.unlink(tmp_path)

    if not ok:
        return False
    adb_shell(f"am start -n {GMAIL_ACTIVITY}", device=device)
    print(f"  [OK] Mail app restarted ({len(state['mails'])} mails injected)")
    return True


def inject_calendar(step: dict, task_dir: Path, device: str | None) -> bool:
    fixture_path = task_dir / step.get("fixture", "fixtures/calendar/events.json")
    if not fixture_path.exists():
        print(f"  [FAIL] fixture not found: {fixture_path}", file=sys.stderr)
        return False

    with open(fixture_path, encoding="utf-8") as f:
        events = json.load(f)
    if not isinstance(events, list):
        print("  [FAIL] events.json must be a list", file=sys.stderr)
        return False

    if not step.get("no_clear", False):
        _cal.clear_calendar_events(device=device)

    ok = True
    for evt in events:
        start       = _iso_to_local(evt.get("start_time", ""))
        end         = _iso_to_local(evt.get("end_time", ""))
        title       = evt.get("title", "")
        location    = evt.get("location", "")
        description = evt.get("description", "")
        attendees_str = ", ".join(evt.get("attendees", []))
        if attendees_str:
            description = f"{description}\nAttendees: {attendees_str}".strip()
        ok &= _cal.insert_event(device, title, start, end, location, description)

    _cal.restart_calendar_app(device=device)
    print(f"  [OK] Calendar app restarted ({len(events)} events injected)")
    return ok


def _dismiss_contacts_onboarding(device: str | None) -> tuple[bool, str]:
    remote_xml = "/sdcard/ui_dump.xml"
    adb_shell(f"uiautomator dump {remote_xml}", device=device)
    with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tmp:
        tmp_xml = tmp.name
    ok, _ = _run(_adb_prefix(device) + ["pull", remote_xml, tmp_xml], device=device)
    if not ok:
        return False, "pull failed"
    try:
        import xml.etree.ElementTree as ET
        tree = ET.parse(tmp_xml)
        for node in tree.iter():
            if node.get("text", "").strip().lower() == "skip":
                bounds = node.get("bounds", "")
                nums = list(map(int, re.findall(r"\d+", bounds)))
                if len(nums) == 4:
                    cx, cy = (nums[0] + nums[2]) // 2, (nums[1] + nums[3]) // 2
                    adb_shell(f"input tap {cx} {cy}", device=device)
                    return True, f"tapped Skip at ({cx},{cy})"
        return False, "Skip button not found"
    finally:
        os.unlink(tmp_xml)


def inject_contacts(step: dict, task_dir: Path, device: str | None) -> bool:
    json_field = step.get("json") or step.get("fixture")  # support legacy "fixture" field for contacts
    if not json_field:
        print("  [FAIL] contacts step requires a 'fixture' field", file=sys.stderr)
        return False

    fixture_path = Path(json_field) if Path(json_field).is_absolute() else task_dir / json_field
    if not fixture_path.exists():
        print(f"  [FAIL] fixture not found: {fixture_path}", file=sys.stderr)
        return False

    with open(fixture_path, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, list):
        print("  [FAIL] contacts.json must be a list", file=sys.stderr)
        return False

    converted = _convert_contacts(raw)
    ok = _con.inject_contacts(converted, device=device, clear=True)
    if not ok:
        return False

    adb_shell("am start -a android.intent.action.MAIN -c android.intent.category.APP_CONTACTS",
              device=device)
    time.sleep(3)
    ok_skip, _ = _dismiss_contacts_onboarding(device)
    if ok_skip:
        print("  [OK] Contacts onboarding dismissed (tapped Skip)")
    return True


_CALL_TYPE_MAP = {
    "incoming": 1,
    "outgoing": 2,
    "missed":   3,
    "voicemail": 4,
    "rejected": 5,
    "blocked":  6,
}


def inject_dialer(step: dict, task_dir: Path, device: str | None) -> bool:
    fixture_path = task_dir / step.get("fixture", "fixtures/dialer/call_log.json")
    if not fixture_path.exists():
        print(f"  [FAIL] fixture not found: {fixture_path}", file=sys.stderr)
        return False

    with open(fixture_path, encoding="utf-8") as f:
        calls = json.load(f)
    if not isinstance(calls, list):
        print("  [FAIL] call_log.json must be a list", file=sys.stderr)
        return False

    if not step.get("no_clear", False):
        adb_shell(f"content delete --uri {CALL_LOG_URI}", device=device)
        print("  [OK] cleared existing call log")

    ok = True
    for call in calls:
        call_type = _CALL_TYPE_MAP.get(call.get("call_type", "incoming"), 1)
        ts_ms     = _iso_to_unix_ms(call.get("timestamp", ""))
        duration  = int(call.get("duration_seconds", 0))
        number    = call.get("phone_number", "")
        name      = call.get("contact_name") or ""
        new_flag  = 1 if call.get("call_type") == "missed" else 0

        def _sq(v: str) -> str:
            return "'" + v.replace("'", "'\\''") + "'"

        binds = (
            f"--bind number:s:{_sq(number)}"
            f" --bind type:i:{call_type}"
            f" --bind date:l:{ts_ms}"
            f" --bind duration:i:{duration}"
            f" --bind new:i:{new_flag}"
        )
        if name:
            binds += f" --bind name:s:{_sq(name)}"

        sub_id = call.get("sim_slot")
        if sub_id is not None:
            binds += f" --bind subscription_id:i:{int(sub_id)}"

        ok_call, msg = adb_shell(
            f"content insert --uri {CALL_LOG_URI} {binds}",
            device=device,
        )
        if ok_call:
            print(f"  [OK] {call.get('call_id', '?')} [{call.get('call_type')}] {number}")
        else:
            print(f"  [FAIL] {call.get('call_id', '?')}: {msg}", file=sys.stderr)
            ok = False

    adb_shell(f"am start -n {DIALER_ACTIVITY}", device=device)
    print(f"  [OK] Dialer restarted ({len(calls)} call(s) injected)")
    return ok


# Per-device cache of the resolved SMS DB path so the candidate probe runs once
# per inject, not once per SQL statement.
_SMS_DB_RESOLVED: dict[str | None, str] = {}


def _resolve_sms_db(device: str | None) -> str:
    """Return the on-device SMS DB path that sqlite3 can actually open.

    Probes ``SMS_DB_CANDIDATES`` in order (``.tables`` must succeed and expose
    the ``sms`` table) so the KVM image keeps using ``/data/data`` while redroid
    falls through to its device-encrypted ``/data/user_de/0`` location. Falls
    back to the legacy default if none probe cleanly, so the caller still
    surfaces a meaningful open error.
    """
    cached = _SMS_DB_RESOLVED.get(device)
    if cached:
        return cached
    for path in SMS_DB_CANDIDATES:
        ok, out = adb_shell(f"sqlite3 {path} '.tables'", device=device, root_required=True)
        if ok and "sms" in out:
            _SMS_DB_RESOLVED[device] = path
            return path
    return SMS_DB_REMOTE


def _sqlite3_exec(sql: str, device: str | None) -> tuple[bool, str]:
    """Run a single SQL statement via sqlite3 on the device's SMS database."""
    db = _resolve_sms_db(device)
    escaped = sql.replace('"', '\\"')
    return adb_shell(f'sqlite3 {db} "{escaped}"', device=device)


def _sqlite3_query(sql: str, device: str | None) -> str:
    """Run a SQL query and return stdout."""
    db = _resolve_sms_db(device)
    escaped = sql.replace('"', '\\"')
    _, out = adb_shell(f'sqlite3 {db} "{escaped}"', device=device)
    return out.strip()


def _normalize_fossify_message_item(msg: dict) -> dict:
    """Normalise a single message dict to {id, time, text, is_outgoing}."""
    sender = str(msg.get("sender") or msg.get("from") or "").strip().lower()
    return {
        "id":          str(msg.get("id") or msg.get("message_id") or ""),
        "time":        msg.get("time") or msg.get("timestamp") or msg.get("date") or "",
        "text":        (
            msg.get("text")
            or msg.get("body")
            or msg.get("message_text")
            or msg.get("content")
            or ""
        ),
        "is_outgoing": msg.get("is_outgoing", msg.get("is_sent", sender == "me")),
    }


def _thread_participants(thread: dict) -> list[str]:
    participants = thread.get("participants")
    if isinstance(participants, list):
        return [str(p) for p in participants if p]
    contact = thread.get("contact") or thread.get("contact_number") or thread.get("address")
    return [str(contact)] if contact else []


def _normalize_fossify_thread(thread: dict) -> dict:
    norm_msgs = [_normalize_fossify_message_item(m) for m in thread.get("messages", [])]
    unread = int(thread.get("unread_count", 0) or 0)
    if unread <= 0:
        unread = sum(
            1
            for msg in thread.get("messages", [])
            if not _normalize_fossify_message_item(msg).get("is_outgoing")
            and not msg.get("is_read", True)
        )
    return {
        "participants": _thread_participants(thread),
        "unread_count": unread,
        "messages":     norm_msgs,
    }


def _group_flat_fossify_messages(raw: list[dict]) -> list[dict]:
    thread_order: list[str] = []
    threads: dict[str, dict] = {}
    for msg in raw:
        tid = str(msg.get("thread_id", ""))
        if tid not in threads:
            thread_order.append(tid)
            threads[tid] = {
                "participants": [msg.get("contact_number") or msg.get("contact", "")],
                "unread_count": 0,
                "messages":     [],
            }
        is_outgoing = msg.get("is_sent", msg.get("is_outgoing", False))
        is_read     = msg.get("is_read", True)
        if not is_outgoing and not is_read:
            threads[tid]["unread_count"] += 1
        threads[tid]["messages"].append(_normalize_fossify_message_item(msg))
    return [threads[tid] for tid in thread_order]


def _normalize_fossify_messages(raw: list) -> list:
    """Convert various fixture formats to the threaded format expected by
    inject_fossify_messages.

    Fixtures may be all-threaded, all-flat, or a mixed list that combines both.
    Threaded rows carry a ``messages`` list; flat rows are individual SMS rows
    grouped by ``thread_id``.
    """
    if not raw:
        return raw

    result: list[dict] = []
    pending_flat: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if "messages" in item:
            if pending_flat:
                result.extend(_group_flat_fossify_messages(pending_flat))
                pending_flat = []
            result.append(_normalize_fossify_thread(item))
        else:
            pending_flat.append(item)
    if pending_flat:
        result.extend(_group_flat_fossify_messages(pending_flat))
    return result


def inject_fossify_messages(step: dict, task_dir: Path, device: str | None) -> bool:
    fixture_path = task_dir / step.get("fixture", "fixtures/fossify_messages/threads.json")
    if not fixture_path.exists():
        print(f"  [FAIL] fixture not found: {fixture_path}", file=sys.stderr)
        return False

    with open(fixture_path, encoding="utf-8") as f:
        threads = _normalize_fossify_messages(json.load(f))
    if not isinstance(threads, list):
        print("  [FAIL] threads.json must be a list", file=sys.stderr)
        return False

    adb_shell(f"am force-stop {FOSSIFY_MESSAGES_PACKAGE}", device=device)
    time.sleep(1)

    if not step.get("no_clear", False):
        _sqlite3_exec("DELETE FROM sms; DELETE FROM threads; DELETE FROM canonical_addresses;", device=device)
        print("  [OK] cleared existing SMS messages")

    def _sql_str(v: str) -> str:
        return "'" + v.replace("'", "''") + "'"

    ok = True
    total_msgs = 0
    for thread in threads:
        participants = thread.get("participants", [])
        thread_address = participants[0] if participants else ""
        unread_count = int(thread.get("unread_count", 0))
        messages = thread.get("messages", [])

        if not messages:
            continue

        # Insert canonical address and get its ID
        _sqlite3_exec(
            f"INSERT INTO canonical_addresses (address) VALUES ({_sql_str(thread_address)});",
            device=device,
        )
        addr_id = _sqlite3_query(
            "SELECT _id FROM canonical_addresses ORDER BY _id DESC LIMIT 1;",
            device=device,
        )
        if not addr_id:
            print(f"  [FAIL] could not insert canonical address for {thread_address}", file=sys.stderr)
            ok = False
            continue

        # Compute thread-level date (latest message timestamp)
        ts_list = [
            _iso_to_unix_ms(m["time"]) for m in messages if m.get("time")
        ]
        thread_date = max(ts_list) if ts_list else int(time.time() * 1000)
        thread_read = 0 if unread_count > 0 else 1

        _sqlite3_exec(
            f"INSERT INTO threads (date, message_count, recipient_ids, snippet, read) "
            f"VALUES ({thread_date}, 0, {_sql_str(addr_id)}, '', {thread_read});",
            device=device,
        )
        thread_db_id = _sqlite3_query(
            "SELECT _id FROM threads ORDER BY _id DESC LIMIT 1;",
            device=device,
        )
        if not thread_db_id:
            print(f"  [FAIL] could not create thread for {thread_address}", file=sys.stderr)
            ok = False
            continue

        # Mark the last `unread_count` incoming messages as unread
        incoming_indices = [i for i, m in enumerate(messages) if not m.get("is_outgoing", False)]
        unread_set = set(incoming_indices[-unread_count:]) if unread_count > 0 else set()

        for idx, msg in enumerate(messages):
            is_outgoing = msg.get("is_outgoing", False)
            msg_type = 2 if is_outgoing else 1
            read = 0 if idx in unread_set else 1
            seen = 0 if idx in unread_set else 1
            ts_ms = _iso_to_unix_ms(msg["time"]) if msg.get("time") else int(time.time() * 1000)
            text = msg.get("text", "")

            ok_msg, err = _sqlite3_exec(
                f"INSERT INTO sms (thread_id, address, date, type, body, read, seen) "
                f"VALUES ({thread_db_id}, {_sql_str(thread_address)}, {ts_ms}, "
                f"{msg_type}, {_sql_str(text)}, {read}, {seen});",
                device=device,
            )
            if ok_msg:
                total_msgs += 1
                print(f"  [OK] {msg.get('id', '?')} [{'out' if is_outgoing else 'in'}] {thread_address}")
            else:
                print(f"  [FAIL] {msg.get('id', '?')}: {err}", file=sys.stderr)
                ok = False

    adb_shell(f"am start -n {FOSSIFY_MESSAGES_ACTIVITY}", device=device)
    print(f"  [OK] Fossify Messages restarted ({total_msgs} message(s) across {len(threads)} thread(s))")
    return ok


def inject_markor(step: dict, task_dir: Path, device: str | None) -> bool:
    fixture_path = task_dir / step.get("fixture", "fixtures/markor/notes.json")
    base_path    = step.get("base_path", MARKOR_BASE_PATH)

    if not fixture_path.exists():
        print(f"  [FAIL] fixture not found: {fixture_path}", file=sys.stderr)
        return False

    with open(fixture_path, encoding="utf-8") as f:
        notes = json.load(f)
    if not isinstance(notes, list):
        print("  [FAIL] notes fixture must be a JSON list", file=sys.stderr)
        return False

    adb_shell(f"am force-stop {MARKOR_PACKAGE}", device=device)

    if not step.get("no_clear", False):
        folders = {n.get("folder", "") for n in notes}
        for folder in folders:
            remote_dir = f"{base_path}/{folder}" if folder else base_path
            adb_shell(f"rm -f {remote_dir}/*.md {remote_dir}/*.txt 2>/dev/null", device=device)
        print("  [OK] cleared existing .md/.txt files in target folders")

    ok = True
    for note in notes:
        filename  = note.get("filename", "note.md")
        folder    = note.get("folder", "")
        content   = note.get("content", "")
        remote_dir  = f"{base_path}/{folder}" if folder else base_path
        adb_shell(f"mkdir -p {remote_dir}", device=device)
        remote_path = f"{remote_dir}/{filename}"
        with tempfile.NamedTemporaryFile(mode="w", suffix=Path(filename).suffix or ".md",
                                        delete=False, encoding="utf-8") as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        try:
            ok &= adb_push(tmp_path, remote_path, device=device)
        finally:
            os.unlink(tmp_path)

    adb_shell(
        f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file://{base_path}",
        device=device,
    )
    adb_shell(f"am start -n {MARKOR_ACTIVITY}", device=device)
    time.sleep(2)
    print(f"  [OK] Markor restarted ({len(notes)} note(s) injected)")
    return ok


# ── Loop Habit Tracker ────────────────────────────────────────────────────────

_LOOP_DAY_BITS = {"Mon": 1, "Tue": 2, "Wed": 4, "Thu": 8, "Fri": 16, "Sat": 32, "Sun": 64}
_LOOP_ALL_DAYS = 127


def _hex_to_android_color(value, default: int = 3) -> int:
    """Convert a CSS hex color string to a signed 32-bit Android ARGB integer,
    or return the value as-is if it is already an integer."""
    if isinstance(value, int):
        return value
    s = str(value).strip().lstrip("#")
    if len(s) == 6:
        s = "FF" + s
    try:
        v = int(s, 16)
        return v - 0x100000000 if v >= 0x80000000 else v
    except ValueError:
        return default


def _normalize_loop_habits(raw: list) -> list:
    """Convert the history-based fixture format to the completions-based format
    expected by inject_loop_habits.

    New format fields that differ from the expected format:
      color        : CSS hex string  → signed 32-bit Android int
      reminder_time: "HH:MM" string  → reminder_hour + reminder_min integers
      history      : [{date, completed, notes}]  → completions (completed-only)
      target_count : int             → target_value float
    """
    if not raw:
        return raw
    first = raw[0]
    # Already correct if color is int and uses 'completions'
    if isinstance(first.get("color"), int) and "completions" in first:
        return raw

    result = []
    for habit in raw:
        norm = dict(habit)  # shallow copy

        # color: hex → Android int
        norm["color"] = _hex_to_android_color(habit.get("color", 3))

        # reminder_time: "HH:MM" → reminder_hour / reminder_min
        rt = habit.get("reminder_time")
        if rt and isinstance(rt, str) and ":" in rt:
            parts = rt.split(":")
            norm["reminder_hour"] = int(parts[0])
            norm["reminder_min"]  = int(parts[1]) if len(parts) > 1 else 0
            norm.pop("reminder_time", None)

        # history → completions (keep only completed=True entries)
        if "history" in habit and "completions" not in habit:
            norm["completions"] = [
                {"date": e["date"], "notes": e.get("notes", "")}
                for e in habit["history"]
                if e.get("completed", False)
            ]
            norm.pop("history", None)

        # target_count → target_value
        if "target_count" in habit and "target_value" not in habit:
            norm["target_value"] = float(habit["target_count"])
            norm.pop("target_count", None)

        result.append(norm)
    return result


def _loop_freq(frequency) -> tuple[int, int]:
    if frequency == "daily":
        return 1, 1
    if frequency == "weekly":
        return 1, 7
    if isinstance(frequency, dict):
        return int(frequency.get("num", 1)), int(frequency.get("den", 1))
    if isinstance(frequency, (list, tuple)) and len(frequency) == 2:
        return int(frequency[0]), int(frequency[1])
    return 1, 1


def _loop_day_mask(days) -> int:
    if days is None:
        return _LOOP_ALL_DAYS
    if isinstance(days, int):
        return days
    return sum(_LOOP_DAY_BITS.get(d, 0) for d in days)


def _loop_date_to_ts(date_str: str) -> int:
    dt = datetime.strptime(date_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def inject_loop_habits(step: dict, task_dir: Path, device: str | None) -> bool:
    fixture_path = task_dir / step.get("fixture", "fixtures/loop_habits/habits.json")
    if not fixture_path.exists():
        print(f"  [FAIL] fixture not found: {fixture_path}", file=sys.stderr)
        return False

    with open(fixture_path, encoding="utf-8") as f:
        habits = _normalize_loop_habits(json.load(f))
    if not isinstance(habits, list):
        print("  [FAIL] habits fixture must be a JSON list", file=sys.stderr)
        return False

    ok, _ = adb_shell(f"ls {LOOP_HABITS_DB_REMOTE}", device=device)
    if not ok:
        print("  [INFO] DB not found — launching app to initialise...")
        adb_shell(f"am start -n {LOOP_HABITS_ACTIVITY}", device=device)
        time.sleep(4)
        ok, _ = adb_shell(f"ls {LOOP_HABITS_DB_REMOTE}", device=device)
        if not ok:
            print("  [FAIL] DB still not found after app launch", file=sys.stderr)
            return False

    adb_shell(f"am force-stop {LOOP_HABITS_PACKAGE}", device=device)
    time.sleep(1)

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_db = tmp.name
    ok, msg = _run(_adb_prefix(device) + ["pull", LOOP_HABITS_DB_REMOTE, tmp_db], device=device)
    if not ok:
        print(f"  [FAIL] pull DB: {msg}", file=sys.stderr)
        return False
    print("  [OK] pulled uhabits.db")

    try:
        conn = sqlite3.connect(tmp_db)
        cur  = conn.cursor()
        if not step.get("no_clear", False):
            cur.execute("DELETE FROM Repetitions")
            cur.execute("DELETE FROM Habits")
            print("  [OK] cleared existing habits and repetitions")

        for pos, habit in enumerate(habits):
            habit_type = 0 if habit.get("type", "boolean") == "boolean" else 1
            freq_num, freq_den = _loop_freq(habit.get("frequency", "daily"))
            reminder_days = _loop_day_mask(habit.get("reminder_days"))
            cur.execute("""
                INSERT INTO Habits
                    (name, description, question, type, color,
                     freq_num, freq_den,
                     reminder_hour, reminder_min, reminder_days,
                     archived, position,
                     target_type, target_value, unit,
                     highlight, uuid)
                VALUES (?,?,?,?,?, ?,?, ?,?,?, ?,?, ?,?,?, ?,?)
            """, (
                habit.get("name", "Habit"),
                habit.get("description", ""),
                habit.get("question", ""),
                habit_type,
                int(habit.get("color", 3)),
                freq_num, freq_den,
                int(habit.get("reminder_hour", -1)),
                int(habit.get("reminder_min", 0)),
                reminder_days,
                1 if habit.get("archived", False) else 0,
                pos,
                int(habit.get("target_type", 0)),
                float(habit.get("target_value", 0.0)),
                habit.get("unit", ""),
                0,
                str(uuid_mod.uuid4()),
            ))
            habit_id = cur.lastrowid

            for entry in habit.get("completions", []):
                if isinstance(entry, str):
                    date_str, value = entry, 2
                else:
                    date_str = entry.get("date", "")
                    raw = entry.get("value", 2)
                    value = int(raw * 1000) if habit_type == 1 else int(raw)
                if not date_str:
                    continue
                ts = _loop_date_to_ts(date_str)
                cur.execute(
                    "INSERT OR REPLACE INTO Repetitions (habit, timestamp, value, notes) VALUES (?,?,?,?)",
                    (habit_id, ts, value,
                     entry.get("notes", "") if isinstance(entry, dict) else ""),
                )

            print(f"  [OK] habit '{habit.get('name')}' — {len(habit.get('completions', []))} completion(s)")

        conn.commit()
    except Exception as exc:
        print(f"  [FAIL] SQLite: {exc}", file=sys.stderr)
        conn.close()
        os.unlink(tmp_db)
        return False
    finally:
        conn.close()

    ok = adb_push(tmp_db, LOOP_HABITS_DB_REMOTE, device=device, root_required=True)
    os.unlink(tmp_db)
    if not ok:
        return False

    adb_shell(
        f"pm grant {LOOP_HABITS_PACKAGE} android.permission.POST_NOTIFICATIONS",
        device=device,
    )
    adb_shell(f"am start -n {LOOP_HABITS_ACTIVITY}", device=device)
    print(f"  [OK] Loop Habit Tracker restarted ({len(habits)} habit(s) injected)")
    return True


# ── My Expenses ───────────────────────────────────────────────────────────────

_MY_EXP_ACCOUNT_TYPES  = {"CASH": 1, "BANK": 2, "CCARD": 3, "ASSET": 4, "LIABILITY": 5}
_MY_EXP_CATEGORY_TYPES = {"expense": 1, "income": 2}

_MY_EXP_TYPE_ALIASES = {
    "checking": "BANK", "savings": "BANK",
    "credit": "CCARD", "credit_card": "CCARD",
    "cash": "CASH",
    "asset": "ASSET",
    "liability": "LIABILITY",
}


def _normalize_my_expenses_account_type(raw_type) -> str:
    raw_type = str(raw_type or "CASH")
    return _MY_EXP_TYPE_ALIASES.get(raw_type.lower(), raw_type.upper())


def _normalize_my_expenses_account_color(value) -> int:
    return _hex_to_android_color(value, default=-3355444)


def _my_expenses_record_key(item: object, fields: tuple[str, ...]) -> str | None:
    if not isinstance(item, dict):
        return None
    for field in fields:
        value = item.get(field)
        if value not in (None, ""):
            return str(value)
    return None


def _dedupe_my_expenses_records(
    records: list, fields: tuple[str, ...], *, prefer_latest: bool = False
) -> list:
    seen: dict[str, int] = {}
    result: list = []
    for record in records:
        key = _my_expenses_record_key(record, fields)
        if key is None or key not in seen:
            if key is not None:
                seen[key] = len(result)
            result.append(record)
            continue
        if prefer_latest:
            result[seen[key]] = record
    return result


def _merge_my_expenses_containers(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, list):
        return {}

    merged = {"accounts": [], "categories": [], "payees": [], "transactions": []}
    for container in raw:
        if not isinstance(container, dict):
            continue
        for key in merged:
            values = container.get(key) or []
            if isinstance(values, list):
                merged[key].extend(values)
    merged["accounts"] = _dedupe_my_expenses_records(
        merged["accounts"], ("account_id", "id", "label", "name")
    )
    merged["categories"] = _dedupe_my_expenses_records(
        merged["categories"], ("category_id", "id", "label", "name")
    )
    merged["payees"] = _dedupe_my_expenses_records(
        merged["payees"], ("payee_id", "id", "name")
    )
    merged["transactions"] = _dedupe_my_expenses_records(
        merged["transactions"], ("transaction_id", "id"), prefer_latest=True
    )
    return merged


def _normalize_my_expenses_data(raw) -> dict:
    """Convert ID-based or list-wrapped fixtures to the label-reference format
    expected by inject_my_expenses.

    Handles three observed variants:
      1. Already-correct: dict with label/account/category fields — returned as-is.
      2. Dict with name/account_id/category_id fields (TGUI10 style).
      3. Single-element list wrapping either of the above (most complex_tasks).
    """
    data = _merge_my_expenses_containers(raw)

    accs = data.get("accounts", [])
    txs  = data.get("transactions", [])
    # Already correct if first account uses "label" and first tx uses "account"
    if accs and "label" in accs[0] and (not txs or "account" in txs[0]):
        normalized = dict(data)
        normalized["accounts"] = [
            {
                **acc,
                "type": _normalize_my_expenses_account_type(acc.get("type", "CASH")),
                "color": _normalize_my_expenses_account_color(acc.get("color", -3355444)),
            }
            for acc in accs
        ]
        return normalized

    # Build id → label maps
    acc_id_to_label: dict[str, str] = {}
    norm_accounts = []
    for acc in accs:
        label = acc.get("label") or acc.get("name", "")
        acc_id = acc.get("account_id") or acc.get("id", "")
        if acc_id:
            acc_id_to_label[acc_id] = label
        norm_type = _normalize_my_expenses_account_type(acc.get("type", "CASH"))
        opening = acc.get("opening_balance") or acc.get("initial_balance") or acc.get("balance") or 0
        norm_accounts.append({
            "label":           label,
            "currency":        acc.get("currency", "CNY"),
            "type":            norm_type,
            "opening_balance": opening,
            "description":     acc.get("description", ""),
            "color":           _normalize_my_expenses_account_color(acc.get("color", -3355444)),
        })

    cat_id_to_label: dict[str, str] = {}
    norm_categories = []
    for cat in data.get("categories", []):
        label = cat.get("label") or cat.get("name", "")
        cat_id = cat.get("category_id") or cat.get("id", "")
        if cat_id:
            cat_id_to_label[cat_id] = label
        norm_categories.append({
            "label":  label,
            "type":   cat.get("type", "expense"),
            "parent": cat_id_to_label.get(cat.get("parent_id") or "", None),
        })

    payee_id_to_name: dict[str, str] = {}
    norm_payees = []
    for p in data.get("payees", []):
        if isinstance(p, str):
            norm_payees.append(p)
        else:
            name = p.get("name", "")
            pid  = p.get("payee_id", "")
            if pid:
                payee_id_to_name[pid] = name
            if name:
                norm_payees.append(name)

    norm_transactions = []
    for tx in txs:
        raw_account = tx.get("account")
        raw_category = tx.get("category")
        acc_label = (
            acc_id_to_label.get(str(raw_account), "")
            if raw_account not in (None, "")
            else acc_id_to_label.get(tx.get("account_id", ""), "")
        ) or raw_account or ""
        cat_label = (
            cat_id_to_label.get(str(raw_category), "")
            if raw_category not in (None, "")
            else cat_id_to_label.get(tx.get("category_id", ""), "")
        ) or raw_category or ""
        payee     = tx.get("payee") or payee_id_to_name.get(tx.get("payee_id", ""), "")
        comment   = tx.get("comment") or tx.get("description") or tx.get("notes", "")
        norm_transactions.append({
            "account":  acc_label,
            "date":     tx.get("date", ""),
            "amount":   tx.get("amount", 0),
            "category": cat_label,
            "payee":    payee,
            "comment":  comment,
            "transaction_id": tx.get("transaction_id") or tx.get("id", ""),
        })

    return {
        "accounts":     norm_accounts,
        "categories":   norm_categories,
        "payees":       norm_payees,
        "transactions": norm_transactions,
    }


_MY_EXP_QIF_ACCOUNT_TYPES = {
    "CASH": "Cash",
    "BANK": "Bank",
    "CCARD": "CCard",
    "ASSET": "Oth A",
    "LIABILITY": "Oth L",
}


def _myexp_date_to_ts(date_str: str) -> int:
    dt = datetime.strptime(date_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _myexp_minor_to_major(amount: int) -> str:
    sign = "-" if amount < 0 else ""
    major = abs(int(amount)) / 100.0
    return f"{sign}{major:.2f}"


def _build_my_expenses_qif(data: dict) -> str:
    lines: list[str] = []
    transactions_by_account: dict[str, list[dict]] = {}
    for tx in data.get("transactions", []):
        account = tx.get("account", "")
        if account:
            transactions_by_account.setdefault(account, []).append(tx)

    for account in data.get("accounts", []):
        label = account.get("label", "").strip()
        if not label:
            continue
        qif_type = _MY_EXP_QIF_ACCOUNT_TYPES.get(account.get("type", "CASH").upper(), "Cash")
        lines.extend([
            "!Account",
            f"N{label}",
            f"T{qif_type}",
        ])
        description = account.get("description", "").strip()
        if description:
            lines.append(f"D{description}")
        opening_balance = int(account.get("opening_balance", 0))
        if opening_balance:
            lines.append(f"B{_myexp_minor_to_major(opening_balance)}")
        lines.append("^")
        lines.append(f"!Type:{qif_type}")

        for tx in transactions_by_account.get(label, []):
            date_str = tx.get("date", "2026-01-01").replace("-", "/")
            lines.extend([
                f"D{date_str}",
                f"T{_myexp_minor_to_major(int(tx.get('amount', 0)))}",
            ])
            payee = (tx.get("payee") or "").strip()
            if payee:
                lines.append(f"P{payee}")
            category = (tx.get("category") or "").strip()
            if category:
                lines.append(f"L{category}")
            comment = (tx.get("comment") or "").strip()
            if comment:
                lines.append(f"M{comment}")
            lines.append("^")

    return "\n".join(lines) + "\n"


def _prepare_my_expenses_qif(data: dict, device: str | None) -> str | None:
    remote_qif = "/sdcard/Download/claw_myexpenses_import.qif"
    if not data.get("accounts"):
        print("  [FAIL] my_expenses fixture must include accounts for QIF import", file=sys.stderr)
        return None

    with tempfile.NamedTemporaryFile(mode="w", suffix=".qif", delete=False, encoding="utf-8") as tmp:
        tmp.write(_build_my_expenses_qif(data))
        tmp_qif = tmp.name
    try:
        adb_shell(f"rm -f {remote_qif}", device=device)
        if not adb_push(tmp_qif, remote_qif, device=device):
            return None
    finally:
        os.unlink(tmp_qif)
    return remote_qif


def _my_expenses_import_succeeded(device: str | None) -> bool:
    ok, _ = _wait_for_ui_selector(device=device, text="未有开支!", timeout_s=3.0)
    if ok:
        return False
    ok, _ = _wait_for_ui_selector(device=device, text="开始使用", timeout_s=2.0)
    if ok:
        return False
    ok, _ = _wait_for_ui_selector(device=device, text="下一步", timeout_s=2.0)
    if ok:
        return False
    return True


def _my_expenses_home_ready(device: str | None) -> bool:
    selectors = (
        {"text": "更多"},
        {"text": "交易"},
        {"text": "账户"},
        {"text_contains": "预算账簿"},
        {"text": "未有开支!"},
    )
    for selector in selectors:
        ok, _ = _wait_for_ui_selector(device=device, timeout_s=1.5, **selector)
        if ok:
            return True
    return False


def _inject_my_expenses_via_non_ui_import(data: dict, device: str | None) -> bool:
    remote_qif = _prepare_my_expenses_qif(data, device=device)
    if not remote_qif:
        return False

    clear_ok, clear_msg = adb_shell(f"pm clear {MY_EXPENSES_PACKAGE}", device=device)
    if not clear_ok:
        print(f"  [FAIL] failed to clear {MY_EXPENSES_PACKAGE}: {clear_msg}", file=sys.stderr)
        return False
    print("  [OK] reset My Expenses app data for clean non-UI import")

    import_cmd = (
        "am start -W "
        "-a android.intent.action.VIEW "
        f"-n {MY_EXPENSES_PACKAGE}/.activity.BackupRestoreActivity "
        f'-d "file://{remote_qif}" '
        "-t application/octet-stream"
    )
    import_ok, import_msg = adb_shell(import_cmd, device=device)
    if not import_ok:
        print(f"  [FAIL] failed to launch non-UI My Expenses import: {import_msg}", file=sys.stderr)
        return False
    print("  [OK] launched My Expenses non-UI import activity")
    time.sleep(6)

    launch_ok, launch_msg = _start_package_launcher(
        MY_EXPENSES_PACKAGE,
        device=device,
        fallback_activity=MY_EXPENSES_ACTIVITY,
    )
    if not launch_ok:
        print(f"  [FAIL] failed to relaunch My Expenses after non-UI import: {launch_msg}", file=sys.stderr)
        return False
    time.sleep(4)

    if not _my_expenses_import_succeeded(device=device):
        print("  [FAIL] non-UI import did not populate My Expenses ledger", file=sys.stderr)
        return False
    print("  [OK] My Expenses imported through non-UI restore flow")
    return True


def _inject_my_expenses_via_qif(data: dict, device: str | None) -> bool:
    remote_qif = _prepare_my_expenses_qif(data, device=device)
    if not remote_qif:
        return False

    clear_ok, clear_msg = adb_shell(f"pm clear {MY_EXPENSES_PACKAGE}", device=device)
    if not clear_ok:
        print(f"  [FAIL] failed to clear {MY_EXPENSES_PACKAGE}: {clear_msg}", file=sys.stderr)
        return False
    print("  [OK] reset My Expenses app data for clean non-root import")

    launch_ok, launch_msg = _start_package_launcher(
        MY_EXPENSES_PACKAGE,
        device=device,
        fallback_activity=MY_EXPENSES_ACTIVITY,
    )
    if not launch_ok:
        print(f"  [FAIL] failed to launch My Expenses: {launch_msg}", file=sys.stderr)
        return False
    time.sleep(3)
    deadline = time.time() + 20
    while time.time() < deadline:
        if _my_expenses_home_ready(device=device):
            break
        ok, _ = _tap_ui_selector(device=device, text="下一步", timeout_s=1.0, poll_s=0.2)
        if ok:
            time.sleep(1)
            continue
        ok, _ = _tap_ui_selector(device=device, text="开始使用", timeout_s=1.0, poll_s=0.2)
        if ok:
            time.sleep(2)
            continue
        time.sleep(0.5)
    else:
        print("  [FAIL] timed out while dismissing My Expenses onboarding", file=sys.stderr)
        return False

    navigation_steps = [
        {"text": "更多"},
        {"text": "设置"},
        {"text": "导入 / 导出"},
        {"text": "导入 QIF"},
    ]
    for selector in navigation_steps:
        ok, msg = _tap_ui_selector(device=device, **selector)
        if not ok:
            print(f"  [FAIL] failed to navigate My Expenses import UI: {selector} ({msg})", file=sys.stderr)
            return False
        time.sleep(1)

    ok, msg = _tap_ui_selector(device=device, desc_contains="Browse files on this device")
    if not ok:
        ok, msg = _tap_ui_selector(device=device, text_contains="Browse", timeout_s=4.0)
    if not ok:
        print(f"  [FAIL] failed to open file picker for QIF import ({msg})", file=sys.stderr)
        return False
    time.sleep(2)

    picker_steps = [
        {"text": "Download"},
        {"text": "下载内容"},
        {"text": "claw_myexpenses_import.qif"},
    ]
    file_selected = False
    for selector in picker_steps:
        ok, _ = _tap_ui_selector(device=device, timeout_s=4.0, **selector)
        if ok and selector.get("text") == "claw_myexpenses_import.qif":
            file_selected = True
            break
        if ok:
            time.sleep(1)
    if not file_selected:
        ok, msg = _tap_ui_selector(device=device, text="claw_myexpenses_import.qif", timeout_s=6.0)
        file_selected = ok
        if not ok:
            print(f"  [FAIL] failed to pick QIF file from DocumentsUI ({msg})", file=sys.stderr)
            return False
    time.sleep(2)

    ok, msg = _tap_ui_selector(device=device, text="确定", timeout_s=8.0)
    if not ok:
        print(f"  [FAIL] failed to confirm QIF import ({msg})", file=sys.stderr)
        return False
    time.sleep(5)

    launch_ok, launch_msg = _start_package_launcher(
        MY_EXPENSES_PACKAGE,
        device=device,
        fallback_activity=MY_EXPENSES_ACTIVITY,
    )
    if not launch_ok:
        print(f"  [FAIL] failed to relaunch My Expenses after QIF import: {launch_msg}", file=sys.stderr)
        return False

    if not _my_expenses_import_succeeded(device=device):
        print("  [FAIL] QIF import completed but app still shows empty ledger", file=sys.stderr)
        return False
    print("  [OK] My Expenses imported through in-app QIF flow")
    return True


# Shell script (run as root, app force-stopped) that rewrites My Expenses
# shared_prefs so the direct-DB inject path skips the first-run wizard.
# Pure grep/printf/cat only — no awk/sed, which many emulator toybox builds
# lack. Overwriting via `cat > "$PREFS"` preserves the existing file's
# inode/owner/SELinux context; a freshly created file gets owner + restorecon.
_ME_PREFS_SCRIPT = r'''
PREFS="@@PREFS@@"
PKGDIR="@@PKG_DIR@@"
V="@@V@@"
ACC="@@ACC@@"
CUR="@@CUR@@"
DIR=$(dirname "$PREFS")
TMP=/data/local/tmp/claw_me_prefs.work
NEW=0
mkdir -p "$DIR"
if [ -f "$PREFS" ]; then
  ENTRIES=$(grep -vE 'name="(currentversion|first_install_version|current_account|home_currency)"' "$PREFS" | grep -vE '<\?xml|<map|</map>')
else
  NEW=1
  ENTRIES=""
fi
{
  echo "<?xml version='1.0' encoding='utf-8' standalone='yes' ?>"
  echo "<map>"
  echo "    <int name=\"currentversion\" value=\"$V\" />"
  echo "    <int name=\"first_install_version\" value=\"$V\" />"
  echo "    <long name=\"current_account\" value=\"$ACC\" />"
  echo "    <string name=\"home_currency\">$CUR</string>"
  [ -n "$ENTRIES" ] && printf '%s\n' "$ENTRIES"
  echo "</map>"
} > "$TMP"
cat "$TMP" > "$PREFS"
if [ "$NEW" = "1" ]; then
  OWNER=$(stat -c %U "$PKGDIR" 2>/dev/null)
  [ -n "$OWNER" ] && chown "$OWNER":"$OWNER" "$PREFS"
fi
restorecon "$PREFS" 2>/dev/null || true
rm -f "$TMP"
grep -E 'currentversion|current_account|home_currency' "$PREFS"
'''


def _my_expenses_version_code(device: str | None) -> int | None:
    ok, out = adb_shell(
        f"dumpsys package {MY_EXPENSES_PACKAGE} | grep -m1 versionCode",
        device=device,
    )
    if not ok:
        return None
    m = re.search(r"versionCode=(\d+)", out)
    return int(m.group(1)) if m else None


def _set_my_expenses_onboarding_prefs(
    device: str | None,
    account_id: int | None,
    currency: str,
) -> bool:
    """Mark My Expenses onboarding complete in shared_prefs.

    The first-run wizard is gated by the ``currentversion`` preference
    (My Expenses ``PrefKey.CURRENT_VERSION``): when it is absent the app shows
    OnboardingActivity regardless of DB content, so the direct-DB inject path
    lands on the wizard instead of the ledger. We also point ``current_account``
    at a freshly injected account and set ``home_currency`` so the app opens on
    real data rather than a stale/deleted account id.

    Must be called while the app is force-stopped — otherwise My Expenses
    overwrites shared_prefs from memory when its process exits.
    """
    vcode = _my_expenses_version_code(device)
    if vcode is None:
        # Onboarding skip is the priority; any value != -1 satisfies the gate.
        print("  [WARN] could not read My Expenses versionCode; using fallback",
              file=sys.stderr)
        vcode = 999999
    acc = account_id if account_id is not None else -1
    cur = currency or "USD"

    script = _ME_PREFS_SCRIPT
    for token, val in (
        ("@@PREFS@@", MY_EXPENSES_PREFS_REMOTE),
        ("@@PKG_DIR@@", f"/data/data/{MY_EXPENSES_PACKAGE}"),
        ("@@V@@", str(vcode)),
        ("@@ACC@@", str(acc)),
        ("@@CUR@@", str(cur)),
    ):
        script = script.replace(token, val)

    with tempfile.NamedTemporaryFile(
        "w", suffix=".sh", delete=False, encoding="utf-8"
    ) as f:
        f.write(script)
        local_sh = f.name
    remote_sh = "/data/local/tmp/claw_me_prefs_fix.sh"
    pushed = adb_push(local_sh, remote_sh, device=device)
    os.unlink(local_sh)
    if not pushed:
        return False
    ok, out = adb_shell(
        f"su 0 sh {remote_sh}", device=device, root_required=True
    )
    adb_shell(f"rm -f {remote_sh}", device=device)
    if not ok:
        print(f"  [FAIL] set My Expenses onboarding prefs: {out}",
              file=sys.stderr)
        return False
    print(f"  [OK] onboarding prefs set (currentversion={vcode}, "
          f"current_account={acc}, home_currency={cur})")
    return True


def inject_my_expenses(step: dict, task_dir: Path, device: str | None) -> bool:
    fixture_path = task_dir / step.get("fixture", "fixtures/myexpenses/data.json")
    if not fixture_path.exists():
        print(f"  [FAIL] fixture not found: {fixture_path}", file=sys.stderr)
        return False

    with open(fixture_path, encoding="utf-8") as f:
        data = _normalize_my_expenses_data(json.load(f))
    if not data:
        print("  [FAIL] my_expenses fixture is empty or unrecognised format", file=sys.stderr)
        return False

    installed, package_msg = _package_installed(MY_EXPENSES_PACKAGE, device=device)
    if not installed:
        print(
            f"  [FAIL] package not installed: {MY_EXPENSES_PACKAGE}"
            + (f" ({package_msg})" if package_msg else ""),
            file=sys.stderr,
        )
        return False

    ok, db_probe = _adb_file_exists(MY_EXPENSES_DB_REMOTE, device=device, root_required=True)
    if not ok:
        print("  [INFO] DB not found — launching app to initialise...")
        launch_ok, launch_msg = _start_package_launcher(
            MY_EXPENSES_PACKAGE,
            device=device,
            fallback_activity=MY_EXPENSES_ACTIVITY,
        )
        if not launch_ok:
            print(
                f"  [FAIL] failed to launch package {MY_EXPENSES_PACKAGE}: {launch_msg}",
                file=sys.stderr,
            )
            return False
        time.sleep(5)
        ok, db_probe = _adb_file_exists(MY_EXPENSES_DB_REMOTE, device=device, root_required=True)
        if not ok:
            print("  [INFO] root DB access unavailable — trying non-UI import")
            if _inject_my_expenses_via_non_ui_import(data, device=device):
                return True
            print(
                "  [FAIL] My Expenses init requires either root DB access or a working non-UI import path. "
                "Current emulator image supports neither; please switch to a rooted / adb-root-capable image.",
                file=sys.stderr,
            )
            return False

    adb_shell(f"am force-stop {MY_EXPENSES_PACKAGE}", device=device)
    time.sleep(1)

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_db = tmp.name
    ok, msg = _run(
        _adb_prefix(device) + ["pull", MY_EXPENSES_DB_REMOTE, tmp_db],
        root_required=True,
        device=device,
    )
    if not ok:
        print(f"  [FAIL] pull DB: {msg}", file=sys.stderr)
        return False
    print("  [OK] pulled My Expenses DB")

    try:
        conn = sqlite3.connect(tmp_db)
        cur  = conn.cursor()
        if not step.get("no_clear", False):
            cur.execute("DELETE FROM transactions")
            cur.execute("DELETE FROM payee")
            cur.execute("DELETE FROM categories WHERE _id > 0")
            cur.execute("DELETE FROM accounts")
            print("  [OK] cleared existing data")

        account_id_map: dict[str, int] = {}
        for acc in data.get("accounts", []):
            acc_type = _MY_EXP_ACCOUNT_TYPES.get(acc.get("type", "CASH").upper(), 1)
            cur.execute("""
                INSERT INTO accounts
                    (label, currency, type, opening_balance, description, color, uuid, grouping, sort_direction)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                acc["label"], acc.get("currency", "USD"), acc_type,
                int(acc.get("opening_balance", 0)), acc.get("description", ""),
                _normalize_my_expenses_account_color(acc.get("color", -3355444)),
                str(uuid_mod.uuid4()), "NONE", "DESC",
            ))
            account_id_map[acc["label"]] = cur.lastrowid
            print(f"  [OK] account '{acc['label']}'")

        category_id_map: dict[str, int] = {}
        for cat in data.get("categories", []):
            cat_type   = _MY_EXP_CATEGORY_TYPES.get(cat.get("type", "expense").lower(), 1)
            parent_id  = category_id_map.get(cat.get("parent")) if cat.get("parent") else None
            cur.execute("""
                INSERT OR IGNORE INTO categories (label, label_normalized, parent_id, type, uuid)
                VALUES (?,?,?,?,?)
            """, (cat["label"], cat["label"].lower(), parent_id, cat_type, str(uuid_mod.uuid4())))
            category_id_map[cat["label"]] = cur.lastrowid

        payee_id_map: dict[str, int] = {}
        for entry in data.get("payees", []):
            name = entry if isinstance(entry, str) else entry.get("name", "")
            if not name:
                continue
            cur.execute("INSERT OR IGNORE INTO payee (name) VALUES (?)", (name,))
            cur.execute("SELECT _id FROM payee WHERE name=?", (name,))
            row = cur.fetchone()
            if row:
                payee_id_map[name] = row[0]

        tx_count = 0
        for tx in data.get("transactions", []):
            account_id = account_id_map.get(tx.get("account", ""))
            if account_id is None:
                print(f"  [WARN] account '{tx.get('account')}' not found, skipping", file=sys.stderr)
                continue
            ts     = _myexp_date_to_ts(tx.get("date", "2026-01-01"))
            cat_id = category_id_map.get(tx["category"]) if tx.get("category") else None
            payee_id = payee_id_map.get(tx["payee"]) if tx.get("payee") else None
            cur.execute("""
                INSERT INTO transactions
                    (account_id, amount, date, value_date, cat_id, payee_id, comment, status, cr_status, uuid)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (
                account_id, int(tx.get("amount", 0)), ts, ts,
                cat_id, payee_id, tx.get("comment", ""),
                0, "RECONCILED", str(uuid_mod.uuid4()),
            ))
            tx_count += 1

        conn.commit()
        print(f"  [OK] {len(data.get('accounts', []))} account(s), "
              f"{len(data.get('categories', []))} categor(ies), {tx_count} transaction(s) injected")
        _accounts_list = data.get("accounts", []) or []
        _onboarding_account_id = next(iter(account_id_map.values()), None)
        _onboarding_currency = (
            _accounts_list[0].get("currency", "USD") if _accounts_list else "USD"
        )
    except Exception as exc:
        print(f"  [FAIL] SQLite: {exc}", file=sys.stderr)
        conn.close()
        os.unlink(tmp_db)
        return False
    finally:
        conn.close()

    ok = adb_push(tmp_db, MY_EXPENSES_DB_REMOTE, device=device, root_required=True)
    os.unlink(tmp_db)
    if not ok:
        return False
    # App is still force-stopped here (force-stop above, relaunch below): the
    # only safe window to rewrite shared_prefs without My Expenses clobbering
    # it on process exit. Without this the first-run wizard masks the injected
    # ledger (gated by the absent ``currentversion`` pref).
    _set_my_expenses_onboarding_prefs(
        device, _onboarding_account_id, _onboarding_currency
    )
    launch_ok, launch_msg = _start_package_launcher(
        MY_EXPENSES_PACKAGE,
        device=device,
        fallback_activity=MY_EXPENSES_ACTIVITY,
    )
    if not launch_ok:
        print(
            f"  [FAIL] failed to relaunch package {MY_EXPENSES_PACKAGE}: {launch_msg}",
            file=sys.stderr,
        )
        return False
    time.sleep(4)
    print("  [OK] My Expenses restarted")
    return True


# ── OpenTracks ────────────────────────────────────────────────────────────────

def _generate_trackpoints(track: dict, start_ts_ms: int) -> list[dict]:
    import math
    loc = track.get("start_location", {"lat": 31.2304, "lng": 121.4737, "elevation": 5.0})
    heading_rad = math.radians(track.get("heading_degrees", 45))
    distance_m  = float(track.get("distance_meters", 1000.0))
    duration_s  = float(track.get("duration_seconds", 600))
    hr          = track.get("avg_heart_rate")

    num_points   = max(10, min(int(distance_m / 30), 300))
    interval_ms  = int(duration_s * 1000 / num_points)
    dist_per_step = distance_m / num_points
    avg_speed    = distance_m / duration_s

    lat_per_m = 1.0 / 111_000.0
    lng_per_m = 1.0 / (111_000.0 * math.cos(math.radians(loc["lat"])))
    dlat = math.cos(heading_rad) * dist_per_step * lat_per_m
    dlng = math.sin(heading_rad) * dist_per_step * lng_per_m

    lat, lng     = loc["lat"], loc["lng"]
    elev_base    = float(loc.get("elevation", 0.0))
    bearing_deg  = track.get("heading_degrees", 45)

    points = []
    for i in range(num_points):
        tp_type = "-1" if i == 0 else ("3" if i == num_points - 1 else "0")
        speed   = avg_speed * (0.85 + 0.3 * abs(math.sin(i * 0.4)))
        elev    = elev_base + math.sin(i * 0.15) * 3.0
        points.append({
            "latitude":         int(lat * 1_000_000),
            "longitude":        int(lng * 1_000_000),
            "time":             start_ts_ms + i * interval_ms,
            "elevation":        elev,
            "accuracy":         5.0,
            "speed":            speed,
            "bearing":          float(bearing_deg),
            "sensor_heartrate": float(hr) if hr else None,
            "type":             tp_type,
        })
        lat += dlat
        lng += dlng
    return points


def inject_opentracks(step: dict, task_dir: Path, device: str | None) -> bool:
    fixture_path = task_dir / step.get("fixture", "fixtures/opentracks/tracks.json")
    if not fixture_path.exists():
        print(f"  [FAIL] fixture not found: {fixture_path}", file=sys.stderr)
        return False

    with open(fixture_path, encoding="utf-8") as f:
        tracks = json.load(f)
    if not isinstance(tracks, list):
        print("  [FAIL] tracks.json must be a JSON list", file=sys.stderr)
        return False

    PACKAGE   = "de.dennisguse.opentracks.playstore"
    DB_REMOTE = f"/data/data/{PACKAGE}/databases/database.db"
    ACTIVITY  = f"{PACKAGE}/de.dennisguse.opentracks.TrackListActivity"

    ok, _ = adb_shell(f"ls {DB_REMOTE}", device=device)
    if not ok:
        print("  [INFO] DB not found — launching app to initialise...")
        adb_shell(f"am start -n {ACTIVITY}", device=device)
        time.sleep(3)
        ok, _ = adb_shell(f"ls {DB_REMOTE}", device=device)
        if not ok:
            print("  [FAIL] DB still not found after app launch", file=sys.stderr)
            return False

    adb_shell(f"am force-stop {PACKAGE}", device=device)
    time.sleep(1)

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_db = tmp.name
    ok, msg = _run(_adb_prefix(device) + ["pull", DB_REMOTE, tmp_db], device=device)
    if not ok:
        print(f"  [FAIL] pull DB: {msg}", file=sys.stderr)
        return False
    print("  [OK] pulled OpenTracks DB")

    try:
        conn = sqlite3.connect(tmp_db)
        cur  = conn.cursor()
        if not step.get("no_clear", False):
            cur.execute("DELETE FROM markers")
            cur.execute("DELETE FROM trackpoints")
            cur.execute("DELETE FROM tracks")
            print("  [OK] cleared existing tracks")

        total = 0
        for track in tracks:
            start_ts  = _iso_to_unix_ms(track["start_time"])
            dur_s     = float(track.get("duration_seconds", 600))
            stop_ts   = start_ts + int(dur_s * 1000)
            dist_m    = float(track.get("distance_meters", 1000.0))
            avg_speed = dist_m / dur_s
            elev_gain = float(track.get("elevation_gain_m", 0.0))
            tz_off    = _tz_offset_ms(track["start_time"]) // 1000

            cur.execute("""
                INSERT INTO tracks
                    (name, description, category, starttime, stoptime,
                     numpoints, totaldistance, totaltime, movingtime,
                     avgspeed, avgmovingspeed, maxspeed,
                     minelevation, maxelevation, elevationgain, elevationloss,
                     icon, uuid, starttime_offset, activity_type)
                VALUES (?,?,?,?,?, ?,?,?,?, ?,?,?, ?,?, ?,?, ?,?,?,?)
            """, (
                track.get("name", "Track"), track.get("description", ""),
                track.get("category", track.get("activity_type", "")),
                start_ts, stop_ts,
                0, dist_m, int(dur_s * 1000), int(dur_s * 1000),
                avg_speed, avg_speed, avg_speed * 1.4,
                0.0,
                float(track.get("start_location", {}).get("elevation", 0)) + elev_gain,
                elev_gain, elev_gain * 0.6,
                track.get("activity_type", "run"),
                uuid_mod.uuid4().bytes, tz_off,
                track.get("activity_type", "run"),
            ))
            track_id = cur.lastrowid

            tps = _generate_trackpoints(track, start_ts)
            cur.executemany("""
                INSERT INTO trackpoints
                    (trackid, longitude, latitude, time,
                     elevation, accuracy, speed, bearing,
                     sensor_heartrate, type)
                VALUES (?,?,?,?, ?,?,?,?, ?,?)
            """, [
                (track_id,
                 tp["longitude"], tp["latitude"], tp["time"],
                 tp["elevation"], tp["accuracy"], tp["speed"], tp["bearing"],
                 tp["sensor_heartrate"], tp["type"])
                for tp in tps
            ])
            cur.execute("UPDATE tracks SET numpoints=? WHERE _id=?", (len(tps), track_id))

            for wp in track.get("waypoints", []):
                offset_s = float(wp.get("offset_seconds", 0))
                wp_ts    = start_ts + int(offset_s * 1000)
                idx      = min(int(offset_s / dur_s * len(tps)), len(tps) - 1)
                tp       = tps[idx]
                cur.execute("""
                    INSERT INTO markers
                        (name, description, trackid,
                         longitude, latitude, time, elevation, accuracy)
                    VALUES (?,?,?,?,?,?,?,?)
                """, (
                    wp.get("title", "Waypoint"), wp.get("description", ""),
                    track_id,
                    tp["longitude"], tp["latitude"], wp_ts,
                    tp["elevation"], 5.0,
                ))

            total += 1
            print(f"  [OK] track '{track.get('name')}' — {len(tps)} pts, "
                  f"{len(track.get('waypoints', []))} waypoint(s)")

        conn.commit()
    except Exception as exc:
        print(f"  [FAIL] SQLite: {exc}", file=sys.stderr)
        conn.close()
        os.unlink(tmp_db)
        return False
    finally:
        conn.close()

    ok = adb_push(tmp_db, DB_REMOTE, device=device, root_required=True)
    os.unlink(tmp_db)
    if not ok:
        return False
    adb_shell(f"am start -n {ACTIVITY}", device=device)
    print(f"  [OK] OpenTracks restarted ({total} track(s) injected)")
    return True


# ── Google Clock ──────────────────────────────────────────────────────────────

_CLOCK_DAY_BITS = {
    "Monday": 1,
    "Tuesday": 2,
    "Wednesday": 4,
    "Thursday": 8,
    "Friday": 16,
    "Saturday": 32,
    "Sunday": 64,
}


def _clock_day_mask(days: list[str] | None) -> int:
    if not days:
        return 0
    return sum(_CLOCK_DAY_BITS.get(day, 0) for day in days)


def _clock_ensure_db(device: str | None) -> bool:
    ok, _ = adb_shell(f"ls {CLOCK_DB_REMOTE}", device=device, root_required=True)
    if ok:
        return True

    print("  [INFO] alarms.db not found — launching Clock to initialise DB...")
    launch_ok, launch_msg = _start_package_launcher(
        CLOCK_PACKAGE,
        device=device,
        fallback_activity=CLOCK_ACTIVITY,
    )
    if not launch_ok:
        print(f"  [FAIL] failed to launch Clock: {launch_msg}", file=sys.stderr)
        return False
    time.sleep(3)

    tapped, tap_msg = _tap_ui_selector(device=device, desc_contains="Alarm", timeout_s=4.0)
    if not tapped:
        tapped, tap_msg = _tap_ui_selector(device=device, text="Alarm", timeout_s=2.0)
    if not tapped:
        tapped, tap_msg = _tap_ui_selector(device=device, text="闹钟", timeout_s=2.0)
    if tapped:
        print("  [INFO] switched Clock to Alarm tab")
        time.sleep(2)
    else:
        print(f"  [INFO] could not confirm Alarm tab navigation ({tap_msg})")

    ok, _ = adb_shell(f"ls {CLOCK_DB_REMOTE}", device=device, root_required=True)
    if not ok:
        print("  [FAIL] alarms.db still not found after launching Clock", file=sys.stderr)
    return ok


def inject_clock(step: dict, task_dir: Path, device: str | None) -> bool:
    fixture_path = task_dir / step.get("fixture", "fixtures/clock_gui/alarms.json")
    if not fixture_path.exists():
        print(f"  [FAIL] fixture not found: {fixture_path}", file=sys.stderr)
        return False

    with open(fixture_path, encoding="utf-8") as f:
        alarms = json.load(f)
    if not isinstance(alarms, list):
        print("  [FAIL] alarms fixture must be a JSON list", file=sys.stderr)
        return False

    if not _clock_ensure_db(device=device):
        return False

    adb_shell(f"am force-stop {CLOCK_PACKAGE}", device=device)
    time.sleep(1)

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_db = tmp.name
    ok, msg = _run(
        _adb_prefix(device) + ["pull", CLOCK_DB_REMOTE, tmp_db],
        root_required=True,
        device=device,
    )
    if not ok:
        print(f"  [FAIL] pull alarms.db: {msg}", file=sys.stderr)
        return False
    print("  [OK] pulled alarms.db")

    try:
        conn = sqlite3.connect(tmp_db)
        cur = conn.cursor()

        if not step.get("no_clear", False):
            cur.execute("DELETE FROM alarm_instances")
            cur.execute("DELETE FROM alarm_templates")
            print("  [OK] cleared existing alarms")

        for alarm in alarms:
            time_value = str(alarm.get("time", "07:00"))
            hour, minute = map(int, time_value.split(":"))
            cur.execute(
                """
                INSERT INTO alarm_templates
                    (external_uuid, hour, minutes, daysofweek, enabled, vibrate,
                     label, ringtone, delete_after_use, wakeup)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid_mod.uuid4()),
                    hour,
                    minute,
                    _clock_day_mask(alarm.get("repeat_days")),
                    1 if alarm.get("enabled", True) else 0,
                    1 if alarm.get("vibrate", True) else 0,
                    alarm.get("label", ""),
                    None,
                    0,
                    0,
                ),
            )
            print(f"  [OK] alarm '{alarm.get('label', '?')}' at {time_value}")

        conn.commit()
    except Exception as exc:
        print(f"  [FAIL] SQLite: {exc}", file=sys.stderr)
        conn.close()
        os.unlink(tmp_db)
        return False
    finally:
        conn.close()

    ok = adb_push(tmp_db, CLOCK_DB_REMOTE, device=device, root_required=True)
    os.unlink(tmp_db)
    if not ok:
        return False

    launch_ok, launch_msg = _start_package_launcher(
        CLOCK_PACKAGE,
        device=device,
        fallback_activity=CLOCK_ACTIVITY,
    )
    if not launch_ok:
        print(f"  [FAIL] failed to relaunch Clock: {launch_msg}", file=sys.stderr)
        return False
    print(f"  [OK] Clock restarted ({len(alarms)} alarm(s) injected)")
    return True


def inject_fossify_notes(step: dict, task_dir: Path, device: str | None) -> bool:
    fixture_path = task_dir / step.get("fixture", "fixtures/fossify_notes_gui/notes.json")
    if not fixture_path.exists():
        print(f"  [FAIL] fixture not found: {fixture_path}", file=sys.stderr)
        return False

    with open(fixture_path, encoding="utf-8") as f:
        notes = json.load(f)
    if not isinstance(notes, list):
        print("  [FAIL] notes.json must be a list", file=sys.stderr)
        return False

    ok, _ = adb_shell(f"ls {FOSSIFY_NOTES_DB_REMOTE}", device=device, root_required=True)
    if not ok:
        print("  [INFO] notes.db not found — launching app to initialise...")
        launch_ok, launch_msg = _start_package_launcher(
            FOSSIFY_NOTES_PACKAGE,
            device=device,
            fallback_activity=FOSSIFY_NOTES_ACTIVITY,
        )
        if not launch_ok:
            print(f"  [FAIL] failed to launch Fossify Notes: {launch_msg}", file=sys.stderr)
            return False
        time.sleep(3)
        ok, _ = adb_shell(f"ls {FOSSIFY_NOTES_DB_REMOTE}", device=device, root_required=True)
        if not ok:
            print("  [FAIL] notes.db still not found after app launch", file=sys.stderr)
            return False

    adb_shell(f"am force-stop {FOSSIFY_NOTES_PACKAGE}", device=device)
    time.sleep(1)

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_db = tmp.name
    tmp_wal = tmp_db + "-wal"
    tmp_shm = tmp_db + "-shm"

    ok, msg = _run(
        _adb_prefix(device) + ["pull", FOSSIFY_NOTES_DB_REMOTE, tmp_db],
        root_required=True,
        device=device,
    )
    if not ok:
        print(f"  [FAIL] pull notes.db: {msg}", file=sys.stderr)
        return False
    _run(
        _adb_prefix(device) + ["pull", FOSSIFY_NOTES_DB_REMOTE + "-wal", tmp_wal],
        root_required=True,
        device=device,
    )
    print("  [OK] pulled notes.db")

    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(tmp_db)
        cur = conn.cursor()
        cur.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        if not step.get("no_clear", False):
            cur.execute("DELETE FROM notes")
            print("  [OK] cleared existing notes")

        for note in notes:
            title = note.get("title", "")
            content = note.get("content", "")
            note_type = 1 if note.get("checklist", False) else 0
            cur.execute(
                "INSERT INTO notes (title, value, type, path, protection_type, protection_hash) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (title, content, note_type, "", -1, ""),
            )
            print(f"  [OK] note '{title[:40]}'")

        conn.commit()
        cur.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    except Exception as exc:
        print(f"  [FAIL] SQLite: {exc}", file=sys.stderr)
        if conn is not None:
            conn.close()
        os.unlink(tmp_db)
        for path in (tmp_wal, tmp_shm):
            if os.path.exists(path):
                os.unlink(path)
        return False
    finally:
        if conn is not None:
            conn.close()

    ok = adb_push(tmp_db, FOSSIFY_NOTES_DB_REMOTE, device=device, root_required=True)
    with tempfile.NamedTemporaryFile(delete=False) as empty:
        empty_path = empty.name
    _run(
        _adb_prefix(device) + ["push", empty_path, FOSSIFY_NOTES_DB_REMOTE + "-wal"],
        root_required=True,
        device=device,
    )
    os.unlink(empty_path)
    for path in (tmp_db, tmp_wal, tmp_shm):
        if os.path.exists(path):
            os.unlink(path)
    if not ok:
        return False

    launch_ok, launch_msg = _start_package_launcher(
        FOSSIFY_NOTES_PACKAGE,
        device=device,
        fallback_activity=FOSSIFY_NOTES_ACTIVITY,
    )
    if not launch_ok:
        print(f"  [FAIL] failed to relaunch Fossify Notes: {launch_msg}", file=sys.stderr)
        return False
    print(f"  [OK] Fossify Notes restarted ({len(notes)} note(s) injected)")
    return True


# ── Project-shadow GUI apps (TestMall, Mattermost) ────────────────────────────
# Self-contained shadow apps registered in ``gen/persona.py`` but with no
# host-side CLI mock service backing them. They read their state from a
# fixture JSON pushed to /sdcard/Android/data/<package>/files/state.json —
# the same pattern inject_email uses for the Gmail clone.

TESTMALL_PACKAGE    = "com.testmall.app"
TESTMALL_STATE_PATH = f"/sdcard/Android/data/{TESTMALL_PACKAGE}/files/state.json"

MATTERMOST_PACKAGE    = "com.mattermost.rnbeta"
MATTERMOST_STATE_PATH = f"/sdcard/Android/data/{MATTERMOST_PACKAGE}/files/state.json"

SHADOW_STATE_ROOT = "/data/local/tmp/claw_gui_state"


def _shadow_state_fallback_path(package: str) -> str:
    return f"{SHADOW_STATE_ROOT}/{package}/state.json"


def _push_json_state(state: dict, remote_path: str, device: str | None) -> bool:
    remote_dir = remote_path.rsplit("/", 1)[0]
    adb_shell(f"mkdir -p {remote_dir}", device=device)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tmp:
        json.dump(state, tmp, ensure_ascii=False, indent=2)
        tmp_path = tmp.name
    try:
        return adb_push(tmp_path, remote_path, device=device)
    finally:
        os.unlink(tmp_path)


def _inject_shadow_app_state(
    step: dict,
    task_dir: Path,
    device: str | None,
    *,
    package: str,
    state_path: str,
    state_key: str,
    default_fixture: str,
    app_label: str,
) -> bool:
    """Push a fixture JSON list onto a project-shadow GUI app's state file.

    Wraps ``items`` as ``{state_key: items}`` so the on-device shape stays
    forward-compatible with apps that need top-level metadata later; mirrors
    inject_email for shadow apps without a CLI mock service.

    Always writes a host-readable fallback under ``SHADOW_STATE_ROOT`` so
    graders/app-direct tools can still consume fixture state when the APK is
    missing from the recommended image.
    """
    fixture_path = task_dir / step.get("fixture", default_fixture)
    if not fixture_path.exists():
        print(f"  [FAIL] fixture not found: {fixture_path}", file=sys.stderr)
        return False

    with open(fixture_path, encoding="utf-8") as f:
        items = json.load(f)
    if not isinstance(items, list):
        print(f"  [FAIL] {app_label} fixture must be a JSON list", file=sys.stderr)
        return False

    state = {state_key: items}
    fallback_path = _shadow_state_fallback_path(package)
    adb_shell(f"rm -f {fallback_path}", device=device)
    if not _push_json_state(state, fallback_path, device=device):
        return False

    installed, package_msg = _package_installed(package, device=device)
    if not installed:
        print(
            f"  [WARN] package not installed: {package}; "
            f"wrote {app_label} shadow state to {fallback_path}"
            + (f" ({package_msg})" if package_msg else ""),
            file=sys.stderr,
        )
        return True

    adb_shell(f"am force-stop {package}", device=device)
    adb_shell(f"rm -f {state_path}", device=device)
    if not _push_json_state(state, state_path, device=device):
        return False

    launch_ok, launch_msg = _start_package_launcher(package, device=device)
    if not launch_ok:
        print(f"  [FAIL] failed to relaunch {app_label}: {launch_msg}", file=sys.stderr)
        return False
    print(
        f"  [OK] {app_label} restarted ({len(items)} item(s) injected; "
        f"fallback state also at {fallback_path})"
    )
    return True


def inject_testmall(step: dict, task_dir: Path, device: str | None) -> bool:
    """Inject the TestMall product catalog into the shadow app's state.json."""
    return _inject_shadow_app_state(
        step, task_dir, device,
        package=TESTMALL_PACKAGE,
        state_path=TESTMALL_STATE_PATH,
        state_key="products",
        default_fixture="fixtures/gui/testmall_gui/products.json",
        app_label="TestMall",
    )


def inject_mattermost(step: dict, task_dir: Path, device: str | None) -> bool:
    """Inject Mattermost channel messages into the shadow app's state.json.

    task.yaml currently uses package ``com.mattermost.rnbeta`` — assumed to be
    a self-contained shadow build, not the upstream React Native beta which
    reads from a remote server and would ignore the pushed state.
    """
    return _inject_shadow_app_state(
        step, task_dir, device,
        package=MATTERMOST_PACKAGE,
        state_path=MATTERMOST_STATE_PATH,
        state_key="messages",
        default_fixture="fixtures/gui/mattermost_gui/messages.json",
        app_label="Mattermost",
    )


# ── Orchestrator ──────────────────────────────────────────────────────────────

_HANDLERS = {
    "email":              inject_email,
    "calendar":           inject_calendar,
    "fossify_calendar":   inject_calendar,
    "contacts":           inject_contacts,
    "dialer":             inject_dialer,
    "fossify_messages":   inject_fossify_messages,
    "opentracks":         inject_opentracks,
    "markor":             inject_markor,
    "loop_habits":        inject_loop_habits,
    "my_expenses":        inject_my_expenses,
    "clock":              inject_clock,
    "clock_alarm":        inject_clock,
    "fossify_notes":      inject_fossify_notes,
    "testmall":           inject_testmall,
    "mattermost":         inject_mattermost,
}


def init_gui_task(
    task_dir: str,
    device: str | None = None,
    adb_path: str | None = None,
    yaml_name: str = "task.yaml",
    screenshots_root: str | Path | None = None,
) -> bool:
    global _ADB
    _ADB = _resolve_adb_bin(adb_path)
    _cal._ADB_BIN = _ADB
    _con._ADB_BIN = _ADB

    task_path = Path(task_dir).resolve()
    yaml_path = task_path / yaml_name

    if not yaml_path.exists():
        print(f"[FAIL] {yaml_name} not found in {task_dir}", file=sys.stderr)
        return False

    try:
        import yaml
    except ImportError:
        print("[FAIL] pyyaml not installed — run: pip install pyyaml", file=sys.stderr)
        return False

    with open(yaml_path, encoding="utf-8") as f:
        task = yaml.safe_load(f)

    task_id      = task.get("task_id", task_path.name)
    inject_steps = task.get("inject", [])

    if not inject_steps:
        print(f"[{task_id}] No inject steps defined.")
        return True

    print(f"[{task_id}] Running {len(inject_steps)} inject step(s)...")
    all_ok = True
    screenshots_base_dir = Path(screenshots_root) if screenshots_root else (task_path / "screenshots")
    task_screenshots_dir = screenshots_base_dir / "_task"
    task_screenshots_dir.mkdir(parents=True, exist_ok=True)
    _capture_screenshot(task_id, task_screenshots_dir, "before_inject.png", device=device)

    for i, step in enumerate(inject_steps, 1):
        step_type = step.get("type", "unknown")
        desc      = step.get("description", "")
        print(f"\n  Step {i}/{len(inject_steps)}: [{step_type}] {desc}")
        step_screenshots_dir = screenshots_base_dir / _sanitize_screenshot_dir_name(step_type)
        step_screenshots_dir.mkdir(parents=True, exist_ok=True)
        _capture_screenshot(task_id, step_screenshots_dir, f"step_{i:04d}_before.png", device=device)
        handler = _HANDLERS.get(step_type)
        if handler is None:
            print(
                f"  [FAIL] Unknown inject type: {step_type!r}. "
                f"Register a handler in init_gui_task.py::_HANDLERS or fix the task.yaml.",
                file=sys.stderr,
            )
            _capture_screenshot(task_id, step_screenshots_dir, f"step_{i:04d}_after.png", device=device)
            all_ok = False
            continue
        step_ok = handler(step, task_path, device)
        all_ok &= step_ok
        _capture_screenshot(task_id, step_screenshots_dir, f"step_{i:04d}_after.png", device=device)

    status = "OK" if all_ok else "PARTIAL FAILURE"
    print(f"\n[{task_id}] Initialization {status}")

    _capture_screenshot(task_id, task_screenshots_dir, "after_inject.png", device=device)

    return all_ok


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description="Initialize GUI task by injecting fixture data into Android emulator"
    )
    parser.add_argument("task_dir", help="Path to task directory")
    parser.add_argument("-s", "--device", default=None, help="adb device serial")
    parser.add_argument("--adb", dest="adb_path", default=None, help="path to adb binary")
    parser.add_argument("--yaml", default="task.yaml", help="YAML filename (default: task.yaml)")
    args = parser.parse_args()
    ok = init_gui_task(args.task_dir, device=args.device, adb_path=args.adb_path, yaml_name=args.yaml)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
