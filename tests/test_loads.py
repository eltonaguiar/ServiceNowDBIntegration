#!/usr/bin/env python3
"""
test_loads.py -- repeated-load harness: does the row count converge, and does
                 any event appear twice?

    python tests/test_loads.py --size 100 --loads 5

Runs N loads of the same payload back to back and, after every load, measures
the target table independently of what the importer reported:

  * total rows
  * distinct u_event_id
  * duplicate rows (same event id more than once)
  * orphan rows (blank / null event id)

Then it perturbs the payload -- adds a new event, changes one, deletes one --
and measures again. This is the "does the primary key guard" test in its most
direct form: an event id must never occupy more than one row, no matter how many
times we load it.

Exit code 0 = every load was clean.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from sn_api import SNError, ServiceNow  # noqa: E402

TARGET = "u_toronto_event"
KEY = "u_event_id"

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))
    if not ok:
        FAILURES.append(name)


def table_state(sn: ServiceNow) -> dict:
    """Independently measure the target table. Never trust the importer's report."""
    counts: Counter = Counter()
    blank = 0
    rows = 0
    offset = 0
    while True:
        page = sn.query(TARGET, fields=["sys_id", KEY], limit=1000, offset=offset, order_by="sys_id")
        if not page:
            break
        for row in page:
            rows += 1
            key = row.get(KEY)
            if key:
                counts[key] += 1
            else:
                blank += 1
        offset += 1000
        if len(page) < 1000:
            break
    dupes = {k: v for k, v in counts.items() if v > 1}
    return {
        "rows": rows,
        "distinct_ids": len(counts),
        "duplicate_ids": len(dupes),
        "duplicate_rows": sum(v - 1 for v in dupes.values()),
        "blank_ids": blank,
        "counts": counts,
    }


def clear(sn: ServiceNow) -> int:
    from concurrent.futures import ThreadPoolExecutor

    removed = 0
    while True:
        page = sn.query(TARGET, fields=["sys_id"], limit=1000)
        if not page:
            break

        def drop(sys_id: str) -> int:
            try:
                sn.delete(TARGET, sys_id)
                return 1
            except SNError:
                return 0

        with ThreadPoolExecutor(max_workers=20) as pool:
            removed += sum(pool.map(drop, [r["sys_id"] for r in page]))
    return removed


def run_import(source: Path, batch: str) -> dict:
    cmd = [
        sys.executable, str(REPO / "import_events.py"),
        "--source", str(source), "--workers", "8", "--batch-id", batch,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO))
    summary: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0] in {
            "inserted", "updated", "unchanged", "duplicates_removed", "swept", "errors",
        }:
            try:
                summary[parts[0]] = int(parts[1])
            except ValueError:
                pass
    summary["_rc"] = proc.returncode
    return summary


