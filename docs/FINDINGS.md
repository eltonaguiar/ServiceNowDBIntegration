# Findings: what testing actually showed

This file records the claims that turned out to be **wrong**, the defects the
tests caught, and the evidence. Everything below was measured on a live
instance; nothing is inferred.

---

## Part 1 — Three claims I made that were incorrect

### ❌ Claim: "`sys_import_set_row` is a single staging table shared by every import set, so parallelism corrupts it."

**Verdict: false.**

The platform's own property description
(`glide.import_set_insert_serialized_when_no_coalesce`, value `false`) says:

> Controls the processing of web service import sets which have no coalesce
> field(s) defined. When this property is set to false (default), the instance
> will perform transformations **concurrently** from the source to the target
> table. When this property is set to true, the instance will perform
> transformations **one at a time** for a given staging table. This property can
> be overridden by the table-specific property
> `glide.import_set_insert_serialized.<table name>`.

So **concurrent transformation is the default and supported behaviour.**
Serialization is an opt-in safety valve, and it only applies to import sets with
**no coalesce fields defined** — precisely the case where two threads could
insert rows that nothing can deduplicate afterwards.

Correct statement: *concurrency is normal; if you have no coalesce field you may
want `glide.import_set_insert_serialized.<table>=true` to avoid duplicate
inserts.* Nothing "corrupts" anything.

### ❌ Claim: "`glide.import_set.target.stop_transform = true` means one bad field mapping aborts the entire transform."

**Verdict: false — and the real meaning matters for idempotency.**

The shipped description on the instance (value `true`):

> During a transformation via transform maps, if enabled, if **multiple target
> records match a source row for all coalesce fields**, will ignore the row.

It is not an error threshold. It controls what happens when coalesce is
*ambiguous* — when a source row would match more than one existing target row.
With it enabled, the row is **skipped** rather than applied to an arbitrary
match.

This is directly relevant to idempotency: it is the platform's defence against
duplicating or arbitrarily overwriting when the target already contains
duplicates from an earlier bad run.

### ❌ Claim: "Import Set REST API reachable — HTTP 400 on empty body means the handler is live."

**Verdict: false. The API does not exist on this instance.**

```
POST /api/now/import/incident        -> 405  "POST method not supported for API"
POST /api/now/import/u_ev_stage      -> 405  "POST method not supported for API"
POST /api/now/import/u_toronto_event -> 405  "POST method not supported for API"
GET  (any of the above)              -> 400  "Invalid table import"
```

No `sys_ws_definition` record with `service_id = import` exists. The earlier 400
was `Invalid table import` — the endpoint was never there. I misread a 400 as
"handler reached" when it means "no such API".

**Consequence:** the REST Import Set push (Option 4 in the README) is **not
available here**. A push-based sync must go straight at the Table API.

---

## Part 2 — Why the design changed

| Assumption | Reality | Result |
|---|---|---|
| Push chunks into a staging table via Import Set API | 405 — API absent | Push directly to the target table |
| Transform-map coalesce handles identity | No transform map reachable without an import set | Enforce identity in the client |
| Import Sets give me the platform's upsert | Only via the UI-driven data source path | Implement upsert explicitly |

So `import_events.py` performs a **merge/upsert by natural key**, which is the
same semantics a transform-map coalesce would give — and which is what an ETL
tool (Fivetran / Airbyte "merge" mode) does when the target has no constrained
unique key.

---

## Part 3 — Defects the tests caught

### Defect 1 — Silent truncation caused permanent re-writes

`u_title` was created as `string(255)`. The feed contains a **429-character**
title. ServiceNow does not error on overflow; it truncates. The stored value then
differs from the source on every run, so that row was "updated" forever and the
sync could never converge.

```
DRIFT key=1d900dd2a9f0311a107bbe4a84e19e20 field=u_title
      want 429 chars
      got  255 chars
```

