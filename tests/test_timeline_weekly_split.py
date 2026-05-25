"""Smoke tests for ISO-weekly timeline splitting.

Covers:
  * Engine helpers (``iso_week_bounds``, ``weekly_filename``).
  * ``ActivityLogEngine.rebuild_timeline()`` produces weekly files from a
    synthetic ``services/`` dir and deletes any pre-existing monolithic
    ``timeline.md``.
  * Migration script (``scripts/split_timeline.py``) round-trip: feeding a
    fake monolithic timeline yields weekly files whose concatenated rows
    equal the input.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime
from pathlib import Path

from claw_anything.gen.activity_log_engine import ActivityLogEngine, LogEntry


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_split_script():
    path = REPO_ROOT / "scripts" / "split_timeline.py"
    spec = importlib.util.spec_from_file_location("split_timeline", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["split_timeline"] = module
    spec.loader.exec_module(module)
    return module


# --- helpers -----------------------------------------------------------------


def test_iso_week_bounds_thursday_jan1():
    # 2026-01-01 is Thursday -> Monday of that ISO week is 2025-12-29
    mon, sun = ActivityLogEngine.iso_week_bounds(datetime(2026, 1, 1, 8, 3, 40))
    assert mon == date(2025, 12, 29)
    assert sun == date(2026, 1, 4)


def test_iso_week_bounds_monday_jan5():
    mon, sun = ActivityLogEngine.iso_week_bounds(datetime(2026, 1, 5, 0, 0, 0))
    assert mon == date(2026, 1, 5)
    assert sun == date(2026, 1, 11)


def test_weekly_filename_format():
    fn = ActivityLogEngine.weekly_filename(date(2026, 1, 1), date(2026, 1, 4))
    assert fn == "timeline_20260101_20260104.md"


# --- engine ------------------------------------------------------------------


def test_rebuild_timeline_creates_weekly_files(tmp_path: Path):
    logs_dir = tmp_path / "logs"
    services_dir = logs_dir / "services"
    services_dir.mkdir(parents=True)

    # seed a stale monolithic timeline.md to verify it gets deleted
    (logs_dir / "timeline.md").write_text("stale content\n", encoding="utf-8")
    # also seed a stale weekly file to verify rebuild clears it
    (logs_dir / "timeline_19990101_19990107.md").write_text("stale\n", encoding="utf-8")

    engine = ActivityLogEngine(noise_ratio=0.0, seed=0)
    entries = {
        "finance": [
            LogEntry(timestamp=datetime(2026, 1, 1, 8, 3, 40), service="finance", action="open_finance", duration_sec=3),
            LogEntry(timestamp=datetime(2026, 1, 4, 18, 0, 0), service="finance", action="close_finance", duration_sec=2),
        ],
        "notes": [
            LogEntry(timestamp=datetime(2026, 1, 5, 9, 0, 0), service="notes", action="open_notes", duration_sec=1),
            LogEntry(timestamp=datetime(2026, 1, 11, 22, 59, 0), service="notes", action="close_notes", duration_sec=1),
            LogEntry(timestamp=datetime(2026, 1, 12, 10, 0, 0), service="notes", action="open_notes", duration_sec=1),
        ],
    }
    engine.append_service_logs(logs_dir, entries)
    engine.rebuild_timeline(logs_dir)

    # stale files are gone
    assert not (logs_dir / "timeline.md").exists()
    assert not (logs_dir / "timeline_19990101_19990107.md").exists()

    # expected weekly buckets
    # Week 1 (ISO Mon 2025-12-29 .. Sun 2026-01-04), data Jan 1 - Jan 4
    expected_w1 = logs_dir / "timeline_20260101_20260104.md"
    # Week 2 (Mon 2026-01-05 .. Sun 2026-01-11), full week
    expected_w2 = logs_dir / "timeline_20260105_20260111.md"
    # Week 3 (Mon 2026-01-12 .. Sun 2026-01-18), only Jan 12
    expected_w3 = logs_dir / "timeline_20260112_20260112.md"

    assert expected_w1.exists()
    assert expected_w2.exists()
    assert expected_w3.exists()
    # no extras
    weekly = sorted(p.name for p in logs_dir.glob("timeline_*.md"))
    assert weekly == [expected_w1.name, expected_w2.name, expected_w3.name]

    w1 = expected_w1.read_text(encoding="utf-8")
    assert "2026-01-01 08:03:40" in w1 and "2026-01-04 18:00:00" in w1
    assert "2026-01-05" not in w1

    w2 = expected_w2.read_text(encoding="utf-8")
    assert "2026-01-05 09:00:00" in w2 and "2026-01-11 22:59:00" in w2


def test_rebuild_timeline_empty_services_noop(tmp_path: Path):
    logs_dir = tmp_path / "logs"
    (logs_dir / "services").mkdir(parents=True)
    (logs_dir / "timeline.md").write_text("stale\n", encoding="utf-8")

    engine = ActivityLogEngine()
    engine.rebuild_timeline(logs_dir)

    # stale file cleared even if no entries were found
    assert not (logs_dir / "timeline.md").exists()
    assert list(logs_dir.glob("timeline_*.md")) == []


# --- migration script --------------------------------------------------------


def test_migration_script_round_trip(tmp_path: Path):
    module = _load_split_script()

    # Build a fake fixture tree with one gen_tasks/*/logs/timeline.md
    task_logs = tmp_path / "gen_tasks" / "persona_X" / "T01_foo" / "logs"
    task_logs.mkdir(parents=True)

    rows = [
        "| 2026-01-01 08:03:40 | finance | open_finance | - | 3s |",
        "| 2026-01-04 18:00:00 | finance | close_finance | - | 2s |",
        "| 2026-01-05 09:00:00 | notes | open_notes | - | 1s |",
        "| 2026-01-11 22:59:00 | notes | close_notes | - | 1s |",
        "| 2026-01-12 10:00:00 | notes | open_notes | - | 1s |",
        "| BAD LINE SHOULD BE SKIPPED |",
    ]
    content = (
        "# Activity Timeline\n\n"
        "| Time | Service | Action | Reference | Duration |\n"
        "|------|---------|--------|-----------|----------|\n"
        + "\n".join(rows) + "\n"
    )
    timeline_path = task_logs / "timeline.md"
    timeline_path.write_text(content, encoding="utf-8")

    rc = module.main(["--root", str(tmp_path), "--subdirs", "gen_tasks"])
    assert rc == 0

    # original deleted
    assert not timeline_path.exists()

    produced = sorted(p.name for p in task_logs.glob("timeline_*.md"))
    assert produced == [
        "timeline_20260101_20260104.md",
        "timeline_20260105_20260111.md",
        "timeline_20260112_20260112.md",
    ]

    # concatenated rows equal the valid input rows (preserving order)
    concatenated = []
    for name in produced:
        for line in (task_logs / name).read_text(encoding="utf-8").splitlines():
            if line.startswith("| 2026-"):
                concatenated.append(line)
    valid_input_rows = [r for r in rows if r.startswith("| 2026-")]
    assert concatenated == valid_input_rows


def test_migration_script_dry_run_is_noop(tmp_path: Path):
    module = _load_split_script()

    task_logs = tmp_path / "gold_envs" / "persona_Y" / "logs"
    task_logs.mkdir(parents=True)
    content = (
        "# Activity Timeline\n\n"
        "| Time | Service | Action | Reference | Duration |\n"
        "|------|---------|--------|-----------|----------|\n"
        "| 2026-01-01 08:03:40 | finance | open_finance | - | 3s |\n"
    )
    tl = task_logs / "timeline.md"
    tl.write_text(content, encoding="utf-8")

    rc = module.main(["--root", str(tmp_path), "--subdirs", "gold_envs", "--dry-run"])
    assert rc == 0
    # original still there, no weekly files written
    assert tl.exists()
    assert list(task_logs.glob("timeline_*.md")) == []
