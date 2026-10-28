#!/usr/bin/env python3
"""
verify_table.py -- prove a table's columns actually store what you send them.

A dictionary that *looks* correct is not proof. This script writes one probe
row, reads it back, compares every value, then deletes the row.

    python verify_table.py u_toronto_event
    python verify_table.py u_toronto_event --keep      # leave the row behind

Why bother
----------
This is not ceremony. On a real instance, a `decimal` column declared with
max_length=15 stored 43.667712 as 43.67 -- the metadata was valid, the round
trip was lossy. Only a write/read comparison catches that class of bug.

Exit code 0 = every column round-tripped exactly.
Exit code 1 = at least one column drifted, truncated, or failed to write.
"""

from __future__ import annotations

import argparse
import random
import string
import sys
import uuid

from sn_api import SNError, ServiceNow

# A representative value per type. Chosen to expose precision loss and
# length truncation, not just "did it save anything".
SAMPLE_BY_TYPE = {
    "string": "verify_" + "".join(random.choices(string.ascii_lowercase, k=8)),
    "boolean": "true",
    "integer": "42",
    "longint": "1234567890123",
    "long": "1234567890123",
    "decimal": "43.667712",      # <-- the precision trap
    "float": "43.667712",        # <-- the precision trap
    "glide_date_time": "2026-11-02 19:00:00",
    "url": "https://example.com/probe?x=1",
    "email": "probe@example.com",
    "phone_number": "+1 416 555 0100",
}

# Fields the platform owns; never written by us.
SYSTEM_FIELDS = {
    "sys_id",
    "sys_created_on",
    "sys_created_by",
    "sys_updated_on",
    "sys_updated_by",
    "sys_mod_count",
    "sys_domain",
    "sys_domain_path",
    "sys_class_name",
}


def sample_for(col: dict) -> str | None:
    """Pick a probe value for a column, or None if we don't know its type."""
    ctype = str(col.get("type") or "")
    value = SAMPLE_BY_TYPE.get(ctype)
    if value is None:
        return None

    # For strings, probe the declared length to catch silent truncation.
    max_len = col.get("max_length")
    if ctype == "string" and max_len:
        try:
            width = int(max_len)
        except (TypeError, ValueError):
            return value
        if width <= len(value):
            return value[:width]
        return value + ("x" * (width - len(value) - 1))[: max(width - len(value), 0)]
    return value


def compare(ctype: str, sent: str, got) -> bool:
    """True if the value survived the round trip unchanged."""
    got_str = "" if got is None else str(got)

    if ctype in ("decimal", "float", "integer", "longint", "long"):
        try:
            # Exact comparison: any rounding is a failure, not a tolerance issue.
            return abs(float(got_str) - float(sent)) < 1e-9
        except (TypeError, ValueError):
            return False

    if ctype == "boolean":
        return got_str.lower() == str(sent).lower()

    return got_str == str(sent)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Round-trip verify a ServiceNow table's columns.")
    parser.add_argument("table", help="table name, e.g. u_toronto_event")
    parser.add_argument("--keep", action="store_true", help="leave the probe row in place")
    parser.add_argument(
        "--ignore",
        default="",
        help="comma-separated fields expected to round, e.g. currency columns "
        "that intentionally keep only 2 decimals: --ignore u_price_amount",
    )
    args = parser.parse_args(argv)

    ignore = {name.strip() for name in args.ignore.split(",") if name.strip()}

    sn = ServiceNow()
    table = args.table

    print(f"table : {table}")
    print(f"host  : {sn.instance}")
    print()

    if not sn.table_exists(table):
        print(f"ERROR: table {table} does not exist")
        return 1

    columns = [
        c for c in sn.columns(table)
        if c["element"] and c["element"] not in SYSTEM_FIELDS and c["element"] not in ignore
    ]
    if not columns:
        print("ERROR: table has no columns to verify")
        return 1

    print(f"[1/4] columns: {len(columns)}")
    if ignore:
        print(f"      ignoring (expected to round): {', '.join(sorted(ignore))}")

    # ---- build probe row -------------------------------------------------
    record: dict = {}
    skipped: list[str] = []
    for col in columns:
        value = sample_for(col)
        if value is None:
            skipped.append(f"{col['element']}({col['type']})")
            continue
        record[col["element"]] = value

    if skipped:
        print(f"      no probe value for: {', '.join(skipped)}")

    if not record:
        print("ERROR: nothing to write -- no known column types")
        return 1

    # ---- insert ----------------------------------------------------------
    print(f"[2/4] insert probe row ({len(record)} fields)")
    try:
        created = sn.insert(table, record)
    except SNError as exc:
        print(f"      INSERT FAILED: {exc}")
        return 1

    sys_id = created.get("sys_id")
    print(f"      sys_id: {sys_id}")

    # ---- read back and compare ------------------------------------------
    print("[3/4] read back and compare")
    try:
        fetched = sn.get(table, sys_id)
    except SNError as exc:
        print(f"      GET FAILED: {exc}")
        return 1

    ok = 0
    bad: list[str] = []
    for element, sent in record.items():
        ctype = next(c["type"] for c in columns if c["element"] == element)
        got = fetched.get(element)
        if compare(str(ctype), sent, got):
            ok += 1
        else:
            bad.append(f"{element:<24} {ctype:<16} sent={sent!r:<30} got={got!r}")

    for line in bad:
        print(f"      DRIFT  {line}")
    print(f"      {ok}/{len(record)} exact")

    # ---- cleanup ---------------------------------------------------------
    print("[4/4] cleanup")
    if args.keep:
        print(f"      kept row {sys_id} (--keep)")
    else:
        try:
            sn.delete(table, sys_id)
            print(f"      deleted row {sys_id}")
        except SNError as exc:
            print(f"      DELETE FAILED for {sys_id}: {exc} -- remove it manually")
            return 1

    print()
    if bad:
        print(f"RESULT: FAIL -- {len(bad)} column(s) did not round-trip")
        print("If a decimal/float lost precision, raise its max_length (see README).")
        return 1

    print(f"RESULT: PASS -- {ok}/{len(record)} columns verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