**Detection:** repeat runs reported `unchanged=199 updated=1` instead of
`unchanged=200`.

**Fix:** measure the real maximum length per field, widen the column
(`u_title` → 500 via `create_table.py --fix-drift`), and add a **preflight** to
the importer that refuses to run when any value exceeds its column.

### Defect 2 — Float last-digit instability

15 of 11,940 rows disagreed in the 6th decimal:

```
want='-79.255913'   got='-79.255914'
```

The column stores a `float`; the stored representation differs from the source
in the last digit, so string comparison never matched.

**Fix:** round numeric values to the column's storage precision before writing,
and compare at that precision rather than as strings.

### Defect 3 — Duplicate source keys picked a different winner each run

The feed contains **5 duplicate `id` values** with *different* payloads. Whichever
copy was applied last won, so the next run compared the stored row against the
other copy and "fixed" it back — a permanent 2-cycle with no convergence.

```
u_last_updated: want='2026-07-06 13:48:18' got='2026-07-11 12:16:50'
u_last_updated: want='2026-07-12 12:50:23' got='2026-07-14 13:37:21'
```

**Fix:** canonicalise the source before applying — one record per key, winner =
newest `last_updated`. The choice is now deterministic, so the row stops moving.

### Defect 4 — Decimal column precision

`u_price_amount` is `decimal` with `max_length 15`, which stores **two decimal
places**. A source value with more precision rounded on write and drifted. Now
rounded and compared at 2 dp.

---

## Part 4 — Evidence

### Behavioural suite — 27 assertions

```
python tests/test_sync.py --size 200 --workers 6

PASSED 27   FAILED 0
ALL TESTS PASSED
```

| # | Test | Proves |
|---|---|---|
| 1 | dry run writes nothing | `--dry-run` is safe |
| 2 | first import inserts every record | baseline load |
| 3 | **second import is a no-op** | **the key guards — 0 inserts, 200 unchanged** |
| 4 | third import still a no-op | no drift between runs |
| 5 | changed field updates in place | 1 update, `sys_id` preserved, no delete+insert |
| 6 | duplicate key in one batch | converges to 1 row |
| 7 | stale sweep | 200 → 199, dropped key gone |
| 8 | **two concurrent full runs** | key set correct, dedupe removed race duplicates |
| 9 | final integrity | every key appears exactly once |

### Full-scale runs — 11,945 records

| Run | Batch | inserted | updated | unchanged | duplicates removed | final rows | elapsed |
|---|---|---|---|---|---|---|---|
| 1 | `full-1` | **11,945** | 0 | 0 | 5 | 11,940 | 230 s |
| 2 | `full-2` | 0 | 20 | 11,925 | 0 | 11,940 | 40 s |
| 3 | `full-3` | 0 | 17 | 11,923 | 0 | 11,940 | 36 s |
| 4 | `full-4` | **0** | **0** | **11,940** | 0 | 11,940 | 36 s |
| 5 | `full-5` | **0** | **0** | **11,940** | 0 | 11,940 | 36 s |

Run 1 → 11,945 inserts, then the dedupe guard removed exactly the 5 duplicate
keys → **11,940 rows = 11,940 unique source keys**. Runs 2 and 3 were the
convergence work (fixing defects 2 and 3). Runs 4 and 5 are the steady state:
**zero writes**, repeatedly.

### What "the same event repeats" was tested three ways

1. **Sequential repeats** (tests 3, 4; runs 4, 5) — zero inserts.
2. **Duplicate inside one batch** (test 6) — one row survives.
3. **Simultaneous runs** (test 8) — two processes importing the same 200 records
   at once; the dedupe guard converged the table back to exactly 200.

That third case is the honest one: with no unique constraint available on a
custom field, two concurrent runs *can* both insert. The guarantee is therefore
"converges", not "never races" — enforced by the dedupe pass, and verified.

---

## Part 5 — Performance note

