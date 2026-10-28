#!/usr/bin/env python3
"""
pa_widgets.py -- build correct `par_dashboard_widget` records.

Why this file exists
--------------------
The first attempt at a programmatic dashboard produced records that *looked*
right in the database but rendered as:

    Can't display this information
    You are either not authorized or the record is not valid

...on every widget. Two mistakes, both found by inspecting native widgets
rather than by guessing:

1. **`reportSysId` is not a prop this platform uses.** A scan of all 318
   `par_dashboard_widget` rows on the instance found `reportSysId` in exactly
   one place: the widgets I had created. No native widget uses it. PAR
   dashboards in this release are Platform-Analytics-native: data is declared
   in `dataSources` / `metrics` / `groupBy`, not by pointing at a `sys_report`.

2. **`component` and `stored_component` must be populated.** All 318 native
   rows set `component` to a *visualization* macroponent ("Single score",
   "Column", "Donut", "Horizontal bar", ...). My rows left it empty, so the
   renderer had no component to instantiate -- which is precisely what
   "the record is not valid" means. `stored_component` is optional (221 of 318
   native rows omit it), so we do not set it.

The shape below is cloned from a native, working widget
(`15b8cbd09f9e88dce075017cb4744fa4`, "Computers by OS") so every cosmetic
default -- axis config, legend, empty-state text, configVersion -- matches what
the platform itself writes. Only `dataSources`, `metrics`, `groupBy`,
`headerTitle`, `yAxisTitle` and `componentId` are replaced per widget.

`specs/pa_widget_template.json` is that native prop set with the data-specific
keys removed. Regenerate it from any working table-mode widget if the platform
upgrades:

    python -c "from sn_api import ServiceNow; import json; \\
      print(json.loads(ServiceNow().get('par_dashboard_widget','<id>')['component_props']))"
"""

from __future__ import annotations

import base64
import copy
import json
import time
from pathlib import Path

# Visualization macroponents, read off the 318 native widget rows (count in
# brackets). Add more by querying par_dashboard_widget and joining `component`
# to sys_ux_macroponent.name.
VIZ = {
    "single_score":   "d24d53f60350de7a652caf3188a46ed2",  # Single score   [101]
    "vertical_bar":   "23051643b7e03010097cb81cde11a910",  # Vertical bar   [ 35]
    "column":         "2bd5dc19a7ad083941b7c992a966dd78",  # Column         [ 29]
    "line":           "18ac962264404bcc0039359d184b15f3",  # Line           [ 26]
    "donut":          "a2b0596cec6b9d49dd1ff9bf76b5084b",  # Donut          [ 22]
    "pie":            "96abe7e38d8790718022c5630a92176c",  # Pie            [ 13]
    "horizontal_bar": "85855283b7e03010097cb81cde11a91d",  # Horizontal bar [  9]
}

# `sys_report.type` -> widget visualization. The reports we already created use
# these three type names; "bar" is the plain column chart.
REPORT_TYPE_TO_VIZ = {
    "single_score": "single_score",
    "bar": "column",
    "column": "column",
    "horizontal_bar": "horizontal_bar",
    "donut": "donut",
    "pie": "pie",
    "line": "line",
}

DEFAULT_TEMPLATE = Path(__file__).with_name("specs") / "pa_widget_template.json"
TEMPLATE_DIR = Path(__file__).with_name("specs") / "templates"


def load_template(kind: str | None = None, path: Path | str | None = None) -> dict:
    """Cosmetic prop skeleton for a visualization.

    A single shared template does NOT work: each visualization carries its own
    `extensionType`, `dataCategory` and `trendBy`. Rendering a chart whose
    template lacks them fails with:

        Invalid configuration -- A 'Trend By' must be selected for the
        visualization to render.

    So each kind gets a template captured from a native widget that renders.
    Fall back to the generic skeleton when no per-kind file exists.
    """
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    if kind:
        for cand in (TEMPLATE_DIR / f"chart_{kind}.json", TEMPLATE_DIR / f"{kind}.json"):
            if cand.exists():
                return json.loads(cand.read_text(encoding="utf-8"))
    return json.loads(DEFAULT_TEMPLATE.read_text(encoding="utf-8"))


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _source_id(table: str) -> str:
    """Unique datasource id.

    Native ids are base64 of "table" + table name + a high-resolution number,
    e.g. `tablecmdb_ci_computer17055166110336636223.385035667`. They only need
    to be unique and stable within the widget, so we mimic the format.
    """
    return _b64(f"table{table}{int(time.time() * 1000)}{time.perf_counter_ns()}")


def _metric_id(source_id: str) -> str:
    return _b64(f"{source_id}{int(time.time() * 1000)}.{time.perf_counter_ns()}")


