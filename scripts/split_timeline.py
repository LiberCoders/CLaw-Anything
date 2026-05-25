"""Migrate monolithic ``logs/timeline.md`` fixtures into ISO-weekly files.

Splits each ``timeline.md`` into ``timeline_YYYYMMDD_YYYYMMDD.md`` files — one
per ISO week (Monday–Sunday) — and deletes the original. Filenames use the
actual first/last row date within each week, so the leading/trailing partial
weeks are named by their clipped range (e.g. ``timeline_20260101_20260104.md``
for a first week starting Thursday).

Default scope matches the Claw-Anything fixture layout:
    - ``gold_envs/**/logs/timeline.md``
    - ``gen_tasks/**/logs/timeline.md``
    - ``benchmark/**/logs/timeline.md``

Run from the ``claw-anything/`` directory::

    python scripts/split_timeline.py --dry-run        # preview only
    python scripts/split_timeline.py                  # apply migration
    python scripts/split_timeline.py --root ../foo    # custom repo root
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

ROW_RE = re.compile(
    r"^\|\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*\|"
    r"\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*$"
)
TS_FMT = "%Y-%m-%d %H:%M:%S"

HEADER = "# Activity Timeline\n\n"
TABLE_HEAD = "| Time | Service | Action | Reference | Duration |\n"
TABLE_SEP = "|------|---------|--------|-----------|----------|\n"

DEFAULT_SUBDIRS = ("gold_envs", "gen_tasks", "benchmark")


@dataclass
class Row:
    ts: datetime
    raw: str


def iso_week_bounds(dt: datetime | date) -> tuple[date, date]:
    """Monday and Sunday of the ISO week containing ``dt``."""
    d = dt.date() if isinstance(dt, datetime) else dt
    monday = d - timedelta(days=d.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def weekly_filename(start: date, end: date) -> str:
    return f"timeline_{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}.md"


def parse_timeline(path: Path) -> tuple[list[Row], int]:
    """Return (parsed rows, skipped row count)."""
    rows: list[Row] = []
    skipped = 0
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        if set(stripped) <= {"|", "-", " "}:
            continue
        if "| Time |" in stripped:
            continue
        m = ROW_RE.match(stripped)
        if not m:
            skipped += 1
            continue
        try:
            ts = datetime.strptime(m.group(1), TS_FMT)
        except ValueError:
            skipped += 1
            continue
        rows.append(Row(ts=ts, raw=stripped))
    rows.sort(key=lambda r: r.ts)
    return rows, skipped


def bucket_by_week(rows: list[Row]) -> dict[date, list[Row]]:
    buckets: dict[date, list[Row]] = {}
    for r in rows:
        monday, _ = iso_week_bounds(r.ts)
        buckets.setdefault(monday, []).append(r)
    return buckets


def render_bucket(rows: list[Row]) -> str:
    body = "".join(r.raw + "\n" for r in rows)
    return HEADER + TABLE_HEAD + TABLE_SEP + body


def split_one(path: Path, dry_run: bool) -> tuple[int, int, int]:
    """Return (rows_migrated, rows_skipped, files_written)."""
    rows, skipped = parse_timeline(path)
    if not rows:
        return 0, skipped, 0
    buckets = bucket_by_week(rows)
    logs_dir = path.parent
    written = 0
    for monday in sorted(buckets):
        week_rows = buckets[monday]
        start = week_rows[0].ts.date()
        end = week_rows[-1].ts.date()
        out_path = logs_dir / weekly_filename(start, end)
        if dry_run:
            written += 1
            continue
        out_path.write_text(render_bucket(week_rows), encoding="utf-8")
        written += 1
    if not dry_run:
        path.unlink()
    return len(rows), skipped, written


def discover(root: Path, subdirs: tuple[str, ...]) -> list[Path]:
    out: list[Path] = []
    for sub in subdirs:
        base = root / sub
        if not base.is_dir():
            continue
        out.extend(sorted(base.rglob("logs/timeline.md")))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parent.parent),
        help="Claw-Anything repo root (directory containing gold_envs/, gen_tasks/, benchmark/).",
    )
    ap.add_argument(
        "--subdirs",
        nargs="+",
        default=list(DEFAULT_SUBDIRS),
        help=f"Which subdirectories to scan. Default: {' '.join(DEFAULT_SUBDIRS)}.",
    )
    ap.add_argument("--dry-run", action="store_true", help="Report only, no file changes.")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"error: --root {root} is not a directory", file=sys.stderr)
        return 2

    targets = discover(root, tuple(args.subdirs))
    if not targets:
        print(f"no timeline.md found under {root}/{{{', '.join(args.subdirs)}}}")
        return 0

    print(f"{'[dry-run] ' if args.dry_run else ''}processing {len(targets)} timeline.md files under {root}")
    total_rows = 0
    total_skipped = 0
    total_files = 0
    empty_sources = 0
    for path in targets:
        migrated, skipped, written = split_one(path, dry_run=args.dry_run)
        total_rows += migrated
        total_skipped += skipped
        total_files += written
        if migrated == 0 and written == 0:
            empty_sources += 1

    verb = "would migrate" if args.dry_run else "migrated"
    print(f"  sources scanned: {len(targets)}")
    print(f"  sources empty (skipped, no rows): {empty_sources}")
    print(f"  rows {verb}: {total_rows}")
    print(f"  rows unparseable (skipped): {total_skipped}")
    print(f"  weekly files {'planned' if args.dry_run else 'written'}: {total_files}")
    if not args.dry_run:
        print(f"  original timeline.md deleted: {len(targets) - empty_sources}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