def show(load: str, imp: dict, state: dict, expected_unique: int) -> None:
    print(
        f"  {load:<12} importer[ins={imp.get('inserted','?'):>4} upd={imp.get('updated','?'):>3} "
        f"unch={imp.get('unchanged','?'):>4} dedup={imp.get('duplicates_removed','?'):>2} "
        f"swept={imp.get('swept','?'):>2}]  "
        f"table[rows={state['rows']:>5} distinct={state['distinct_ids']:>5} "
        f"dup_ids={state['duplicate_ids']} blank={state['blank_ids']}]  "
        f"expected={expected_unique}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", type=int, default=100)
    parser.add_argument("--loads", type=int, default=5)
    args = parser.parse_args(argv)

    sn = ServiceNow()
    workdir = Path("/tmp/sn-load-tests")
    workdir.mkdir(exist_ok=True)

    with urllib.request.urlopen(
        urllib.request.Request(
            "https://findtorontoevents.ca/events.json",
            headers={"User-Agent": "sn-load-tests/1.0"},
        ),
        timeout=180,
    ) as resp:
        feed = json.loads(resp.read())

    # take a slice, then de-duplicate it so "expected unique" is unambiguous
    seen: set[str] = set()
    payload: list[dict] = []
    for record in feed:
        key = record.get("id")
        if key and key not in seen:
            seen.add(key)
            payload.append(record)
        if len(payload) >= args.size:
            break

    source = workdir / "payload.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    expected = len(payload)

    print(f"instance : {sn.instance}")
    print(f"target   : {TARGET}")
    print(f"payload  : {expected} unique events, {args.loads} consecutive loads\n")

    print("[setup] clearing target")
    print(f"        removed {clear(sn)} row(s)\n")

    print(f"[phase 1] {args.loads} identical loads -- count must never grow")
    for index in range(1, args.loads + 1):
        imp = run_import(source, f"load-{index}")
        state = table_state(sn)
        show(f"load {index}", imp, state, expected)

        check(f"load {index}: rows == {expected}", state["rows"] == expected, f"rows={state['rows']}")
        check(f"load {index}: no duplicate event ids", state["duplicate_ids"] == 0,
              f"dup_ids={state['duplicate_ids']} dup_rows={state['duplicate_rows']}")
        check(f"load {index}: distinct ids == {expected}", state["distinct_ids"] == expected)
        check(f"load {index}: no blank event ids", state["blank_ids"] == 0)
        if index > 1:
            check(f"load {index}: zero inserts on repeat", imp.get("inserted") == 0,
                  f"inserted={imp.get('inserted')}")
            check(f"load {index}: all rows unchanged", imp.get("unchanged") == expected,
                  f"unchanged={imp.get('unchanged')}")

    print("\n[phase 2] payload grows by one new event")
    grown = payload + [{
        "id": "zz-brand-new-event-for-test",
        "title": "Brand new event",
        "status": "UPCOMING",
        "date": "2026-12-01T19:00:00Z",
    }]
    grown_path = workdir / "grown.json"
    grown_path.write_text(json.dumps(grown), encoding="utf-8")
    imp = run_import(grown_path, "grown")
    state = table_state(sn)
    show("grow +1", imp, state, expected + 1)
    check("exactly one insert for the new event", imp.get("inserted") == 1, f"inserted={imp.get('inserted')}")
    check("row count increased by one", state["rows"] == expected + 1, f"rows={state['rows']}")
    check("still no duplicates", state["duplicate_ids"] == 0)

    print("\n[phase 3] same payload again -- new event must not re-insert")
    imp = run_import(grown_path, "grown-repeat")
    state = table_state(sn)
    show("repeat", imp, state, expected + 1)
    check("zero inserts on repeat of grown payload", imp.get("inserted") == 0,
          f"inserted={imp.get('inserted')}")

    print("\n[phase 4] payload shrinks -- stale sweep must remove the dropped event")
    imp = run_import(source, "shrunk")
    state = table_state(sn)
    show("shrink -1", imp, state, expected)
    check("sweep removed exactly one", imp.get("swept") == 1, f"swept={imp.get('swept')}")
    check("row count back to payload size", state["rows"] == expected, f"rows={state['rows']}")
    check("dropped event is gone", "zz-brand-new-event-for-test" not in state["counts"])

    print("\n[phase 5] final duplicate scan")
    state = table_state(sn)
    worst = sorted(state["counts"].items(), key=lambda kv: -kv[1])[:5]
    print(f"  top event-id frequencies: {worst}")
    check("every event id appears exactly once", all(v == 1 for v in state["counts"].values()),
          f"{sum(1 for v in state['counts'].values() if v != 1)} id(s) not exactly 1")
    check("total rows == distinct event ids", state["rows"] == state["distinct_ids"],
          f"rows={state['rows']} distinct={state['distinct_ids']}")

    print("\n" + "=" * 62)
    if FAILURES:
        print(f"FAILED {len(FAILURES)} check(s):")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("ALL LOAD TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
