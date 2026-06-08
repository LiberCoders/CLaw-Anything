"""End-to-end host-side GUI inject smoke test against a pool-launched container.

Pipeline mirrors what ``cli._run_one_trial`` does host-side for a mobile_gui
task, minus the agent:

  1. EmulatorPool starts one fresh claw_anything:latest container, bridges adb.
  2. ``init_gui_task`` injects the task's fixtures (TGUI01 = my_expenses) into
     the emulator over the handed-out ``localhost:<port>`` serial.
  3. We re-pull the My Expenses SQLite DB and assert the injected transactions
     actually landed (host adb can both push state AND read it back).
  4. stop_all tears the pool down.

Run on 10.108.13.53:
  PATH=$PWD/../platform-tools:$PATH .venv/bin/python scripts/_gui_inject_smoke.py [TASK_DIR]
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from claw_anything.runner.emulator_pool import EmulatorPool
from claw_anything.task.mobile_gui import init_gui_task, resolve_adb_bin

DEFAULT_TASK = "benchmark/gui/TGUI01_myexpenses_overbudget_finance_email"
MY_EXP_DB = "/data/data/org.totschnig.myexpenses/databases/data"


def _adb(adb: str, serial: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([adb, "-s", serial, *args], capture_output=True, text=True)


def _probe_root(adb: str, serial: str) -> str:
    r = _adb(adb, serial, "shell", "whoami")
    return (r.stdout + r.stderr).strip()


def _count_my_expenses_tx(adb: str, serial: str) -> int:
    """Pull the My Expenses DB and count rows in transactions."""
    import sqlite3

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        local = tmp.name
    r = _adb(adb, serial, "pull", MY_EXP_DB, local)
    if r.returncode != 0:
        print(f"[inject-smoke] pull DB failed: {(r.stdout + r.stderr).strip()}")
        return -1
    try:
        conn = sqlite3.connect(local)
        n = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        conn.close()
        return int(n)
    finally:
        Path(local).unlink(missing_ok=True)


def main() -> int:
    task_dir = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TASK
    adb = resolve_adb_bin(None)
    print(f"[inject-smoke] adb={adb} task={task_dir}")

    pool = EmulatorPool(image="claw_anything:latest", size=1, boot_timeout_s=420)
    serial = pool.start_all()[0]
    print(f"[inject-smoke] serial={serial}")

    try:
        who = _probe_root(adb, serial)
        print(f"[inject-smoke] whoami over bridge: {who!r}")

        before = _count_my_expenses_tx(adb, serial)
        print(f"[inject-smoke] my_expenses tx BEFORE inject: {before}")

        with tempfile.TemporaryDirectory() as shots:
            ok = init_gui_task(task_dir, device=serial, screenshots_root=shots)
        print(f"[inject-smoke] init_gui_task returned: {ok}")

        # Re-connect in case `adb root` (if it ran) bounced the bridge.
        subprocess.run([adb, "connect", serial], capture_output=True, text=True)
        after = _count_my_expenses_tx(adb, serial)
        print(f"[inject-smoke] my_expenses tx AFTER inject: {after}")

        assert ok, "init_gui_task reported failure"
        assert after > 0, f"no transactions after inject (after={after})"
        print("[inject-smoke] PASS")
        return 0
    finally:
        pool.stop_all()


if __name__ == "__main__":
    sys.exit(main())
