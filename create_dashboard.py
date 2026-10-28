#!/usr/bin/env python3
"""
create_dashboard.py -- build a PAR dashboard populated with report widgets.

    python create_dashboard.py specs/dashboard_toronto_events.json --dry-run
    python create_dashboard.py specs/dashboard_toronto_events.json
    python create_dashboard.py specs/dashboard_toronto_events.json --delete

This works. It is not a shell: each widget is bound to a real `sys_report` via
the "Report/PA Widget" macroponent's `reportSysId` prop.

How a PAR dashboard is assembled
--------------------------------
Three records, in this order:

    par_dashboard          the container        (name, grid, active)
      par_dashboard_tab    a tab                (name, order)   <- required
        par_dashboard_canvas  the layout JSON   (dashboard, dashboard_tab, layout)

`par_dashboard_canvas.layout` is a JSON array; each element is:

    {"sys_id": "<32 hex>", "x":0, "y":0, "w":12, "h":14,
     "name": "<widget title>",
     "component_id": "<macroponent sys_id>",
     "component_props": "<JSON string>"}

For report widgets the macroponent is **Report/PA Widget**
(`7dbc62b27efa88d6d910579a68c7a65d`) and its props are:

    {"label": "...", "reportSysId": "<sys_report sys_id>"}

Two caveats learned the hard way
--------------------------------
1. **The canvas must have `dashboard_tab` set at CREATE time.** Patching it
   afterwards returns 200 and silently does not persist. Recreate instead.
2. **`pa_dashboards` (the legacy model) cannot be created over REST at all** --
   403 "security constraints" even as admin. Use `par_dashboard`.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from sn_api import SNError, ServiceNow

REPORT_WIDGET = "7dbc62b27efa88d6d910579a68c7a65d"   # macroponent: Report/PA Widget


def existing_dashboard(sn: ServiceNow, name: str) -> str | None:
    rows = sn.query("par_dashboard", encoded_query=f"name={name}", fields=["sys_id"], limit=1)
    return rows[0]["sys_id"] if rows else None


def resolve_reports(sn: ServiceNow, table: str, titles: list[str]) -> dict[str, str]:
    """title -> sys_id, for the reports named in the spec."""
    found: dict[str, str] = {}
    offset = 0
    while True:
        rows = sn.query("sys_report", encoded_query=f"table={table}^ORDERBYtitle",
                        fields=["sys_id", "title"], limit=1000, offset=offset)
        if not rows:
            break
        for r in rows:
            if r.get("title") in titles:
                found[r["title"]] = r["sys_id"]
        offset += 1000
        if len(rows) < 1000:
            break
    return found


def build_layout(widgets: list[dict], report_ids: dict[str, str]) -> list[dict]:
    layout = []
    for w in widgets:
        title = w["report"]
        layout.append({
            "sys_id": uuid.uuid4().hex,
            "x": w.get("x", 0),
            "y": w.get("y", 0),
            "w": w.get("w", 12),
            "h": w.get("h", 14),
            "name": w.get("label", title),
            "component_id": REPORT_WIDGET,
            "component_props": json.dumps({
                "label": w.get("label", title),
                "reportSysId": report_ids[title],
            }),
        })
    return layout


def delete_dashboard(sn: ServiceNow, name: str) -> int:
    dash_id = existing_dashboard(sn, name)
    if not dash_id:
        print(f"  no dashboard named {name!r}")
        return 0
    removed = 0
    for canvas in sn.query("par_dashboard_canvas", encoded_query=f"dashboard={dash_id}",
                           fields=["sys_id"], limit=50):
        sn.delete("par_dashboard_canvas", canvas["sys_id"]); removed += 1
    for tab in sn.query("par_dashboard_tab", encoded_query=f"dashboard={dash_id}",
                        fields=["sys_id"], limit=50):
        sn.delete("par_dashboard_tab", tab["sys_id"]); removed += 1
    sn.delete("par_dashboard", dash_id); removed += 1
    print(f"  deleted dashboard {name!r} ({removed} record(s))")
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a PAR dashboard from a JSON spec.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--delete", action="store_true")
    args = parser.parse_args(argv)

    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    name = spec["name"]
    table = spec["table"]
    widgets = spec["widgets"]

    print(f"spec      : {args.spec}")
    print(f"dashboard : {name}")
    print(f"source    : {table}")
    print(f"widgets   : {len(widgets)}")
    print()

    sn = ServiceNow()

    if args.delete:
        delete_dashboard(sn, name)
        return 0

    titles = [w["report"] for w in widgets]
    report_ids = resolve_reports(sn, table, titles)
    missing = [t for t in titles if t not in report_ids]
    if missing:
        print("MISSING reports (create them first with create_reports.py):")
        for t in missing:
            print(f"  x {t}")
        return 1
    print(f"resolved {len(report_ids)}/{len(titles)} report(s)")
    for t in titles:
        print(f"  . {t}")

    if args.dry_run:
        print("\nDRY RUN -- nothing created")
        for el in build_layout(widgets, report_ids):
            props = json.loads(el["component_props"])
            print(f"  would place x={el['x']:<3} y={el['y']:<3} w={el['w']:<3} h={el['h']:<3} "
                  f"{el['name'][:38]:<40} report={props['reportSysId']}")
        return 0

    if existing_dashboard(sn, name):
        print(f"\nA dashboard named {name!r} already exists. Use --delete first to rebuild.")
        return 1

    print("\n[1/3] par_dashboard")
    dash = sn.insert("par_dashboard", {
        "name": name,
        "grid": str(spec.get("grid", 48)),
        "active": "true",
        "description": spec.get("description", ""),
    })
    dash_id = dash["sys_id"]
    print(f"      created {dash_id}")

    print("[2/3] par_dashboard_tab")
    tab = sn.insert("par_dashboard_tab", {"dashboard": dash_id, "name": "Overview", "order": "0"})
    tab_id = tab["sys_id"]
    print(f"      created {tab_id}")

    print("[3/3] par_dashboard_canvas")
    layout = build_layout(widgets, report_ids)
    # dashboard_tab MUST be supplied here; a later PATCH silently does not persist.
    canvas = sn.insert("par_dashboard_canvas", {
        "dashboard": dash_id,
        "dashboard_tab": tab_id,
        "layout": json.dumps(layout),
    })
    print(f"      created {canvas['sys_id']} with {len(layout)} widget(s)")

    print("\n=== verification (re-reading from the instance) ===")
    back = sn.query("par_dashboard_canvas", encoded_query=f"dashboard={dash_id}",
                    fields=["sys_id", "dashboard_tab", "layout"], limit=10)
    ok = True
    for c in back:
        try:
            arr = json.loads(c.get("layout") or "[]")
        except Exception:
            arr = []
        linked = bool(c.get("dashboard_tab"))
        print(f"  canvas {c['sys_id']}: tab={'yes' if linked else 'NO'} widgets={len(arr)}")
        ok = ok and linked and len(arr) == len(layout)
        for el in arr:
            props = json.loads(el["component_props"])
            print(f"    {el['name'][:44]:<46} reportSysId={props.get('reportSysId')}")

    print()
    if not ok:
        print("RESULT: created, but verification failed -- inspect above")
        return 1
    print(f"RESULT: dashboard {name!r} created with {len(layout)} report widget(s)")
    print("Open it under Dashboards, or /now/dashboards")
    return 0


if __name__ == "__main__":
    sys.exit(main())
