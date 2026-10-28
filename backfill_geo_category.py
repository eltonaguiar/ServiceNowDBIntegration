#!/usr/bin/env python3
"""
backfill_geo_category.py -- add the two derived columns the dashboard needs.

    python backfill_geo_category.py --dry-run
    python backfill_geo_category.py
    python backfill_geo_category.py --limit 200      # small trial run

Why this exists
---------------
Two things the user asked for are impossible against the raw staging table:

**1. Category filtering.** `u_categories` holds a CSV such as
`"Dating,Music,Arts,Nightlife"`. Grouping or filtering on it produces 221
distinct combinations for 11,940 events -- useless as a filter. The first token
gives 11 clean buckets (Arts 4,475 / General 3,042 / Sports 1,266 / Community
936 / Nightlife 781 / Music 624 / Dating 384 / Business 310 / Food & Drink 101 /
Toronto 17 / Family 4). So we add `u_primary_category` and keep `u_categories`
untouched for full-text search.

**2. A geo map.** This instance's Geomap plots *location reference chains*, not
raw coordinate columns: all 52 native `sys_report_map_source` records walk a
reference (e.g. `cmdb_ci.location.city`) and none reads a lat/lng column pair.
So `u_lat` / `u_lng` alone cannot feed it. We therefore:

    - create one `cmn_location` record per distinct venue that has coordinates
      (440 of them), storing latitude/longitude on the location record
    - add `u_location_ref` -> cmn_location on the events table
    - backfill the reference on the 9,599 events that carry coordinates
    - create a `sys_report_map_source` (type_mapping=use_lat_lon) for our table

Only then can a Geomap widget render real points.

Idempotency
-----------
Safe to re-run. Columns are created only if absent; locations are matched on
(name, latitude, longitude) and reused; events are only PATCHed when a value is
missing. `--dry-run` performs no ServiceNow writes at all.
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Tuple

from sn_api import SNError, ServiceNow

EVENTS = "u_toronto_event"
CAT_FIELD = "u_primary_category"
REF_FIELD = "u_location_ref"
LOC_TABLE = "cmn_location"

# Copied from the native "Problem on CI asset location (level 3)" map source.
MAP_TYPE_USE_LAT_LON = "23fdd872d7101200bd4a4ebfae610340"
MAP_JOIN_BY = "hc-key"


# --------------------------------------------------------------- columns
def ensure_category_column(sn: ServiceNow, dry: bool) -> None:
    have = {c["element"] for c in sn.columns(EVENTS)}
    if CAT_FIELD in have:
        print(f"  = {CAT_FIELD} already exists")
        return
    print(f"  + creating {CAT_FIELD} (string 50)")
    if dry:
        return
    sn.create_column(EVENTS, CAT_FIELD, "Primary Category", "string", max_length=50)


def ensure_reference_column(sn: ServiceNow, dry: bool) -> None:
    """Reference columns need `reference` set on the dictionary row, which
    sn_api.create_column() does not expose, so insert the metadata directly."""
    have = {c["element"] for c in sn.columns(EVENTS)}
    if REF_FIELD in have:
        print(f"  = {REF_FIELD} already exists")
        return
    print(f"  + creating {REF_FIELD} (reference -> {LOC_TABLE})")
    if dry:
        return
    sn.insert("sys_dictionary", {
        "name": EVENTS,
        "element": REF_FIELD,
        "column_label": "Location",
        "internal_type": "reference",
        "reference": LOC_TABLE,
        "max_length": "32",
        "active": "true",
    })


# --------------------------------------------------------------- data
def scan_events(sn: ServiceNow, limit: int = 0) -> List[dict]:
    """Every event, with just the fields the backfill needs."""
    fields = ["sys_id", CAT_FIELD, REF_FIELD, "u_categories",
              "u_location", "u_lat", "u_lng"]
    out: List[dict] = []
    offset = 0
    while True:
        page = sn.query(EVENTS, fields=fields, limit=2000, offset=offset)
        if not page:
            break
        out.extend(page)
        offset += 2000
        if limit and len(out) >= limit:
            return out[:limit]
        if len(page) < 2000:
            break
    return out


def primary_category(categories: str) -> str:
    """First CSV token, or '' when the field is empty."""
    c = (categories or "").strip()
    return c.split(",")[0].strip()[:50] if c else ""


def venues(events: List[dict]) -> Dict[str, Tuple[str, str]]:
    """venue name -> (lat, lng) for every event that has all three."""
    out: Dict[str, Tuple[str, str]] = {}
    for e in events:
        name = (e.get("u_location") or "").strip()
        lat, lng = (e.get("u_lat") or "").strip(), (e.get("u_lng") or "").strip()
        if name and lat and lng and name not in out:
            out[name] = (lat, lng)
    return out


def existing_locations(sn: ServiceNow) -> Dict[Tuple[str, str, str], str]:
    """(name, lat, lng) -> sys_id, so a re-run never duplicates a venue."""
    out: Dict[Tuple[str, str, str], str] = {}
    offset = 0
    while True:
        page = sn.query(LOC_TABLE, fields=["sys_id", "name", "latitude", "longitude"],
                        limit=1000, offset=offset)
        if not page:
            break
        for r in page:
            key = ((r.get("name") or "").strip(),
                   (r.get("latitude") or "").strip(),
                   (r.get("longitude") or "").strip())
            out[key] = r["sys_id"]
        offset += 1000
        if len(page) < 1000:
            break
    return out


def create_locations(sn: ServiceNow, wanted: Dict[str, Tuple[str, str]], dry: bool) -> Dict[str, str]:
    """venue name -> cmn_location sys_id, creating what is missing."""
    have = existing_locations(sn)
    name_to_id: Dict[str, str] = {}
    for (nm, _lat, _lng), sid in have.items():
        name_to_id.setdefault(nm, sid)

    created = reused = 0
    for name, (lat, lng) in wanted.items():
        key = (name, lat, lng)
        if key in have:
            name_to_id[name] = have[key]
            reused += 1
            continue
        if name in name_to_id:
            # Same venue name, different coordinates; reuse the existing record
            # rather than creating a near-duplicate.
            reused += 1
            continue
        if dry:
            name_to_id[name] = "<would-create>"
            created += 1
            continue
        rec = {
            "name": name[:100],
            "latitude": lat,
            "longitude": lng,
            "city": "Toronto",
            "state": "ON",
            "country": "Canada",
        }
        try:
            sid = sn.insert(LOC_TABLE, rec)["sys_id"]
            name_to_id[name] = sid
            created += 1
        except SNError as exc:
            print(f"      x {name[:40]}: {exc}")
    print(f"      created {created}, reused {reused}")
    return name_to_id


def apply_updates(sn: ServiceNow, events: List[dict], name_to_id: Dict[str, str],
                  workers: int, dry: bool) -> Tuple[int, int, int, int]:
    """PATCH only the rows that actually need a change.

    Returns (would_update, done, failed, skipped).
    """
    todo = []
    for e in events:
        patch = {}
        want_cat = primary_category(e.get("u_categories"))
        if want_cat and (e.get(CAT_FIELD) or "") != want_cat:
            patch[CAT_FIELD] = want_cat
        name = (e.get("u_location") or "").strip()
        if name and (e.get("u_lat") or "").strip() and (e.get("u_lng") or "").strip():
            target = name_to_id.get(name)
            if target and (e.get(REF_FIELD) or "") != target:
                patch[REF_FIELD] = target
        if patch:
            todo.append((e["sys_id"], patch))

    print(f"      {len(todo)} row(s) need updating, {len(events) - len(todo)} already correct")
    if dry or not todo:
        return len(todo), 0, 0, len(events) - len(todo)

    done = failed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(sn.update, EVENTS, sid, patch): sid for sid, patch in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                fut.result()
                done += 1
            except SNError as exc:
                failed += 1
                if failed <= 5:
                    print(f"      x {futs[fut]}: {exc}")
            if i % 1000 == 0:
                print(f"      ... {i}/{len(todo)}")
    return len(todo), done, failed, len(events) - len(todo)


# --------------------------------------------------------------- map source
def ensure_map_source(sn: ServiceNow, dry: bool) -> str | None:
    name = "Toronto events by venue"
    rows = sn.query("sys_report_map_source", encoded_query=f"name={name}",
                    fields=["sys_id"], limit=1)
    if rows:
        print(f"  = map source exists ({rows[0]['sys_id']})")
        return rows[0]["sys_id"]
    print(f"  + creating map source {name!r} (use_lat_lon over {REF_FIELD})")
    if dry:
        return None
    rec = {
        "name": name,
        "table": EVENTS,
        "aggregate_field": REF_FIELD,
        "type": MAP_TYPE_USE_LAT_LON,
        "type_mapping": "use_lat_lon",
        "join_by_column": MAP_JOIN_BY,
        "active": "true",
    }
    try:
        sid = sn.insert("sys_report_map_source", rec)["sys_id"]
    except SNError as exc:
        print(f"      x map source failed: {exc}")
        return None
    print(f"      created {sid}")
    return sid


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="no ServiceNow writes")
    ap.add_argument("--limit", type=int, default=0, help="only process N events")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--skip-map-source", action="store_true")
    args = ap.parse_args(argv)

    sn = ServiceNow()
    dry = args.dry_run

    print("[1/5] columns")
    ensure_category_column(sn, dry)
    ensure_reference_column(sn, dry)

    print("[2/5] reading events")
    events = scan_events(sn, args.limit)
    print(f"      {len(events)} event(s)")
    if not events:
        print("nothing to do")
        return 0

    print("[3/5] venues with coordinates")
    v = venues(events)
    print(f"      {len(v)} distinct venue(s)")

    print(f"[4/5] {LOC_TABLE} records")
    name_to_id = create_locations(sn, v, dry)

    print("[5/5] backfilling events")
    would, done, failed, skipped = apply_updates(sn, events, name_to_id, args.workers, dry)
    if dry:
        print(f"      would update {would}, already correct {skipped}")
    else:
        print(f"      updated {done}, failed {failed}, already correct {skipped}")

    if not args.skip_map_source:
        print("\nmap source")
        ensure_map_source(sn, dry)

    print("\nRESULT:", "DRY RUN -- nothing written" if dry else "backfill complete")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
