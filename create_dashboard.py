#!/usr/bin/env python3
"""
create_dashboard.py -- build a PAR dashboard populated with report widgets.

    python create_dashboard.py specs/dashboard_toronto_events.json --dry-run
    python create_dashboard.py specs/dashboard_toronto_events.json
    python create_dashboard.py specs/dashboard_toronto_events.json --delete

Widgets are Platform-Analytics-native: each declares its own `dataSources`
(sourceType=table, table name, encoded query), a `metrics` entry (COUNT) and an
optional `groupBy`. They are NOT bound to sys_report records -- there is no
`reportSysId` prop on this platform. See pa_widgets.py for the evidence behind
that claim.

How a PAR dashboard is assembled
--------------------------------
Four records, in this order:

    par_dashboard          the container        (name, grid, active)
      par_dashboard_tab    a tab                (name, order)   <- required
        par_dashboard_canvas  the layout JSON   (dashboard, dashboard_tab, layout)
        par_dashboard_widget  ONE ROW PER LAYOUT ELEMENT

`par_dashboard_canvas.layout` is a JSON array; each element is:

    {"sys_id": "<32 hex>", "x":0, "y":0, "w":12, "h":14,
     "name": "<widget title>",
     "component_id": "<visualization macroponent sys_id>",
     "component_props": "<JSON string>"}

A widget's filter, group-by field and visualization are read from
specs/reports_toronto_events.json, so the dashboard and the standalone reports
stay in step.

Caveats learned the hard way
----------------------------
1. **The canvas layout is only a cache.** The renderer reads
   `par_dashboard_widget` rows, joined to the layout by a SHARED sys_id:
   `layout[i]["sys_id"] == widget_row.sys_id`. A canvas holding 12 layout
   elements and 0 widget rows renders as "Add your first element".
2. **The widget row's `component` column is mandatory.** An empty `component`
   renders "Can't display this information / the record is not valid" on that
   widget. `stored_component` is optional (221 of 318 native rows omit it).
3. **The canvas must have `dashboard_tab` set at CREATE time.** Patching it
   afterwards returns 200 and silently does not persist. Recreate instead.
4. **`pa_dashboards` (the legacy model) cannot be created over REST at all** --
   403 "security constraints" even as admin. Use `par_dashboard`.
5. **Rebuild in place with --update.** Delete-then-create mints a new sys_id, so
   every open tab, bookmark and shared link 404s.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

import pa_widgets
from sn_api import SNError, ServiceNow


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


def build_layout(widgets: list[dict], report_defs: dict[str, dict], template: dict,
                 table: str) -> list[dict]:
    """One layout element per widget.

    The visualization, filter and group-by are taken from the matching entry in
    specs/reports_toronto_events.json, so the reports and the dashboard cannot
    drift apart -- they are two views of one definition.
    """
    layout = []
    for w in widgets:
        title = w["report"]
        rd = report_defs[title]
        viz = pa_widgets.REPORT_TYPE_TO_VIZ.get(rd.get("type", "bar"), "column")
        label = w.get("label", title)
        layout.append({
            "sys_id": uuid.uuid4().hex,
            "x": w.get("x", 0),
            "y": w.get("y", 0),
            "w": w.get("w", 12),
            "h": w.get("h", 14),
            "name": label,
            "component_id": pa_widgets.VIZ[viz],
            "component_props": pa_widgets.build_props(
                template,
                table=table,
                label=label,
                filter_query=rd.get("filter", ""),
                group_by=rd.get("field", ""),
                viz=viz,
            ),
            "_filter": rd.get("filter", ""),
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
        for wid in sn.query("par_dashboard_widget", encoded_query=f"canvas={canvas['sys_id']}",
                            fields=["sys_id"], limit=500):
            sn.delete("par_dashboard_widget", wid["sys_id"]); removed += 1
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
    parser.add_argument("--update", action="store_true",
                        help="refresh widgets/layout on the existing dashboard, keeping its sys_id")
    parser.add_argument("--reports-spec", type=Path,
                        default=Path("specs/reports_toronto_events.json"),
                        help="where each widget's filter/group-by/visualization comes from")
    parser.add_argument("--template", type=Path, default=None,
                        help="native component_props skeleton (default specs/pa_widget_template.json)")
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
    # Widget data comes from the report DEFINITIONS (type/field/filter), not from
    # the sys_report records. The definitions are the single source of truth, so
    # the dashboard and the reports cannot drift apart.
    if not args.reports_spec.exists():
        print(f"reports spec not found: {args.reports_spec}")
        return 1
    rspec = json.loads(args.reports_spec.read_text(encoding="utf-8"))
    report_defs = {r["title"]: r for r in rspec["reports"]}
    missing = [t for t in titles if t not in report_defs]
    if missing:
        print(f"MISSING definitions in {args.reports_spec}:")
        for t in missing:
            print(f"  x {t}")
        return 1
    template = pa_widgets.load_template(args.template)
    print(f"resolved {len(titles)} widget definition(s) from {args.reports_spec}")

    if args.dry_run:
        print("\nDRY RUN -- nothing created")
        for el in build_layout(widgets, report_defs, template, table):
            props = json.loads(el["component_props"])
            ds = props["dataSources"][0]
            print(f"  x={el['x']:<3} y={el['y']:<3} w={el['w']:<3} h={el['h']:<3} "
                  f"{el['name'][:34]:<36} viz={el['component_id'][:8]} "
                  f"group={ds and (props['groupBy'][0]['groupBy'][0]['groupByField'] if props['groupBy'] else '-'):<20} "
                  f"filter={ds['filterQuery'][:40]}")
        return 0

    existing_id = existing_dashboard(sn, name)
    if existing_id and not args.update:
        print(f"\nA dashboard named {name!r} already exists ({existing_id}).")
        print("  --update  refresh its widgets in place (keeps the sys_id / URL)")
        print("  --delete  remove it entirely")
        return 1

    print("\n[1/4] par_dashboard")
    if existing_id:
        # Updating: keep the dashboard record so its sys_id -- and therefore any
        # bookmark or link to it -- stays valid.
        dash_id = existing_id
        print(f"      reusing existing {dash_id} (URL preserved)")
    else:
        dash = sn.insert("par_dashboard", {
            "name": name,
            "grid": str(spec.get("grid", 48)),
            "active": "true",
            "description": spec.get("description", ""),
        })
        dash_id = dash["sys_id"]
        print(f"      created {dash_id}")

    print("[2/4] par_dashboard_tab")
    tabs = sn.query("par_dashboard_tab", encoded_query=f"dashboard={dash_id}",
                    fields=["sys_id"], limit=10)
    if tabs:
        tab_id = tabs[0]["sys_id"]
        print(f"      reusing existing tab {tab_id}")
    else:
        tab = sn.insert("par_dashboard_tab", {"dashboard": dash_id, "name": "Overview", "order": "0"})
        tab_id = tab["sys_id"]
        print(f"      created {tab_id}")

    # Clear any previous canvas + widget rows so the rebuild is not additive.
    if existing_id:
        for old_canvas in sn.query("par_dashboard_canvas", encoded_query=f"dashboard={dash_id}",
                                   fields=["sys_id"], limit=50):
            for wid in sn.query("par_dashboard_widget", encoded_query=f"canvas={old_canvas['sys_id']}",
                                fields=["sys_id"], limit=500):
                sn.delete("par_dashboard_widget", wid["sys_id"])
            sn.delete("par_dashboard_canvas", old_canvas["sys_id"])
        print("      cleared previous canvas + widget rows")

    print("[3/4] par_dashboard_canvas")
    layout = build_layout(widgets, report_defs, template, table)
    # `_filter` is carried for the pre-flight row count in verification; it must
    # not leak into the stored layout, which the UI also writes to.
    canvas_layout = [{k: v for k, v in el.items() if not k.startswith("_")} for el in layout]
    # dashboard_tab MUST be supplied here; a later PATCH silently does not persist.
    canvas = sn.insert("par_dashboard_canvas", {
        "dashboard": dash_id,
        "dashboard_tab": tab_id,
        "layout": json.dumps(canvas_layout),
    })
    print(f"      created {canvas['sys_id']} with {len(layout)} element(s)")

    # The canvas layout is only a cached representation. The renderer reads
    # par_dashboard_widget rows, and the two are joined by a shared sys_id:
    # layout[i]["sys_id"] MUST equal the widget row's sys_id.
    print("[4/4] par_dashboard_widget rows")
    made = 0
    for element in layout:
        try:
            sn.insert("par_dashboard_widget",
                      pa_widgets.build_widget_row(element, canvas["sys_id"], dash_id))
            made += 1
        except SNError as exc:
            print(f"      x widget failed ({element['name'][:30]}): {exc}")
    print(f"      created {made}/{len(layout)} widget row(s)")

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
        rows = sn.query("par_dashboard_widget", encoded_query=f"canvas={c['sys_id']}",
                        fields=["sys_id", "name", "component", "component_props"], limit=200)
        wid = len(rows)
        print(f"  canvas {c['sys_id']}: tab={'yes' if linked else 'NO'} "
              f"layout_elements={len(arr)} widget_rows={wid}")
        ok = ok and linked and len(arr) == len(layout) and wid == len(layout)

        by_id = {r["sys_id"]: r for r in rows}
        print("\n  pre-flight: does each widget's own filter return any rows?")
        for el in arr:
            row = by_id.get(el.get("sys_id"))
            comp = (row or {}).get("component")
            comp_val = comp.get("value") if isinstance(comp, dict) else comp
            props = json.loads(el["component_props"])
            ds = props["dataSources"][0]
            grp = props["groupBy"][0]["groupBy"][0]["groupByField"] if props.get("groupBy") else "-"
            if not comp_val:
                print(f"    NO COMPONENT -- will not render: {el['name'][:40]}")
                ok = False
                continue
            try:
                n = sn.count(ds["tableOrViewName"], encoded_query=ds["filterQuery"])
            except SNError as exc:
                n = f"ERR {exc}"
                ok = False
            print(f"    {el['name'][:40]:<42} comp={comp_val[:8]} group={grp:<22} rows={n}")

    print()
    if not ok:
        print("RESULT: created, but verification failed -- inspect above")
        return 1
    print(f"RESULT: dashboard {name!r} created with {len(layout)} report widget(s)")
    print("Open it under Dashboards, or /now/dashboards")
    return 0


if __name__ == "__main__":
    sys.exit(main())