def build_props(template: dict,
                table: str,
                label: str,
                filter_query: str = "",
                group_by: str = "",
                viz: str = "column",
                trend_field: str = "",
                follow_filters: bool = True) -> str:
    """Return the JSON string for par_dashboard_widget.component_props.

    filter_query is a ServiceNow encoded query. Native widgets terminate theirs
    with `^EQ` (the condition builder appends it), so we do the same for
    consistency with anything the UI later re-saves.

    follow_filters=False is important for widgets that represent an ABSOLUTE
    window ("today", "next 7 days"). A dashboard date filter is ANDed with the
    widget's own condition, so "today" plus a filter for a different day gives
    zero rows -- see docs/DASHBOARDS.md 2g.
    """
    if viz not in VIZ:
        raise ValueError(f"unknown visualization {viz!r}; pick one of {sorted(VIZ)}")

    props = copy.deepcopy(template)
    src = _source_id(table)

    fq = (filter_query or "").strip()
    if fq and not fq.endswith("^EQ"):
        fq = fq + "^EQ"

    props["dataSources"] = [{
        "isDatabaseView": False,
        "allowRealTime": True,
        "label": label,
        "sourceType": "table",              # the other real option is "indicator"
        "tableOrViewName": table,
        "filterQuery": fq,
        # Native rows carry this list; the `component` column is what actually
        # decides the visual, but keeping them in step is harmless and avoids
        # confusing anyone who reads the record.
        "preferredVisualizations": [VIZ[viz]],
        "id": src,
        "dataCategories": ["trend", "group", "simple"],
    }]

    metric_id = _metric_id(src)
    props["metrics"] = [{
        "dataSource": src,
        "id": metric_id,
        "label": label,
        "aggregateFunction": "COUNT",
        "axisId": "primary",
        "numberFormat": {"customFormat": False},
        "dateFormat": {"customFormat": False},
        "stringFormat": {"customFormat": False},
    }]

    if group_by:
        props["groupBy"] = [{
            "groupBy": [{
                "dataSource": src,
                "groupByField": group_by,
                "isRange": False,
                "isChoice": True,
                "isPaBucket": False,
            }],
            "maxNumberOfGroups": 12,
            "numberOfGroupsBasedOn": "NO_OF_GROUP_BASED_ON_PER_METRIC",
            "showOthers": True,
            "disableRanges": False,
        }]
    else:
        props["groupBy"] = []

    # A native template may carry a trendBy pointing at ANOTHER widget's metric.
    # Leaving those ids alone renders the wrong series (or nothing), so remap
    # them onto this widget's own metric and date field.
    tb = props.get("trendBy")
    if isinstance(tb, dict) and tb.get("trendByFields"):
        for f in tb["trendByFields"]:
            if isinstance(f, dict):
                f["metric"] = metric_id
                if trend_field:
                    f["field"] = trend_field

    # `dataCategory` is what tells the renderer whether to expect a trend axis.
    # Omit it on a grouped chart and the platform demands a 'Trend By'.
    if "dataCategory" in props or group_by:
        props["dataCategory"] = "group" if group_by else "simple"

    props["headerTitle"] = label
    props["yAxisTitle"] = label if group_by else ""
    props["followFilters"] = bool(follow_filters)
    # A short random-ish id; native rows have values like "h1dnwom".
    props["componentId"] = base64.b32encode(
        f"{table}{label}{time.perf_counter_ns()}".encode()).decode().lower()[:8]
    return json.dumps(props)


def build_widget_row(element: dict, canvas_id: str, dash_id: str) -> dict:
    """The par_dashboard_widget insert body for one layout element.

    `component` is the visualization macroponent and is mandatory -- omitting it
    is what produced "the record is not valid".
    """
    props = json.loads(element["component_props"])
    return {
        "sys_id": element["sys_id"],
        "canvas": canvas_id,
        "name": element["name"],
        "x": str(element["x"]),
        "y": str(element["y"]),
        "w": str(element["w"]),
        "h": str(element["h"]),
        "component": element["component_id"],
        "component_props": element["component_props"],
        "required_translations": json.dumps([{
            "code": f"PAR_DASHBOARD.{dash_id}.{element['sys_id']}",
            "message": props.get("headerTitle") or props.get("listTitle")
                       or props.get("label") or element["name"],
            "comment": "",
        }]),
    }


# --------------------------------------------------------------- headings
HEADING = "1f6e0643eca7a637e36bd7833549ec9e"   # Heading  [20 native rows]


def build_heading(template: dict, label: str, level: str = "2") -> str:
    """A section title. `variant: header-secondary` is what native rows use."""
    p = copy.deepcopy(template)
    p["label"] = label
    p["level"] = str(level)
    return json.dumps(p)


# --------------------------------------------------------------- record lists
LIST_SIMPLE = "2b1c080881e05dc63b917044290b233f"   # List - Simple  [18 native rows]