One REST call costs ~166 ms on this instance. 11,945 sequential writes would take
~33 minutes; 10 workers brings the first load to **230 seconds**. Steady-state
runs are read-dominated and take ~36 s.

`clear_target` in the test suite originally deleted one row at a time, which made
a 11,940-row cleanup take longer than the tests themselves — now parallelised.

---

## Part 6 — Standing rules that came out of this

1. **Never assume an API exists because an endpoint returns a 4xx.** 400 and 405
   both mean "nothing here"; read the message.
2. **Read the property description, not the property name.** `stop_transform`
   sounds like error handling and is not.
3. **A sync that "updates" rows on a no-op run is broken**, even though nothing
   errors. Assert `unchanged == total` on the second run, and treat any other
   result as a defect.
4. **Widths must be measured from the data**, not guessed. `255` is a habit, not
   a measurement.
5. **Sort out duplicate source keys before comparing.** Otherwise the winner is
   chosen by feed order and the row oscillates forever.

---

## Part 7 — The original plan vs what was actually built

### Original plan (as designed before touching the instance)

1. **Transport: Option 3** — a JSON *data source* with `file_retrieval_method=HTTPS`
   and `Use MID Server=gx10-mid`, pulling `events.json` nightly.
2. **Landing:** Import Set staging table → transform map → `u_toronto_event`.
3. **Idempotency:** transform-map **coalesce** on `u_event_id`, which is the
   platform-native upsert. `glide.import_set.target.stop_transform=true` was
   believed to abort bad rows, so mappings would need to be right first.
4. **Chunking:** push chunks via `POST /api/now/import/<staging>` if volume was a
   problem.
5. **Scheduling:** `scheduled_import_set` with `time_zone=America/Toronto`.

### What actually happened

| # | Plan | Reality | Consequence |
|---|---|---|---|
| 1 | Import Set API available | **405 — the API does not exist** on this instance | Option 4 and chunked push both dropped |
| 2 | Transform-map coalesce | unreachable without an import set | identity moved into the client |
| 3 | `stop_transform` guards bad rows | it only guards *ambiguous coalesce matches* | the real guard had to be a preflight + round-trip test |
| 4 | `sys_import_set_row` shared → no parallelism | concurrency is the **default**; serialization is opt-in | "sequential only" was withdrawn |
| 5 | Two-column identity is enough | feed has **5 duplicate ids**, and float/decimal precision differs | canonicalisation + precision-aware comparison added |

Final architecture: **REST push with an explicit merge/upsert**, batch-stamping,
stale sweep, and a dedupe guard. Same end state as a coalesced transform, built
out of parts that actually exist.

---

## Part 8 — The debugging method that worked

Layered, cheapest check first. Each layer answers one question and the next
layer is only worth trying if this one is green.

```
1 connectivity   → can I authenticate at all?         sn_api.py count sys_user
2 existence      → does the table exist?              sn_api.py probe <table>
3 schema         → are the columns right?             create_table.py (reconcile)
4 storage        → does a value survive a write?      verify_table.py
5 single record  → does one real row land correctly?  import_events.py --limit 1
6 bulk           → do many rows converge?             tests/test_loads.py
7 concurrency    → do two runs collide?               tests/test_sync.py test 8
```

### The single highest-leverage technique: cross-check input against output

The bug that mattered most — 255-character column silently truncating a
429-character title — **does not appear in any log, error, or schema check**.
The dictionary was valid. The insert succeeded. Only comparing *what I sent*
with *what came back* revealed it.

Three variants, all in this repo:

**(a) Single-value round trip — `verify_table.py`**

Writes one probe row, reads it back, compares field by field, deletes it.

```
DRIFT  u_price_amount  decimal  sent='43.667712'  got='43.67'
RESULT: FAIL -- 1 column(s) did not round-trip
```

**(b) Whole-dataset diff — the diagnostic that found the residual drift**

