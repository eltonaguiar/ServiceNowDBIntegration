#!/usr/bin/env python3
"""
fix_source_timezone.py -- correct u_start / u_end, which are 4-5 hours early.

    python fix_source_timezone.py --dry-run
    python fix_source_timezone.py --limit 200
    python fix_source_timezone.py

The bug
-------
`events.json` stamps Toronto LOCAL times with a `Z` (UTC) suffix:

    "title": "Coffee and Conversation",
    "date":  "2026-10-27T10:00:00Z",     <- Toronto Public Library lists 10:00am

11,756 of 11,758 source timestamps end in `Z`. The import took that at face
value and stored the literal wall-clock time as UTC, so every event sits
4 hours early (EDT) or 5 hours early (EST).

Symptoms this produced:
  - a 10:00 event rendered as 06:00 for a Toronto viewer and 03:00 for a UTC-7
    viewer -- the offset was the *viewer's*, not the event's
  - `gs.beginningOfToday()` resolving a day behind the instance clock, which
    made a "Today" card read 0 while a filter for that date found 66 events

The fix
-------
Interpret the stored value as America/Toronto local time and rewrite it as the
equivalent UTC instant. 10:00 EDT -> 14:00 UTC, which a Toronto session renders
back as 10:00.

This is DST-aware via zoneinfo, so a 10:00 event in January becomes 15:00 UTC
(EST, UTC-5) rather than 14:00. A flat "+4 hours" would corrupt winter events.

Write semantics (verified by probe, not assumed)
------------------------------------------------
A PATCH of "2026-10-27 14:00:00" reads back with `sysparm_display_value=false`
as exactly 14:00:00, i.e. the Table API stores the literal value as UTC for this
user. Display values, in contrast, are shifted by the *session* timezone
(measured: -7 in one session, -4 in another, with admin.time_zone empty). So we
write UTC, and never rely on display values for comparisons.

Safety
------
- writes a CSV backup of every original value before touching anything
- a marker file records that the migration ran; re-running needs --force,
  because applying it twice would shift everything another 4-5 hours
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

from sn_api import SNError, ServiceNow

EVENTS = "u_toronto_event"
DATETIME_FIELDS = ("u_start", "u_end")
SOURCE_TZ = "America/Toronto"
MARKER = Path(__file__).resolve().parent / ".migrations" / "source_timezone_v1.json"
BACKUP_DIR = Path(__file__).resolve().parent / "backups"


def load_zone():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(SOURCE_TZ)
    except Exception as exc:                     # missing tzdata
        print(f"!! cannot load {SOURCE_TZ}: {exc}")
        print("   On Debian/Ubuntu: sudo apt-get install tzdata")
        sys.exit(2)


def parse_sn(value: str) -> datetime | None:
    """ServiceNow returns 'YYYY-MM-DD HH:MM:SS'. Treat it as naive local."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


def to_utc(value: str, zone) -> str | None:
    """naive America/Toronto -> UTC string ServiceNow will store literally."""
    local = parse_sn(value)
    if local is None:
        return None
    return local.replace(tzinfo=zone).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def scan(sn: ServiceNow, limit: int = 0) -> List[dict]:
    fields = ["sys_id", *DATETIME_FIELDS]
    out: List[dict] = []
    offset = 0
    while True:
        page = sn.query(EVENTS, fields=fields, limit=2000, offset=offset)
        if not page:
            break
        out.extend(page)
        offset += 2000
        if limit and len(out) >= limit:
            return out[:limit]
        if len(page) < 2000:
            break
    return out


def backup(rows: List[dict]) -> Path:
    BACKUP_DIR.mkdir(exist_ok=True)
    path = BACKUP_DIR / f"u_toronto_event_datetimes_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["sys_id", *DATETIME_FIELDS])
        for r in rows:
            w.writerow([r["sys_id"], *(r.get(k) or "" for k in DATETIME_FIELDS)])
    return path


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--force", action="store_true",
                    help="run even though the marker says this already ran")
    args = ap.parse_args(argv)

    if MARKER.exists() and not args.force:
        print(f"This migration already ran: {MARKER}")
        print("Re-running would shift every timestamp another 4-5 hours.")
        print("Pass --force only if you truly intend that.")
        return 2

    zone = load_zone()
    sn = ServiceNow()

    print("[1/4] scanning")
    rows = scan(sn, args.limit)
    print(f"      {len(rows)} row(s)")
    if not rows:
        return 0

    print("[2/4] planning")
    plan: List[Tuple[str, Dict[str, str]]] = []
    for r in rows:
        patch: Dict[str, str] = {}
        for f in DATETIME_FIELDS:
            raw = r.get(f)
            if not raw:
                continue
            new = to_utc(raw, zone)
            if new and new != raw:
                patch[f] = new
        if patch:
            plan.append((r["sys_id"], patch))

    print(f"      {len(plan)} row(s) need shifting")
    for sid, patch in plan[:3]:
        detail = ", ".join(f"{k}: {next(r for r in rows if r['sys_id']==sid)[k]} -> {v}"
                           for k, v in patch.items())
        print(f"        {sid}  {detail}")

    if args.dry_run:
        print("\nDRY RUN -- nothing written")
        return 0
    if not plan:
        MARKER.parent.mkdir(exist_ok=True)
        MARKER.write_text(json.dumps({"applied_at": datetime.now().isoformat(),
                                      "rows": 0, "note": "nothing to do"}))
        print("nothing to update")
        return 0

    print("[3/4] backup")
    path = backup(rows)
    print(f"      {path}")

    print("[4/4] updating")
    done = failed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(sn.update, EVENTS, sid, patch): sid for sid, patch in plan}
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                fut.result()
                done += 1
            except SNError as exc:
                failed += 1
                if failed <= 5:
                    print(f"      x {futs[fut]}: {exc}")
            if i % 1000 == 0:
                print(f"      ... {i}/{len(plan)}")

    MARKER.parent.mkdir(exist_ok=True)
    MARKER.write_text(json.dumps({
        "applied_at": datetime.now().isoformat(),
        "source_tz": SOURCE_TZ,
        "rows_updated": done,
        "rows_failed": failed,
        "backup": str(path),
    }, indent=1))

    print(f"\nupdated {done}, failed {failed}")
    print(f"marker written to {MARKER}")

    print("\nverification sample (raw storage should now be local+offset):")
    for r in sn.query(EVENTS, encoded_query='u_title=Coffee and Conversation',
                      fields=["sys_id", *DATETIME_FIELDS], limit=2):
        print("  ", {k: r.get(k) for k in DATETIME_FIELDS})
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
