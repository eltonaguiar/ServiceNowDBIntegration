#!/usr/bin/env python3
"""
test_sync.py -- behavioural test suite for the idempotent events sync.

    python tests/test_sync.py --size 200

Runs against a REAL instance, so it clears the target table first. Every test
asserts on observed state read back from ServiceNow, not on what the script
claims it did.

The question this suite exists to answer: *does the primary key actually guard,
or does the same event repeat?* Tests 3, 5 and 7 answer it three different ways.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from sn_api import SNError, ServiceNow  # noqa: E402

TARGET = "u_toronto_event"
KEY = "u_event_id"

PASSED: list[str] = []
FAILED: list[str] = []


# ------------------------------------------------------------------- helpers


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAILED.append(name)
        print(f"  FAIL  {name}  {detail}")


def clear_target(sn: ServiceNow, workers: int = 20) -> int:
    """Delete every row. Parallel: one-at-a-time is ~166ms/row on a PDI."""
    removed = 0
    while True:
        rows = sn.query(TARGET, fields=["sys_id"], limit=1000)
        if not rows:
            break

        def drop(sys_id: str) -> int:
            try:
                sn.delete(TARGET, sys_id)
                return 1
            except SNError:
                return 0

        with ThreadPoolExecutor(max_workers=workers) as pool:
            removed += sum(pool.map(drop, [r["sys_id"] for r in rows]))
    return removed


def key_counts(sn: ServiceNow) -> Counter:
    counter: Counter = Counter()
    offset = 0
    while True:
        rows = sn.query(TARGET, fields=["sys_id", KEY], limit=1000, offset=offset, order_by="sys_id")
        if not rows:
            break
        for row in rows:
            counter[row.get(KEY)] += 1
        offset += 1000
        if len(rows) < 1000:
            break
    return counter


def duplicates(sn: ServiceNow) -> dict[str, int]:
    return {k: v for k, v in key_counts(sn).items() if v > 1 and k}


def run_import(source: Path, *extra: str) -> dict:
    cmd = [sys.executable, str(REPO / "import_events.py"), "--source", str(source), *extra]
    started = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO))
    elapsed = time.time() - started
    if proc.returncode not in (0, 1):
        print(proc.stdout[-2000:])
        print(proc.stderr[-2000:])
        raise RuntimeError(f"import failed rc={proc.returncode}")
    summary = {}
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if "===" in stripped or not stripped:
            continue
        parts = stripped.split()
        if len(parts) >= 2 and parts[0] in {
            "inserted", "updated", "unchanged", "errors",
            "duplicates_removed", "swept", "target_rows_final", "fetched",
        }:
            try:
                summary[parts[0]] = int(parts[1])
            except ValueError:
                pass
    summary["_elapsed"] = round(elapsed, 1)
    summary["_rc"] = proc.returncode
    return summary


def write_source(records: list[dict], path: Path) -> Path:
    path.write_text(json.dumps(records), encoding="utf-8")
    return path


def fetch_feed(limit: int) -> list[dict]:
    url = "https://findtorontoevents.ca/events.json"
    req = urllib.request.Request(url, headers={"User-Agent": "sn-sync-tests/1.0"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read())
    return data[:limit]


# --------------------------------------------------------------------- tests


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", type=int, default=200, help="records per test run")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args(argv)

    sn = ServiceNow()
    workdir = Path("/tmp/sn-sync-tests")
    workdir.mkdir(exist_ok=True)

    print(f"instance : {sn.instance}")
    print(f"target   : {TARGET}")
    print(f"size     : {args.size} records/run\n")

    print("[setup] fetching feed + clearing target")
    feed = fetch_feed(args.size)
    subset = write_source(feed, workdir / "subset.json")
    cleared = clear_target(sn)
    print(f"        {len(feed)} records prepared, {cleared} old rows removed\n")

    # ---------------------------------------------------------------- test 1
    print("[1] dry run writes nothing")
    before = sn.count(TARGET)
    r = run_import(subset, "--dry-run")
    after = sn.count(TARGET)
    check("dry run leaves table untouched", before == after == 0, f"{before} -> {after}")

    # ---------------------------------------------------------------- test 2
    print("\n[2] first import inserts every record")
    r = run_import(subset, "--workers", str(args.workers))
    rows = sn.count(TARGET)
    unique_keys = len({rec["id"] for rec in feed})
    check("inserted count matches feed", r.get("inserted", -1) == len(feed), f"inserted={r.get('inserted')}")
    check("target row count matches feed", rows == len(feed), f"rows={rows} expected={len(feed)}")
    check("no duplicates after first run", not duplicates(sn), f"dupes={duplicates(sn)}")
    check("key set matches feed exactly", set(key_counts(sn)) == {rec["id"] for rec in feed})

    # ---------------------------------------------------------------- test 3
    print("\n[3] second import is a no-op (the core idempotency test)")
    r = run_import(subset, "--workers", str(args.workers))
    rows2 = sn.count(TARGET)
    check("zero inserts on repeat", r.get("inserted", -1) == 0, f"inserted={r.get('inserted')}")
    check("every row reported unchanged", r.get("unchanged", -1) == len(feed), f"unchanged={r.get('unchanged')}")
    check("row count unchanged", rows2 == len(feed), f"{rows} -> {rows2}")
    check("no duplicates after repeat", not duplicates(sn), f"dupes={duplicates(sn)}")

    # ---------------------------------------------------------------- test 4
    print("\n[4] third import still a no-op (guards against drift)")
    r = run_import(subset, "--workers", str(args.workers))
    check("still zero inserts on third run", r.get("inserted", -1) == 0, f"inserted={r.get('inserted')}")
    check("still zero updates on third run", r.get("updated", -1) == 0, f"updated={r.get('updated')}")
    check("row count stable", sn.count(TARGET) == len(feed))

    # ---------------------------------------------------------------- test 5
    print("\n[5] changed field updates in place, same sys_id")
    sample = next(r for r in feed if r.get("id"))
    original = sn.query(TARGET, encoded_query=f"{KEY}={original_key(sample)}",
                        fields=["sys_id", "u_title"], limit=1)
    sys_id_before = original[0]["sys_id"]
    mutated = json.loads(json.dumps(feed))
    for rec in mutated:
        if rec.get("id") == sample["id"]:
            rec["title"] = "MUTATED TITLE FOR TEST"
            break
    mutated_path = write_source(mutated, workdir / "mutated.json")
    r = run_import(mutated_path, "--workers", str(args.workers))
    after_row = sn.query(TARGET, encoded_query=f"{KEY}={original_key(sample)}",
                         fields=["sys_id", "u_title"], limit=1)
    check("exactly one update", r.get("updated", -1) == 1, f"updated={r.get('updated')}")
    check("zero inserts", r.get("inserted", -1) == 0, f"inserted={r.get('inserted')}")
    check("sys_id preserved (no delete+insert)", after_row[0]["sys_id"] == sys_id_before)
    check("title actually changed", after_row[0]["u_title"] == "MUTATED TITLE FOR TEST",
          f"got={after_row[0]['u_title']!r}")
    check("row count still matches", sn.count(TARGET) == len(feed))

    # ---------------------------------------------------------------- test 6
    print("\n[6] duplicate key inside ONE batch converges to one row")
    dupes_payload = [
        {"id": "dup-key-1", "title": "First copy", "status": "UPCOMING"},
        {"id": "dup-key-1", "title": "Second copy", "status": "UPCOMING"},
        {"id": "dup-key-2", "title": "Unique one", "status": "UPCOMING"},
    ]
    dupe_path = write_source(dupes_payload, workdir / "dupes.json")
    run_import(dupe_path, "--no-sweep", "--workers", "3")
    counts = key_counts(sn)
    check("duplicate key stored once", counts.get("dup-key-1") == 1, f"count={counts.get('dup-key-1')}")
    check("sibling key stored once", counts.get("dup-key-2") == 1, f"count={counts.get('dup-key-2')}")

    # ---------------------------------------------------------------- test 7
    print("\n[7] stale sweep deletes rows absent from the feed")
    clear_target(sn)
    run_import(subset, "--workers", str(args.workers))
    baseline = sn.count(TARGET)
    reduced = feed[:-1]                      # drop the last record
    reduced_path = write_source(reduced, workdir / "reduced.json")
    r = run_import(reduced_path, "--workers", str(args.workers))
    after = sn.count(TARGET)
    check("sweep reported one deletion", r.get("swept", -1) == 1, f"swept={r.get('swept')}")
    check("row count dropped by exactly one", after == baseline - 1, f"{baseline} -> {after}")
    check("dropped key is gone", feed[-1]["id"] not in key_counts(sn))

    # ---------------------------------------------------------------- test 8
    print("\n[8] concurrent runs do not corrupt the key set")
    clear_target(sn)
    cmd = [sys.executable, str(REPO / "import_events.py"), "--source", str(subset),
           "--workers", str(args.workers), "--batch-id"]
    p1 = subprocess.Popen(cmd + ["race-a"], cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    p2 = subprocess.Popen(cmd + ["race-b"], cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    p1.communicate(timeout=900)
    p2.communicate(timeout=900)
    race_counts = key_counts(sn)
    race_dupes = {k: v for k, v in race_counts.items() if v > 1 and k}
    check("key set still matches feed after race",
          set(race_counts) == {rec["id"] for rec in feed},
          f"{len(race_counts)} keys vs {len(feed)} expected")
    check("dedupe guard removed race duplicates", not race_dupes, f"dupes={race_dupes}")
    check("race produced no orphan rows", sn.count(TARGET) == len(feed), f"rows={sn.count(TARGET)}")

    # ---------------------------------------------------------------- test 9
    print("\n[9] final integrity check on a real subset")
    final_counts = key_counts(sn)
    check("every key appears exactly once",
          all(v == 1 for v in final_counts.values()),
          f"{sum(1 for v in final_counts.values() if v != 1)} key(s) off")
    check("row count == unique feed keys", sn.count(TARGET) == len({r['id'] for r in feed}))

    # -------------------------------------------------------------- summary
    print("\n" + "=" * 66)
    print(f"PASSED {len(PASSED)}   FAILED {len(FAILED)}")
    if FAILED:
        for name in FAILED:
            print(f"  FAILED: {name}")
        return 1
    print("ALL TESTS PASSED")
    return 0


def original_key(record: dict) -> str:
    return record["id"]


if __name__ == "__main__":
    sys.exit(main())
