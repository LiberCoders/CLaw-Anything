"""Fossify Calendar SQLite injection helpers.

Adapted from dailybench-gui/inject/inject_calendar.py.
Only the functions used by init_gui_task are kept; the task registry and CLI
entry point are intentionally omitted.

Set _ADB_BIN before calling any function to use a non-default adb binary.
"""

from __future__ import annotations

import os
import random
import subprocess
import sys
from datetime import datetime

_ADB_BIN = "adb"

CALENDAR_DB_PATH = "/data/user/0/org.fossify.calendar/databases/events.db"
CALENDAR_PACKAGE = "org.fossify.calendar"
CALENDAR_ACTIVITY = f"{CALENDAR_PACKAGE}/.activities.MainActivity"


def _adb_prefix(device: str | None = None) -> str:
    if device:
        return f"{_ADB_BIN} -s {device}"
    return _ADB_BIN


def execute_adb(cmd: str, device: str | None = None, root_required: bool = False) -> tuple[bool, str]:
    prefix = _adb_prefix(device)
    full_cmd = cmd if cmd.startswith("adb") else f"{prefix} {cmd}"
    env = os.environ.copy()
    if root_required:
        check = subprocess.run(
            f"{prefix} shell whoami", shell=True, capture_output=True, text=True, env=env
        )
        if check.returncode == 0 and check.stdout.strip() != "root":
            subprocess.run(f"{prefix} root", shell=True, capture_output=True, text=True, env=env)
    result = subprocess.run(full_cmd, shell=True, capture_output=True, text=True, env=env)
    if result.returncode == 0:
        return True, result.stdout.strip()
    return False, result.stderr.strip() or "Command failed"


def _ts(date_str: str) -> int:
    return int(datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S").timestamp())


def insert_event(
    device: str | None,
    title: str,
    start_time: str,
    end_time: str,
    location: str = "",
    description: str = "",
    reminder_1_minutes: int = -1,
    event_type: int = 1,
) -> bool:
    start_ts = _ts(start_time)
    end_ts = _ts(end_time)
    import_id = f"inject{random.randint(10000, 99999)}"
    last_updated = int(datetime.now().timestamp() * 1000)

    title_esc = title.replace("'", "''")
    location_esc = location.replace("'", "''")
    description_esc = description.replace("'", "''")

    insert_sql = (
        "INSERT INTO events ("
        "start_ts, end_ts, title, location, description, "
        "reminder_1_minutes, reminder_2_minutes, reminder_3_minutes, "
        "reminder_1_type, reminder_2_type, reminder_3_type, "
        "repeat_interval, repeat_rule, repeat_limit, "
        "repetition_exceptions, attendees, import_id, time_zone, "
        "flags, event_type, parent_id, last_updated, source, "
        "availability, access_level, color, type, status"
        ") VALUES ("
        f"{start_ts}, {end_ts}, '{title_esc}', '{location_esc}', '{description_esc}', "
        f"{reminder_1_minutes}, -1, -1, 0, 0, 0, 0, 0, 0, "
        f"'[]', '[]', '{import_id}', 'UTC', "
        f"0, {event_type}, 0, {last_updated}, 'inject_calendar', 0, 0, 0, 0, 1"
        ");"
    )

    cmd = f'shell "sqlite3 {CALENDAR_DB_PATH} \\"{insert_sql}\\""'
    ok, msg = execute_adb(cmd, device=device, root_required=True)
    if ok:
        print(f"  [OK] {title} ({start_time} ~ {end_time})")
    else:
        print(f"  [FAIL] {title}: {msg}", file=sys.stderr)
    return ok


def clear_calendar_events(device: str | None = None) -> bool:
    cmd = f'shell "sqlite3 {CALENDAR_DB_PATH} \\"DELETE FROM events;\\""'
    ok, msg = execute_adb(cmd, device=device, root_required=True)
    if ok:
        print("  [OK] Cleared all existing calendar events")
    else:
        print(f"  [FAIL] Failed to clear events: {msg}", file=sys.stderr)
    return ok


def restart_calendar_app(device: str | None = None) -> None:
    execute_adb(f"shell am force-stop {CALENDAR_PACKAGE}", device=device)
    execute_adb(f"shell am start -n {CALENDAR_ACTIVITY}", device=device)
    print("  [OK] Calendar app restarted")
