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


def build_layout(widgets: list[dict], report_defs: dict[str, dict], template: dict | None,
                 table: str, trend_field: str = "u_start") -> list[dict]:
    """One layout element per widget.

    `kind` selects the component:

        chart   (default) count / group-by from the matching reports-spec entry
        heading section title
        list    live record feed (the 'happening today' style panels)
        filter  dashboard filter pill

    For charts the visualization, filter and group-by come from the matching
    entry in specs/reports_toronto_events.json, so the reports and the dashboard
    cannot drift apart -- they are two views of one definition.
    """
    layout = []
    for w in widgets:
        kind = w.get("kind", "chart")
        label = w.get("label") or w.get("report") or ""

        if kind == "heading":
            comp = pa_widgets.HEADING
            props = pa_widgets.build_heading(
                pa_widgets.load_template("heading"), label, w.get("level", "2"))

        elif kind == "richtext":
            comp = pa_widgets.RICH_TEXT
            props = pa_widgets.build_richtext(w.get("html", ""))

        elif kind == "list":
            comp = pa_widgets.LIST_SIMPLE
            props = pa_widgets.build_list(
                pa_widgets.load_template("list"),
                table=w.get("table", table),
                label=label,
                query=w.get("query", ""),
                columns=w.get("columns"),
                follow_filters=w.get("follow_filters", True),
            )

        elif kind == "filter":
            comp = pa_widgets.FILTER
            props = pa_widgets.build_filter(
                label,
                table=w.get("table", table),
                field=w["field"],
                kind=w.get("filter_kind", "multiselect"),
                field_type=w.get("field_type", "string"),
            )

        elif kind == "geomap":
            comp = pa_widgets.VIZ["geomap"]
            props = pa_widgets.build_geopoint(
                pa_widgets.load_template("geomap"),
                table=table,
                label=label,
                filter_query=w.get("query", ""),
                group_by=w.get("group_by", "u_location_ref"),
                follow_filters=w.get("follow_filters", True),
                basemap=w.get("basemap", pa_widgets.BASEMAP_WORLD),
            )

        elif kind == "chart":
            rd = report_defs[w["report"]]
            viz = pa_widgets.REPORT_TYPE_TO_VIZ.get(rd.get("type", "bar"), "column")
            comp = pa_widgets.VIZ[viz]
            props = pa_widgets.build_props(
                template if template is not None else pa_widgets.load_template(viz),
                table=table,
                label=label,
                filter_query=rd.get("filter", ""),
                group_by=rd.get("field", ""),
                viz=viz,
                trend_field=w.get("trend_field", trend_field),
                follow_filters=w.get("follow_filters", True),
            )

        else:
            raise ValueError(f"unknown widget kind {kind!r}")

        layout.append({
            "sys_id": uuid.uuid4().hex,
            "x": w.get("x", 0),
            "y": w.get("y", 0),
            "w": w.get("w", 12),
            "h": w.get("h", 14),
            "name": label,
            "component_id": comp,
            "component_props": props,
            "_kind": kind,
            "_filter": (report_defs[w["report"]].get("filter", "") if kind == "chart"
                        else w.get("query", "")),        })
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


