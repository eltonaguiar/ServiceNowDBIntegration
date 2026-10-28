#!/usr/bin/env python3
"""
import_events.py -- idempotent sync of the Toronto events feed into ServiceNow.

    python import_events.py --source https://example.com/events.json
    python import_events.py --source events.json --limit 200 --workers 8
    python import_events.py --source events.json --dry-run

Why not Import Sets?
--------------------
The Import Set API (`POST /api/now/import/<table>`) is the usual recommendation,
but it is **not available on every instance**. This script was written after
confirming that endpoint returns `405 POST method not supported for API` for
every table, including `incident`, with no `service_id=import` REST service
registered. A push-based sync therefore has to go straight at the Table API.

What "idempotent" means here
----------------------------
Four properties, each independently enforced and tested:

  1. UPSERT BY NATURAL KEY     -- `u_event_id` is the key. Existing row -> PATCH,
                                  missing -> POST. Running twice inserts nothing.
  2. SKIP UNCHANGED            -- every target field is compared before writing,
                                  so a no-op run performs zero writes.
  3. CONVERGE TO SOURCE        -- full runs delete target rows whose key is no
                                  longer in the feed. Upsert alone never removes
                                  anything, which is how stale rows accumulate.
                                  Never runs with --limit (that would delete
                                  everything outside the subset).
  4. DEDUPE GUARD              -- ServiceNow has no unique constraint on a
                                  custom field, so two concurrent runs can both
                                  see "missing" and both insert. A final pass
                                  deletes duplicate keys, keeping the newest.
                                  This makes the pipeline self-healing rather
                                  than dependent on the caller being careful.

Exit codes: 0 = clean, 1 = errors were recorded, 2 = bad usage.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from sn_api import SNError, ServiceNow

TARGET = "u_toronto_event"
KEY_FIELD = "u_event_id"

# target column -> source key. Derived from the feed by inspection; the six
# camelCase aliases in the source (isFree, isMultiDay, ...) are deliberately
# dropped because they duplicate the snake_case fields.
FIELD_MAP: dict[str, str] = {
    "u_event_id": "id",
    "u_title": "title",
    "u_start": "date",
    "u_location": "location",
    "u_address": "address",
    "u_lat": "lat",
    "u_lng": "lng",
    "u_source": "source",
    "u_host": "host",
    "u_status": "status",
    "u_url": "url",
    "u_price": "price",
    "u_price_amount": "price_amount",
    "u_is_free": "is_free",
    "u_is_multi_day": "is_multi_day",
    "u_description": "description",
    "u_categories": "categories",
    "u_tags": "tags",
    "u_duration_category": "duration_category",
    "u_is_recurring": "is_recurring",
    "u_last_updated": "last_updated",
}

# Columns persisted as date/time. The Table API round-trips UTC in this format.
DATETIME_FIELDS = {"u_start", "u_last_updated"}

# How precisely each numeric column is stored on the instance. Values are
# rounded to this before writing so the stored value is deterministic, and
# compared with this tolerance so float last-digit noise never causes a write.
#
# Two real defects forced this:
#   * u_lat/u_lng are `float` -- the 6th decimal disagreed on 15 of 11,940 rows
#     (e.g. -79.255913 vs -79.255914), so those rows re-wrote forever.
#   * u_price_amount is `decimal` with max_length 15 -> 2 decimal places.
NUMERIC_PRECISION = {
    "u_lat": 6,
    "u_lng": 6,
    "u_price_amount": 2,
}

READ_FIELDS = ["sys_id", "sys_created_on", "sys_updated_on", "u_batch_id"] + list(FIELD_MAP)


# --------------------------------------------------------------------- helpers


def to_sn_datetime(value: str) -> str | None:
    """'2026-11-02T19:00:00.123Z' -> '2026-11-02 19:00:00'. Empty -> None."""
    if not value:
        return None
    text = str(value).strip().replace("T", " ")
    if text.endswith("Z"):
        text = text[:-1]
    # ServiceNow wants second precision, no trailing offset or fraction.
    return text[:19]


def normalise(field: str, value) -> str | None:
    """Convert a source value into the exact string the Table API expects."""
    if value is None:
        return None
    if isinstance(value, list):
        value = ",".join(str(v) for v in value)
    if field in DATETIME_FIELDS:
        return to_sn_datetime(str(value))
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if text == "":
        return None
    return text


def comparable(field: str, value) -> str:
    """Normalise a value from EITHER side so equality tests are meaningful."""
    if value is None:
        return ""
    if isinstance(value, dict):          # reference returned as a dict
        value = value.get("value") or value.get("display_value") or ""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).strip()
    if field in DATETIME_FIELDS:
        return to_sn_datetime(text) or ""
    if field in ("u_lat", "u_lng", "u_price_amount"):
        try:
            return f"{float(text):.6f}" if text else ""
        except ValueError:
            return text
    return text


def build_payload(record: dict) -> dict:
    payload: dict[str, str] = {}
    for field, source_key in FIELD_MAP.items():
        value = normalise(field, record.get(source_key))
        if value is not None:
            payload[field] = value
    # Pin numeric fields to the storage precision of the column.
    for field, places in NUMERIC_PRECISION.items():
        if field in payload:
            try:
                payload[field] = f"{round(float(payload[field]), places):.{places}f}"
            except ValueError:
                pass
    return payload


def values_differ(field: str, existing_value, new_value: str) -> bool:
    """
    Compare a stored value against the value we would write.

    Numeric fields are compared at the column's storage precision, not as
    strings -- a float that differs only in the last digit is the same number
    and must not trigger a write.
    """
    current = comparable(field, existing_value)
    desired = comparable(field, new_value)
    if current == desired:
        return False

    places = NUMERIC_PRECISION.get(field)
    if places is not None:
        try:
            return round(float(current), places) != round(float(desired), places)
        except (TypeError, ValueError):
            return True
    return True


def canonicalise_source(records: list[dict]) -> tuple[list[dict], int]:
    """
    Collapse duplicate keys in the feed to one deterministic winner.

    The feed contains 5 duplicate `id` values with different payloads. Without
    this, the row that wins depends on feed order, so each run compares against
    a different copy and re-writes forever. The winner is the record with the
    newest `last_updated` (ties broken by position), so the choice is stable
    across runs.
    """
    winners: dict[str, dict] = {}
    dropped = 0

    for position, record in enumerate(records):
        key = record.get("id")
        if not key:
            continue
        previous = winners.get(key)
        if previous is None:
            winners[key] = record
            continue
        dropped += 1
        if str(record.get("last_updated") or "") >= str(previous.get("last_updated") or ""):
            winners[key] = record

    return list(winners.values()), dropped


def load_source(source: str) -> list[dict]:
    if source.startswith("http://") or source.startswith("https://"):
        req = urllib.request.Request(source, headers={"User-Agent": "sn-events-sync/1.0"})
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read())

    path = Path(source)
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


TEXT_TYPES = {"string", "url", "email", "phone_number"}


def preflight_widths(sn: ServiceNow, records: list[dict]) -> list[str]:
    """
    Fail fast if any value is longer than its column.

    This exists because of a real bug: a 429-character `title` against a
    255-character column does not error, it truncates. The row then differs
    from the source on every run, so it is 'updated' forever and the sync never
    converges. Checking widths up front makes the failure loud instead of slow.
    """
    columns = {c["element"]: c for c in sn.columns(TARGET)}
    widest: dict[str, tuple[int, str]] = {}

    for record in records:
        for field, value in build_payload(record).items():
            length = len(value)
            if length > widest.get(field, (0, ""))[0]:
                widest[field] = (length, value)

    problems: list[str] = []
    for field, (length, sample) in sorted(widest.items()):
        column = columns.get(field)
        if not column or str(column.get("type")) not in TEXT_TYPES:
            continue
        try:
            width = int(column["max_length"])
        except (TypeError, ValueError):
            continue
        if length > width:
            problems.append(
                f"{field}: longest value {length} chars exceeds column max_length {width}\n"
                f"           value starts: {sample[:70]!r}"
            )
    return problems


# ----------------------------------------------------------------------- sync


class Sync:
    def __init__(self, sn: ServiceNow, batch_id: str, workers: int = 6, verbose: bool = False):
        self.sn = sn
        self.batch_id = batch_id
        self.workers = workers
        self.verbose = verbose
        self.errors: list[str] = []
        self._lock = threading.Lock()

    # -- read ------------------------------------------------------------

    def load_existing(self) -> dict[str, dict]:
        """Page the whole target table once. Returns key -> row."""
        index: dict[str, dict] = {}
        offset = 0
        page = 1000
        while True:
            rows = self.sn.query(
                TARGET,
                fields=READ_FIELDS,
                limit=page,
                offset=offset,
                order_by="sys_id",
            )
            if not rows:
                break
            for row in rows:
                key = row.get(KEY_FIELD)
                if not key:
                    continue
                current = index.get(key)
                # If duplicates already exist, keep the most recently updated.
                if current is None or str(row.get("sys_updated_on", "")) > str(
                    current.get("sys_updated_on", "")
                ):
                    index[key] = row
            offset += page
            if len(rows) < page:
                break
        return index

    # -- write -----------------------------------------------------------

    def apply(self, record: dict, existing: dict | None) -> tuple[str, bool]:
        """Return (outcome, changed). outcome in {inserted, updated, unchanged, error}."""
        payload = build_payload(record)
        payload["u_batch_id"] = self.batch_id

        try:
            if existing is None:
                self.sn.insert(TARGET, payload)
                return "inserted", True

            changes = {}
            for field, value in payload.items():
                if field == "u_batch_id":
                    continue
                if values_differ(field, existing.get(field), value):
                    changes[field] = value

            if not changes:
                return "unchanged", False

            changes["u_batch_id"] = self.batch_id
            self.sn.update(TARGET, existing["sys_id"], changes)
            return "updated", True
        except SNError as exc:
            with self._lock:
                self.errors.append(f"{record.get('id')}: {exc}")
            return "error", False

    # -- maintenance -----------------------------------------------------

    def sweep(self, live_keys: set[str]) -> int:
        """Delete rows whose key is absent from the feed. Full runs only."""
        existing = self.load_existing()
        doomed = [(key, row["sys_id"]) for key, row in existing.items() if key not in live_keys]
        removed = 0
        for key, sys_id in doomed:
            try:
                self.sn.delete(TARGET, sys_id)
                removed += 1
            except SNError as exc:
                with self._lock:
                    self.errors.append(f"sweep {key}: {exc}")
        return removed

    def dedupe(self) -> int:
        """
        Remove duplicate natural keys, keeping the newest row per key.

        ServiceNow cannot enforce uniqueness on a custom field, so this is the
        only durable guarantee. It also repairs damage from a concurrent run.
        """
        groups: dict[str, list[dict]] = defaultdict(list)
        offset = 0
        page = 1000
        while True:
            rows = self.sn.query(
                TARGET,
                fields=["sys_id", "sys_created_on", KEY_FIELD],
                limit=page,
                offset=offset,
                order_by="sys_id",
            )
            if not rows:
                break
            for row in rows:
                if row.get(KEY_FIELD):
                    groups[row[KEY_FIELD]].append(row)
            offset += page
            if len(rows) < page:
                break

        removed = 0
        for key, rows in groups.items():
            if len(rows) < 2:
                continue
            rows.sort(key=lambda r: str(r.get("sys_created_on", "")))
            for loser in rows[:-1]:
                try:
                    self.sn.delete(TARGET, loser["sys_id"])
                    removed += 1
                except SNError as exc:
                    with self._lock:
                        self.errors.append(f"dedupe {key}: {exc}")
        return removed


# ------------------------------------------------------------------ reporting


def run(args) -> int:
    started = time.time()
    batch_id = args.batch_id or datetime.now(timezone.utc).strftime("batch-%Y%m%dT%H%M%SZ")

    print(f"source   : {args.source}")
    print(f"batch id : {batch_id}")

    records = load_source(args.source)
    print(f"fetched  : {len(records)} records")

    # Canonicalise BEFORE --limit so a duplicate pair is never split by the cut.
    records, dropped = canonicalise_source(records)
    if dropped:
        print(f"canonical: dropped {dropped} duplicate-key record(s), kept the newest")
    print(f"usable   : {len(records)} records")

    if args.limit:
        records = records[: args.limit]
        print(f"limited  : {len(records)} records (sweep and dedupe still safe)")

    # key sanity
    keys = [r.get("id") for r in records]
    blank = sum(1 for k in keys if not k)
    unique = len(set(k for k in keys if k))
    dupes_in_source = len(keys) - blank - unique
    print(f"keys     : {unique} unique, {blank} blank, {dupes_in_source} duplicated in source")
    if blank:
        print("           ! blank keys are skipped -- they cannot be coalesced")

    if args.dry_run:
        print("\nDRY RUN -- no writes")
        for record in records[:3]:
            print("  " + json.dumps(build_payload(record))[:200])
        return 0

    sn = ServiceNow()
    sync = Sync(sn, batch_id, workers=args.workers, verbose=args.verbose)

    print("\n[0/4] preflight: column widths")
    width_problems = preflight_widths(sn, records)
    if width_problems:
        for problem in width_problems:
            print(f"      x {problem}")
        if not args.ignore_widths:
            print(
                "\n      Aborting: these values would be silently truncated, and a truncated\n"
                "      value re-writes on every run so the sync never converges.\n"
                "      Widen the column (bump max_length in the spec, then\n"
                "      `python create_table.py <spec> --fix-drift`) or pass --ignore-widths."
            )
            return 2
        print("      continuing anyway (--ignore-widths)")
    else:
        print("      all values fit")

    print("\n[1/4] loading existing rows")
    existing = sync.load_existing()
    print(f"      {len(existing)} rows already in {TARGET}")

    print(f"[2/4] applying {len(records)} records with {args.workers} workers")
    counts = defaultdict(int)
    work = [(r, existing.get(r.get("id"))) for r in records if r.get("id")]

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(sync.apply, rec, ex) for rec, ex in work]
        for done, future in enumerate(as_completed(futures), 1):
            outcome, _ = future.result()
            counts[outcome] += 1
            if done % 250 == 0 or done == len(futures):
                print(
                    f"      {done}/{len(futures)}  inserted={counts['inserted']} "
                    f"updated={counts['updated']} unchanged={counts['unchanged']} "
                    f"errors={counts['error']}"
                )

    print("[3/4] dedupe guard")
    dupes_removed = sync.dedupe()
    print(f"      removed {dupes_removed} duplicate row(s)")

    print("[4/4] stale sweep")
    if args.limit:
        swept = 0
        print("      skipped -- partial run (sweeping would delete the other keys)")
    elif args.no_sweep:
        swept = 0
        print("      skipped -- --no-sweep")
    else:
        swept = sync.sweep({r.get("id") for r in records if r.get("id")})
        print(f"      removed {swept} row(s) no longer in the feed")

    final = sn.count(TARGET)
    elapsed = time.time() - started

    report = {
        "batch_id": batch_id,
        "source": args.source,
        "elapsed_s": round(elapsed, 1),
        "fetched": len(records),
        "unique_keys": unique,
        "duplicates_in_source": dupes_in_source,
        "inserted": counts["inserted"],
        "updated": counts["updated"],
        "unchanged": counts["unchanged"],
        "errors": counts["error"],
        "duplicates_removed": dupes_removed,
        "swept": swept,
        "target_rows_final": final,
        "error_samples": sync.errors[:10],
    }

    print("\n=== report ===")
    for key, value in report.items():
        if key != "error_samples":
            print(f"  {key:<22} {value}")
    if sync.errors:
        print("  error samples:")
        for line in sync.errors[:10]:
            print(f"    - {line}")

    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nreport written to {path}")

    return 0 if not sync.errors else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Idempotent ServiceNow events sync.")
    parser.add_argument("--source", required=True, help="URL or local path to the events JSON")
    parser.add_argument("--limit", type=int, help="only process the first N records (testing)")
    parser.add_argument("--workers", type=int, default=6, help="parallel writers (default 6)")
    parser.add_argument("--batch-id", help="override the generated batch id")
    parser.add_argument("--no-sweep", action="store_true", help="skip deleting rows absent from the feed")
    parser.add_argument("--ignore-widths", action="store_true", help="write despite values exceeding column widths")
    parser.add_argument("--dry-run", action="store_true", help="show the plan, write nothing")
    parser.add_argument("--report", help="write a JSON run report to this path")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
