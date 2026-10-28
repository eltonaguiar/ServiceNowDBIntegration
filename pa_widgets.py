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


def load_template(path: Path | str | None = None) -> dict:
    """The native prop skeleton. Cosmetic only -- no data references."""
    p = Path(path) if path else DEFAULT_TEMPLATE
    return json.loads(p.read_text(encoding="utf-8"))


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
                viz: str = "column") -> str:
    """Return the JSON string for par_dashboard_widget.component_props.

    filter_query is a ServiceNow encoded query. Native widgets terminate theirs
    with `^EQ` (the condition builder appends it), so we do the same for
    consistency with anything the UI later re-saves.
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

    props["metrics"] = [{
        "dataSource": src,
        "id": _metric_id(src),
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

    props["headerTitle"] = label
    props["yAxisTitle"] = label if group_by else ""
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
            "message": props.get("headerTitle", element["name"]),
            "comment": "",
        }]),
    }