def build_list(template: dict, table: str, label: str, query: str = "",
               columns: list[str] | None = None, follow_filters: bool = True) -> str:
    """A live record list -- this is the "feed" of upcoming events.

    `columns` are element names on `table`; the native widget stores them as a
    comma-separated string. Keep this list short: the widget renders a fixed
    7 columns at most (maxColumns).
    """
    p = copy.deepcopy(template)
    p["table"] = table
    p["listTitle"] = label
    p["query"] = (query or "").strip()
    p["followFilters"] = bool(follow_filters)
    if columns:
        p["columns"] = ",".join(columns)
    p["maxColumns"] = max(p.get("maxColumns", 7), len(columns or []))
    # Reading is the whole point of this dashboard, so kill the editing affordances.
    p["hideInlineEditing"] = True
    p["hideRowSelector"] = False
    p["hideColumnFiltering"] = False   # let people narrow the feed inline
    p["hideColumnSorting"] = False
    p["hideViewAll"] = False
    return json.dumps(p)


# --------------------------------------------------------------- filters
FILTER = "226449101138d0dff1abe0e1566c8b2a"   # Filter  [15 native rows]

# Dashboard widgets pick up filters through this magic string. Native widgets
# set exactly this value on every filter-aware component.
PAR_FILTERS = "@state.parFilters"


def _filter_target(table: str, field: str, field_type: str = "string") -> dict:
    return {
        "type": "table",
        "payload": {"table": table, "field": field, "primaryKey": None,
                    "fieldType": field_type},
        "encodedQuery": None, "emptyOption": None, "choices": None,
    }


def build_filter(name: str, table: str, field: str, kind: str = "multiselect",
                 field_type: str = "string") -> str:
    """A dashboard filter pill.

    `kind`: multiselect | singleselect | date.

    Native filter widgets also carry a `cascadeScope` value that points at a
    sibling record we could not identify over REST, so it is omitted here. Two
    independent filters on one dashboard behave independently without it; only
    cascading BETWEEN filters is affected.
    """
    if kind not in ("multiselect", "singleselect", "date"):
        raise ValueError(f"unknown filter kind {kind!r}")
    target = _filter_target(table, field, field_type)
    p = {
        "host": "dashboard",
        "id": "targets",
        "filterId": base64.b32encode(f"{table}{field}{time.perf_counter_ns()}".encode())
                            .decode().lower()[:19],
        "filterName": name,
        "filterComponentType": kind,
        "filterConfigurations": [],
        "datasource": {} if kind == "date" else target,
        "targets": [target],
        "value": [target],
        "defaultSelectedItems": [],
        "enableClearFilter": True,
        "filterElementLayout": "vertical",
        "filterElementType": "pill",
        "isShowSelectedValueInPill": True,
        "maxElements": 500,
        "noDebounce": "",
        "sort": "ASC",
        "propLabel": "Data to filter",
    }
    if kind == "date":
        p["dateFilterView"] = "calendar-reldates"
        # Do NOT preselect a range.
        #
        # `defaultSelectedDateRange: {"range": "SN_DYNAMIC_DATERANGE__NEXT_30_DAYS",
        # "label": ...}` is rejected by the renderer with:
        #     Invalid configuration. Invalid value for dynamicTimeRange field:
        #     SN_DYNAMIC_DATERANGE__NEXT_30_DAYS
        # even though that exact token appears in every native widget's
        # `allowedDateRanges`. The error then propagates to EVERY filter-aware
        # widget on the dashboard, so a bad default breaks all the score cards.
        # Starting with no selection leaves the dashboard unconstrained until a
        # human picks a range, which is also the safer default.
        p["enableResetToDefault"] = False
        p["isShowSelectedValueInPill"] = False
        p["allowedDateRanges"] = [
            {"label": "Today", "range": "SN_DYNAMIC_DATERANGE__TODAY"},
            {"label": "Yesterday", "range": "SN_DYNAMIC_DATERANGE__YESTERDAY"},
            {"label": "Next 7 days", "range": "SN_DYNAMIC_DATERANGE__NEXT_7_DAYS"},
            {"label": "Next 30 days", "range": "SN_DYNAMIC_DATERANGE__NEXT_30_DAYS"},
            {"label": "Next 3 months", "range": "SN_DYNAMIC_DATERANGE__NEXT_3_MONTHS"},
            {"label": "Last week", "range": "SN_DYNAMIC_DATERANGE__LAST_WEEK"},
            {"label": "Last month", "range": "SN_DYNAMIC_DATERANGE__LAST_MONTH"},
            {"label": "Last quarter", "range": "SN_DYNAMIC_DATERANGE__LAST_QUARTER"},
            {"label": "Last 6 months", "range": "SN_DYNAMIC_DATERANGE__LAST_6_MONTH"},
            {"label": "Last year", "range": "SN_DYNAMIC_DATERANGE__LAST_YEAR"},
        ]
        p["targets"] = [{"type": "table",
                         "table": {"id": table},
                         "field": {"id": field, "type": "glide_date_time",
                                   "reference": ""}}]
        p["value"] = p["targets"]
    return json.dumps(p)