def normalise_tabs(spec: dict) -> list[tuple[str, int, list[dict]]]:
    """Spec -> [(tab name, order, widgets)].

    Supports either a single implicit tab (`widgets`) or several (`tabs`), so a
    dashboard can carry a working view plus a diagnostics/health tab without
    those widgets cluttering the main view.
    """
    if spec.get("tabs"):
        return [(t["name"], i, t.get("widgets") or []) for i, t in enumerate(spec["tabs"])]
    return [("Overview", 0, spec.get("widgets") or [])]


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
                        help="native component_props skeleton (default per-visualization templates)")
    args = parser.parse_args(argv)

    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    name = spec["name"]
    table = spec["table"]
    tabs = normalise_tabs(spec)
    all_widgets = [w for _, _, ws in tabs for w in ws]

    print(f"spec      : {args.spec}")
    print(f"dashboard : {name}")
    print(f"source    : {table}")
    print(f"tabs      : {len(tabs)} ({', '.join(t[0] for t in tabs)})")
    print(f"widgets   : {len(all_widgets)}")
    print()

    sn = ServiceNow()

    if args.delete:
        delete_dashboard(sn, name)
        return 0

    titles = [w["report"] for w in all_widgets if w.get("kind", "chart") == "chart"]
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
    template = pa_widgets.load_template(args.template) if args.template else None
    print(f"resolved {len(titles)} chart definition(s) from {args.reports_spec}")

    if args.dry_run:
        print("\nDRY RUN -- nothing created")
        for tab_name, order, ws in tabs:
            print(f"  tab {order}: {tab_name} ({len(ws)} widget(s))")
            for el in build_layout(ws, report_defs, template, table):
                print(f"    x={el['x']:<3} y={el['y']:<3} w={el['w']:<3} h={el['h']:<3} "
                      f"{el['name'][:34]:<36} comp={el['component_id'][:8]}")
        return 0

    existing_id = existing_dashboard(sn, name)
    if existing_id and not args.update:
        print(f"\nA dashboard named {name!r} already exists ({existing_id}).")
        print("  --update  refresh its widgets in place (keeps the sys_id / URL)")
        print("  --delete  remove it entirely")
        return 1

    print("\n[1/5] par_dashboard")
    if existing_id:
        dash_id = existing_id
        # Keep the record (and its URL) but let the spec own the description, so
        # --update actually converges the record onto the spec instead of
        # leaving stale text behind.
        sn.update("par_dashboard", dash_id, {
            "description": spec.get("description", ""),
            "grid": str(spec.get("grid", 48)),
        })
        print(f"      reusing existing {dash_id} (URL preserved, description synced)")
    else:
        dash = sn.insert("par_dashboard", {
            "name": name,
            "grid": str(spec.get("grid", 48)),
            "active": "true",
            "description": spec.get("description", ""),
        })
        dash_id = dash["sys_id"]
        print(f"      created {dash_id}")

    # Drop previous canvases + widget rows so the rebuild is not additive. Tabs
    # are kept and matched by name, so their identity survives a rebuild.
    if existing_id:
        n_canvas = n_widget = 0
        for old_canvas in sn.query("par_dashboard_canvas", encoded_query=f"dashboard={dash_id}",
                                   fields=["sys_id"], limit=50):
            for wid in sn.query("par_dashboard_widget", encoded_query=f"canvas={old_canvas['sys_id']}",
                                fields=["sys_id"], limit=500):
                sn.delete("par_dashboard_widget", wid["sys_id"]); n_widget += 1
            sn.delete("par_dashboard_canvas", old_canvas["sys_id"]); n_canvas += 1
        print(f"      cleared {n_canvas} canvas(es) and {n_widget} widget row(s)")

    existing_tabs = {t.get("name"): t["sys_id"] for t in
                     sn.query("par_dashboard_tab", encoded_query=f"dashboard={dash_id}",
                              fields=["sys_id", "name"], limit=20)}

    built: list[tuple[str, str, list[dict]]] = []   # (tab name, canvas id, layout)
    for tab_name, order, ws in tabs:
        print(f"\n=== tab {order}: {tab_name} ===")
        tab_id = existing_tabs.get(tab_name)
        if tab_id:
            sn.update("par_dashboard_tab", tab_id, {"order": str(order)})
            print(f"  [2/5] par_dashboard_tab  reusing {tab_id}")
        else:
            tab = sn.insert("par_dashboard_tab",
                            {"dashboard": dash_id, "name": tab_name, "order": str(order)})
            tab_id = tab["sys_id"]
            print(f"  [2/5] par_dashboard_tab  created {tab_id}")

        print(f"  [3/5] par_dashboard_canvas")
        layout = build_layout(ws, report_defs, template, table)
        canvas_layout = [{k: v for k, v in el.items() if not k.startswith("_")} for el in layout]
        # dashboard_tab MUST be supplied here; a later PATCH silently does not persist.
        canvas = sn.insert("par_dashboard_canvas", {
            "dashboard": dash_id,
            "dashboard_tab": tab_id,
            "layout": json.dumps(canvas_layout),
        })
        print(f"        created {canvas['sys_id']} with {len(layout)} element(s)")

        print(f"  [4/5] par_dashboard_widget rows")
        made = 0
        for element in layout:
            try:
                sn.insert("par_dashboard_widget",
                          pa_widgets.build_widget_row(element, canvas["sys_id"], dash_id))
                made += 1
            except SNError as exc:
                print(f"        x {element['name'][:30]}: {exc}")
        print(f"        created {made}/{len(layout)} widget row(s)")
        built.append((tab_name, canvas["sys_id"], layout))

    print("\n[5/5] verification (re-reading from the instance)")
    ok = True
    for tab_name, canvas_id, layout in built:
        back = sn.query("par_dashboard_canvas", encoded_query=f"sys_id={canvas_id}",
                        fields=["sys_id", "dashboard_tab", "layout"], limit=1)
        arr = json.loads(back[0].get("layout") or "[]") if back else []
        linked = bool(back and back[0].get("dashboard_tab"))
        rows = sn.query("par_dashboard_widget", encoded_query=f"canvas={canvas_id}",
                        fields=["sys_id", "name", "component", "component_props"], limit=200)
        print(f"\n  tab {tab_name!r}: canvas={canvas_id} tab_linked={'yes' if linked else 'NO'} "
              f"layout={len(arr)} widget_rows={len(rows)}")
        ok = ok and linked and len(arr) == len(layout) and len(rows) == len(layout)

        by_id = {r["sys_id"]: r for r in rows}
        for el in arr:
            row = by_id.get(el.get("sys_id"))
            comp = (row or {}).get("component")
            comp_val = comp.get("value") if isinstance(comp, dict) else comp
            props = json.loads(el["component_props"])
            if not comp_val:
                print(f"    NO COMPONENT -- will not render: {el['name'][:40]}")
                ok = False
                continue
            if comp_val in (pa_widgets.HEADING, pa_widgets.FILTER, pa_widgets.RICH_TEXT):
                kind = {pa_widgets.HEADING: "heading", pa_widgets.FILTER: "filter",
                        pa_widgets.RICH_TEXT: "richtext"}[comp_val]
                print(f"    {el['name'][:40]:<42} ({kind})")
                continue
            if comp_val == pa_widgets.LIST_SIMPLE:
                tbl, q, label = props.get("table"), props.get("query", ""), "list"
            else:
                ds = props["dataSources"][0]
                tbl, q = ds["tableOrViewName"], ds["filterQuery"]
                label = (props["groupBy"][0]["groupBy"][0]["groupByField"]
                         if props.get("groupBy") else "-")
            try:
                n = sn.count(tbl, encoded_query=q)
            except SNError as exc:
                n = f"ERR {exc}"
                ok = False
            print(f"    {el['name'][:40]:<42} group={str(label):<22} rows={n}")

    print()
    if not ok:
        print("RESULT: created, but verification failed -- inspect above")
        return 1
    total = sum(len(l) for _, _, l in built)
    print(f"RESULT: dashboard {name!r} built with {len(built)} tab(s), {total} widget(s)")
    print("Open it under Dashboards, or /now/dashboards")
    return 0


if __name__ == "__main__":
    sys.exit(main())