Load every stored row once, rebuild the payload for every source record, compare
in memory, and group the differences by field:

```
drift by field: {'u_lng': 13, 'u_lat': 2, 'u_last_updated': 5}
```

That one line turned "20 rows update every night" into three named defects
(float last-digit noise, decimal precision, duplicate-key winner). Doing this by
hand in the UI would have been hopeless.

**(c) Report-vs-reality check — `tests/test_loads.py`**

The importer *reports* what it did. The test ignores the report and re-measures
the table independently (`rows`, `distinct_ids`, `duplicate_ids`, `blank_ids`).
If the two disagree, the report is lying and that's the bug.

### Other techniques that paid off

| Technique | What it found |
|---|---|
| **Read the shipped property description, not the name** (`sys_properties.description`) | `stop_transform` means something completely different from what its name suggests |
| **Probe for endpoint existence by status code** | 405 vs 400 distinguished "no such API" from "bad request" |
| **Count without fetching** (`X-Total-Count` header) | row counts at 1-record cost, used everywhere |
| **`--dry-run` before every write path** | rehearsed a full rebuild without touching the instance |
| **Repeat the no-op run until it is actually a no-op** | caught all three convergence defects; a single run would have looked perfect |
| **Run the same suite twice — concurrently — on purpose** | proved the dedupe guard, and proved the guarantee is "converges" not "never races" |
| **Isolate tests from each other** | the first red suite was two suites clearing the same table at once, not a code defect |

### Anti-patterns encountered

* **Trusting a 4xx as "endpoint exists".** A 400 became a false positive for
  three sessions.
* **Trusting the importer's own summary.** It counts what it *attempted*.
* **Guessing column widths.** 255 is a default, not a measurement.
* **Assuming a no-op run is a no-op because it exited 0.** It reported
  `unchanged=199, updated=1` and exit code 0 — a silent, permanent defect.

---

## Part 9 — Repeated-load evidence (the "does the event repeat?" question)

`tests/test_loads.py` loads the same payload five times back to back and, after
**every** load, re-measures the table independently of what the importer said.
It reports: total rows, distinct `u_event_id`, duplicate ids, duplicate rows,
blank ids.

```
python tests/test_loads.py --size 100 --loads 5
```

| Load | importer: ins / upd / unch / dedup / swept | table: rows / distinct / dup_ids / blank | expected |
|---|---|---|---|
| 1 | 100 / 0 / 0 / 0 / 0 | 100 / 100 / 0 / 0 | 100 |
| 2 | 0 / 0 / 100 / 0 / 0 | 100 / 100 / 0 / 0 | 100 |
| 3 | 0 / 0 / 100 / 0 / 0 | 100 / 100 / 0 / 0 | 100 |
| 4 | 0 / 0 / 100 / 0 / 0 | 100 / 100 / 0 / 0 | 100 |
| 5 | 0 / 0 / 100 / 0 / 0 | 100 / 100 / 0 / 0 | 100 |

Then the payload is perturbed:

| Phase | Action | importer | table result |
|---|---|---|---|
| grow +1 | add one new event | inserted **1** | 101 rows / 101 distinct / 0 dupes |
| repeat | same payload again | inserted **0** | stays 101 |
| shrink −1 | drop that event | swept **1** | back to 100, dropped key gone |

Final scan: **every event id appears exactly once; rows == distinct ids.**

`ALL LOAD TESTS PASSED` (32 assertions).

### Two harness lessons

1. **Never trust the importer's own counters.** Every assertion reads the table
   back. A summary that says "inserted 0" while the table grew by 200 is
   exactly the failure this catches.
2. **Never run two suites against the same instance concurrently.** The first red
   run of `test_sync.py` was not a code defect — `test_loads.py` was clearing the
   table at the same time, so row counts wobbled. Both suites pass 27/27 and
   32/32 when run in isolation. If you parallelise tests horizontally, give each
   a distinct target table.
