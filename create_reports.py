#!/usr/bin/env python3
"""
create_reports.py -- create a set of read-only reports from a JSON spec.

    python create_reports.py specs/reports_toronto_events.json --dry-run
    python create_reports.py specs/reports_toronto_events.json

Idempotent: a report whose title already exists on that table is skipped, so
re-running adds only what is new and never duplicates.

Why reports and not a dashboard object
-------------------------------------
Reports are ordinary records in `sys_report` and are fully creatable over REST
(verified: create -> read back -> delete). The *modern dashboard container* is a
different story: `par_dashboard` has nine columns, five of which reference UI
Framework artifacts:

    sys_ux_screen, sys_ux_screen_type, sys_ux_app_route,
    sys_ux_macroponent, sys_ux_app_config

A dashboard is therefore a UI Builder artefact, not a row you can meaningfully
INSERT. Hand-building one over REST means fabricating a screen, a screen type, an
app route and a macroponent, each carrying JSON config — brittle, unsupported,
and it will break on upgrade.

So the split this script encodes is the supported one:

    reports   ->  scripted, version-controlled, re-runnable   (this file)
    dashboard ->  assembled once in the UI, 2 minutes, then it
                  picks up every report you scripted

See docs/DASHBOARDS.md for the reasoning and the exact UI steps.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sn_api import SNError, ServiceNow

VALID_TYPES = {
    "single_score", "bar", "horizontal_bar", "pie", "donut", "line", "spline",
    "area", "step_line", "trend", "pivot", "pivot_v2", "list", "map", "heatmap",
    "calendar", "funnel", "pareto", "box", "bubble", "hist", "angular_gauge",
    "solid_gauge", "semi_donut", "pyramid", "tbox", "line_bar", "availability",
}


def existing_titles(sn: ServiceNow, table: str) -> dict[str, str]:
    """title -> sys_id for reports already on this table."""
    out: dict[str, str] = {}
    offset = 0
    while True:
        rows = sn.query(
            "sys_report",
            encoded_query=f"table={table}^ORDERBYtitle",
            fields=["sys_id", "title"],
            limit=1000,
            offset=offset,
        )
        if not rows:
            break
        for row in rows:
            if row.get("title"):
                out[row["title"]] = row["sys_id"]
        offset += 1000
        if len(rows) < 1000:
            break
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create ServiceNow reports from a JSON spec.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="show the plan, create nothing")
    parser.add_argument("--delete", action="store_true",
                        help="remove every report defined in the spec (cleanup)")
    parser.add_argument("--update", action="store_true",
                        help="patch existing reports whose filter/type/field differ from the spec")
    args = parser.parse_args(argv)

    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    table = spec["table"]
    reports = spec["reports"]

    print(f"spec   : {args.spec}")
    print(f"table  : {table}")
    print(f"reports: {len(reports)}")
    print()

    # Warn early rather than letting the platform reject an unknown chart type.
    bad = [r["title"] for r in reports if r.get("type") not in VALID_TYPES]
    if bad:
        print(f"WARNING: unrecognised chart type in: {', '.join(bad)}")
        print()

    if args.dry_run:
        print("DRY RUN -- nothing will be created\n")
        for r in reports:
            print(f"  would create [{r.get('type'):<14}] {r['title']}")
            if r.get("filter"):
                print(f"                    filter: {r['filter']}")
        return 0

    sn = ServiceNow()
    have = existing_titles(sn, table)

    if args.delete:
        removed = 0
        for r in reports:
            sys_id = have.get(r["title"])
            if not sys_id:
                continue
            sn.delete("sys_report", sys_id)
            removed += 1
            print(f"  - deleted {r['title']}")
        print(f"\nremoved {removed} report(s)")
        return 0

    created = skipped = updated = failed = 0
    for r in reports:
        title = r["title"]
        if title in have:
            if not args.update:
                print(f"  = skip    {title} (already exists)")
                skipped += 1
                continue

            # --update: make the instance match the spec. Only the three
            # attributes that define the report's meaning are compared; a
            # difference in, say, the description is not worth a write.
            current = sn.get("sys_report", have[title],
                             fields=["filter", "type", "field", "aggregate"])
            desired = {
                "filter": r.get("filter", ""),
                "type": r["type"],
                "field": r.get("field", ""),
                "aggregate": r.get("aggregate", "COUNT"),
            }
            changes = {}
            for key, want in desired.items():
                got = current.get(key) or ""
                if isinstance(got, dict):
                    got = got.get("value") or ""
                if str(got) != str(want):
                    changes[key] = want

            if not changes:
                print(f"  = same    {title}")
                skipped += 1
                continue

            try:
                sn.update("sys_report", have[title], changes)
                for key, value in changes.items():
                    print(f"  ~ updated {title}: {key}")
                    print(f"              now: {value}")
                updated += 1
            except SNError as exc:
                print(f"  x FAILED  {title}: {exc}")
                failed += 1
            continue

        body = {
            "title": title,
            "table": table,
            "type": r["type"],
            "aggregate": r.get("aggregate", "COUNT"),
            "description": r.get("description", ""),
            "active": "true",
        }
        # `field` is the group-by column; omitted for single-score KPIs.
        if r.get("field"):
            body["field"] = r["field"]
        if r.get("filter"):
            body["filter"] = r["filter"]

        try:
            rec = sn.insert("sys_report", body)
            print(f"  + created {title}")
            have[title] = rec.get("sys_id", "")
            created += 1
        except SNError as exc:
            print(f"  x FAILED  {title}: {exc}")
            failed += 1

    print()
    print(f"created {created} · updated {updated} · skipped {skipped} · failed {failed}")

    # Verify by reading back, rather than trusting the insert responses.
    print("\nverification (re-reading from the instance):")
    now = existing_titles(sn, table)
    missing = [r["title"] for r in reports if r["title"] not in now]
    for r in reports:
        mark = "." if r["title"] in now else "x"
        print(f"  {mark} {r['title']}")
    if missing:
        print(f"\nRESULT: {len(missing)} report(s) missing")
        return 1

    print(f"\nRESULT: {len(reports)}/{len(reports)} reports present on {table}")
    print("\nNext: assemble them into a dashboard (one-time, in the UI):")
    print("  All -> Dashboards -> New dashboard -> name it -> then for each report")
    print("  open it and use 'Add to dashboard'. See docs/DASHBOARDS.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
