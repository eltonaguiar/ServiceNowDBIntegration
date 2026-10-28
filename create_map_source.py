#!/usr/bin/env python3
"""
create_map_source.py -- make the events table mappable.

    python create_map_source.py --dry-run
    python create_map_source.py

What was blocking the Geomap
----------------------------
Three things had to be understood before a map widget could work:

1. **`Geomap.mapSysId` is the BASEMAP, not the data mapping.** It points at
   `sys_report_map` -- the GeoJSON geometry records (World, states, provinces).
   The native widget uses `93b8a3a2d7101200bd4a4ebfae61033a`, key `world`.
   (It is NOT in `sys_report_map_source`, which is why searching there failed.)

2. **The data mapping lives in `sys_report_map_source`, which is a
   parent/child chain.** A business rule named *"If parent is empty set empty
   values"* nulls `type`, `type_mapping` and `join_by_column` on a standalone
   record -- the write returns HTTP 200 and `sys_mod_count` increments, but the
   values stay empty. That silent no-op is what made an earlier attempt look
   like it had worked.

   Native shape (sys_user):

       root   parent=''    type=''        aggregate_field=location
       L1     parent=root  type=Level 1   type_mapping=mapping
                                          aggregate_field=location.country
       L2     parent=L1    type=Level 2   type_mapping=mapping
                                          aggregate_field=location.state
       L3     parent=L2    type=Level 3   type_mapping=use_lat_lon
                                          aggregate_field=location.city

3. **Coordinates come from a location record**, not from bare lat/lng columns
   (all 52 native sources walk a reference). `u_location_ref` -> `cmn_location`
   is what makes this possible; `backfill_geo_category.py` created those 440
   records and populated 9,599 references.

This script mirrors the native chain for `u_toronto_event`, walking through
`u_location_ref` (country / state levels, then the record itself for
use_lat_lon). Idempotent: it reuses any record already present by name.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Dict, List

from sn_api import SNError, ServiceNow

TABLE = "u_toronto_event"
REF = "u_location_ref"
ROOT_NAME = "Toronto events by venue"

# sys_report_map_type records (Level 1..3). Verified present on the instance.
LEVEL = {
    1: "7bbdd872d7101200bd4a4ebfae61031c",
    2: "a6edd872d7101200bd4a4ebfae610359",
    3: "23fdd872d7101200bd4a4ebfae610340",
}
JOIN_BY = "hc-key"          # matches every native Level record

# What to create, in order. aggregate_field is a dot-walk; the final level is
# the record that actually carries latitude/longitude.
CHAIN = [
    (None, "", "", "", REF,                     "root: one group per venue record"),
    (1, "Level 1", "mapping",     JOIN_BY, f"{REF}.country", "drill: country"),
    (2, "Level 2", "mapping",     JOIN_BY, f"{REF}.state",   "drill: province/state"),
    (3, "Level 3", "use_lat_lon", JOIN_BY, REF,              "plot: venue lat/long"),
]

SHOW = ["sys_id", "name", "parent", "type", "type_mapping", "aggregate_field", "join_by_column"]


def existing(sn: ServiceNow) -> Dict[str, dict]:
    out = {}
    for r in sn.query("sys_report_map_source", encoded_query=f"table={TABLE}",
                      fields=SHOW, limit=20):
        out[r.get("name") or ""] = r
    return out


def flat(v):
    return v.get("value") if isinstance(v, dict) else v


def show(r: dict) -> str:
    return (f"type={flat(r.get('type')) or '-'} "
            f"type_mapping={flat(r.get('type_mapping')) or '-'!r} "
            f"aggregate={flat(r.get('aggregate_field'))!r} "
            f"join_by={flat(r.get('join_by_column')) or '-'!r}")


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--recreate", action="store_true",
                    help="delete this table's chain first, then rebuild")
    args = ap.parse_args(argv)

    sn = ServiceNow()
    have = existing(sn)

    if args.recreate and have and not args.dry_run:
        for name, r in have.items():
            sn.delete("sys_report_map_source", r["sys_id"])
            print(f"  deleted {name!r}")
        have = {}

    print(f"creating map source chain for {TABLE} via {REF}\n")
    parent_id = None
    root_id = None
    ok = True

    for level, label, mapping, join_by, aggregate, why in CHAIN:
        name = ROOT_NAME if level is None else f"{ROOT_NAME} ({label})"
        body = {
            "name": name,
            "table": TABLE,
            "aggregate_field": aggregate,
            "active": "true",
        }
        if parent_id:
            body["parent"] = parent_id
        if level is not None:
            body["type"] = LEVEL[level]
            body["type_mapping"] = mapping
            body["join_by_column"] = join_by

        if name in have:
            rec = have[name]
            print(f"  = {name!r} exists")
        elif args.dry_run:
            print(f"  + would create {name!r}  {why}")
            rec = None
        else:
            try:
                rec = sn.insert("sys_report_map_source", body)
                print(f"  + created {name!r}  ({why})")
            except SNError as exc:
                print(f"  x {name!r} failed: {exc}")
                ok = False
                break

        if rec:
            # The parent business rule means a 200 does NOT prove success --
            # re-read and check the mapping actually landed.
            back = sn.query("sys_report_map_source", encoded_query=f"sys_id={rec['sys_id']}",
                            fields=SHOW, limit=1)
            if back:
                got = back[0]
                print(f"      {show(got)}")
                if level is not None and not flat(got.get("type_mapping")):
                    print("      !! type_mapping did NOT persist -- parent rule not satisfied")
                    ok = False
            parent_id = rec["sys_id"]
            if level is None:
                root_id = rec["sys_id"]

    print()
    if not ok:
        print("RESULT: chain incomplete -- inspect above")
        return 1
    print(f"RESULT: chain ready. Root map source: {root_id}")
    if root_id:
        print(f"  Use this as the Geomap datasource mapping; mapSysId stays the")
        print(f"  World basemap 93b8a3a2d7101200bd4a4ebfae61033a (sys_report_map).")
    print("\nVerify the counts the map will show:")
    for q, label in ((f"{REF}ISNOTEMPTY", "rows with a location reference"),
                     ("u_latISNOTEMPTY^u_lngISNOTEMPTY", "rows with raw coordinates")):
        print(f"  {label:<34} {sn.count(TABLE, encoded_query=q)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
