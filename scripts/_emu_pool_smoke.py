"""Smoke test for EmulatorPool against claw_anything:latest on this host.

Launches one fresh emulator container through the pool, verifies the host's
adb can drive the handed-out serial (boot_completed=1 + package list), then
tears the pool down and asserts no labelled container leaks.

Run on 10.108.13.53:  .venv/bin/python scripts/_emu_pool_smoke.py
"""
from __future__ import annotations

import subprocess
import sys

from claw_anything.runner.emulator_pool import EMU_CONTAINER_LABEL, EmulatorPool
from claw_anything.task.mobile_gui import resolve_adb_bin


def _labelled_containers() -> list[str]:
    res = subprocess.run(
        ["docker", "ps", "-q", "--filter", f"label={EMU_CONTAINER_LABEL}"],
        capture_output=True, text=True,
    )
    return res.stdout.split()


def main() -> int:
    adb = resolve_adb_bin(None)
    print(f"[smoke] adb binary: {adb}")

    pool = EmulatorPool(
        image="claw_anything:latest",
        size=1,
        boot_timeout_s=420,
    )
    serials = pool.start_all()
    print(f"[smoke] pool serials: {serials}")
    assert len(serials) == 1, serials
    serial = serials[0]

    try:
        boot = subprocess.run(
            [adb, "-s", serial, "shell", "getprop", "sys.boot_completed"],
            capture_output=True, text=True,
        )
        print(f"[smoke] boot_completed={boot.stdout.strip()!r} rc={boot.returncode}")
        assert boot.stdout.strip() == "1", boot.stdout + boot.stderr

        pkgs = subprocess.run(
            [adb, "-s", serial, "shell", "pm", "list", "packages"],
            capture_output=True, text=True,
        )
        lines = [l for l in pkgs.stdout.splitlines() if l.startswith("package:")]
        print(f"[smoke] package count: {len(lines)}")
        assert len(lines) > 50, f"too few packages: {len(lines)}"

        wanted = ["org.fossify.calendar", "org.totschnig.myexpenses", "com.gmailclone"]
        present = [w for w in wanted if any(w in l for l in lines)]
        print(f"[smoke] inject targets present: {present}")
        assert present == wanted, f"missing inject targets: {set(wanted) - set(present)}"
        print("[smoke] DRIVE OK")
    finally:
        pool.stop_all()

    leaked = _labelled_containers()
    print(f"[smoke] leaked containers after stop_all: {leaked}")
    assert not leaked, leaked
    print("[smoke] PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
