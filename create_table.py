#!/usr/bin/env python3
"""
create_table.py -- create a ServiceNow table and its columns from a JSON spec.

Idempotent: re-running creates only what is missing. Safe to run repeatedly.

    python create_table.py specs/u_toronto_event.json
    python create_table.py specs/u_toronto_event.json --dry-run

Spec format
-----------
{
  "table": "u_toronto_event",
  "label": "Toronto Event",
  "scope": "global",
  "extendable": false,
  "fields": [
    {"element": "u_title", "label": "Title", "type": "string", "max_length": 255},
    {"element": "u_lat",   "label": "Latitude", "type": "float", "max_length": 40},
    {"element": "u_is_free", "label": "Is Free", "type": "boolean"}
  ]
}

`max_length` is optional but see the README: for decimal/float it decides
stored precision, and omitting it can silently truncate values.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sn_api import SNError, ServiceNow


def load_spec(path: Path) -> dict:
    spec = json.loads(path.read_text(encoding="utf-8"))

    missing = [k for k in ("table", "label", "fields") if k not in spec]
    if missing:
        raise SystemExit(f"spec {path} is missing required keys: {missing}")

    if not isinstance(spec["fields"], list):
        raise SystemExit("spec 'fields' must be a list")

    seen = set()
    for field in spec["fields"]:
        for key in ("element", "label", "type"):
            if key not in field:
                raise SystemExit(f"field {field!r} is missing {key!r}")
        if field["element"] in seen:
            raise SystemExit(f"duplicate element {field['element']!r} in spec")
        seen.add(field["element"])

    return spec


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a ServiceNow table from a JSON spec.")
    parser.add_argument("spec", type=Path, help="path to the table spec JSON")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    args = parser.parse_args(argv)

    spec = load_spec(args.spec)
    table = spec["table"]

    print(f"spec      : {args.spec}")
    print(f"table     : {table}  ({spec['label']})")
    print(f"scope     : {spec.get('scope', 'global')}")
    print(f"fields    : {len(spec['fields'])}")
    print()

    if args.dry_run:
        print("DRY RUN -- no changes will be made\n")
        for field in spec["fields"]:
            print(
                f"  would create {field['element']:<24} {field['type']:<16} "
                f"{field.get('max_length') or '-':<6} {field['label']}"
            )
        return 0

    sn = ServiceNow()

    # 1. table
    print("[1/3] table")
    sn.create_table(
        table,
        spec["label"],
        scope=spec.get("scope", "global"),
        extendable=bool(spec.get("extendable", False)),
    )

    # 2. columns
    print("\n[2/3] columns")
    failures = []
    for field in spec["fields"]:
        try:
            sn.create_column(
                table,
                field["element"],
                field["label"],
                field["type"],
                max_length=field.get("max_length"),
            )
        except SNError as exc:
            failures.append((field["element"], str(exc)))
            print(f"  x {field['element']:<24} FAILED: {exc}")

    # 3. reconcile
    print("\n[3/3] reconcile")
    actual = {c["element"]: c for c in sn.columns(table) if c["element"].startswith("u_")}
    expected = {f["element"]: f for f in spec["fields"]}

    for element in sorted(expected):
        if element not in actual:
            print(f"  x MISSING  {element}")
        else:
            got = actual[element]
            want = expected[element]
            drift = []
            if str(got["type"]) != want["type"]:
                drift.append(f"type {got['type']} != {want['type']}")
            if want.get("max_length") and str(got["max_length"]) != str(want["max_length"]):
                drift.append(f"max_length {got['max_length']} != {want['max_length']}")
            if drift:
                print(f"  ! DRIFT    {element}: " + "; ".join(drift))
            else:
                print(f"  . ok       {element}")

    extras = sorted(set(actual) - set(expected))
    for element in extras:
        print(f"  ? EXTRA    {element} (not in spec)")

    print()
    if failures or set(expected) - set(actual):
        print("RESULT: incomplete -- see failures above")
        return 1

    print(f"RESULT: {len(expected)}/{len(expected)} fields present")
    print(f"Next: python verify_table.py {table}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
