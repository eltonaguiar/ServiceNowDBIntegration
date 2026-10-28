# Integration architecture: what we built, versus what ServiceNow calls best practice

Repo: `github.com/eltonaguiar/ServiceNowDBIntegration`

A note on sourcing: `docs.servicenow.com` could not be fetched for this write-up
(the URLs redirect through an ad tracker). Everything attributed to "the
ServiceNow model" below is from ServiceNow's documented integration model, and
each claim is paired with something verifiable **in this instance** — table
counts, data-source types, MID capability records — so you can check it yourself
rather than take it on trust.

---

## 1. Short answers

| Question | Answer |
|---|---|
| **Bi-directional?** | **No. Inbound only, one way: feed → ServiceNow.** |
| **Which direction?** | `https://findtorontoevents.ca/events.json` → `u_toronto_event`. Nothing is ever written back to the feed. |
| **Which frequency?** | Designed as **daily at 03:00 America/Toronto**. **Not currently installed** — see §3. |
| **Import set?** | **No.** We do not use Import Sets. |
| **JDBC?** | **No.** The source is JSON over HTTPS, not a database. |
| **MID Server?** | **Not used by this integration.** `gx10-mid` is Up and registered, but nothing in this pipeline routes through it. |
| **Mechanism** | External Python script on Linux → HTTPS + Basic auth → **Table API** (`/api/now/table/u_toronto_event`), upsert keyed on `u_event_id`. |

---

## 2. How it actually works today

```
events.json  ──HTTPS GET──▶  import_events.py  ──REST POST/PATCH──▶  u_toronto_event
 (public feed)                 (Linux host)        Table API
                                    │
                                    ├─ normalise       types, widths, booleans, numerics
                                    ├─ timezone        'Z' reinterpreted as America/Toronto → UTC
                                    ├─ upsert          match on u_event_id
                                    ├─ sweep           delete rows absent from feed (optional)
                                    └─ derive          u_primary_category, u_location_ref
```

Step by step:

1. **Fetch** the feed (HTTP GET, no auth; the file is public).
2. **Normalise.** `FIELD_MAP` maps 21 source keys to columns. Datetimes are
   converted source-local → UTC, DST-aware. Numerics are rounded to the column's
   storage precision (a `decimal(15)` keeps 2 dp, so writing 6 dp rewrites the row
   forever). Booleans become `"true"`/`"false"`.
3. **Read existing** rows, building `u_event_id → sys_id`.
4. **Upsert.** `values_differ()` compares field by field (numeric-tolerant) and
   PATCHes only genuine changes. This is what makes re-runs free: two consecutive
   runs produced `0 inserted, 0 updated, 11,940 unchanged`.
5. **Sweep** (skippable with `--no-sweep`): delete target rows whose
   `u_event_id` is absent from the feed.
6. **Derive.** `u_primary_category` (first CSV token of `u_categories`) and
   `u_location_ref` (venue → `cmn_location`, which supplies map coordinates).
7. **Report.** A JSON run report (counts, errors, elapsed) is written per run.

Idempotency rests entirely on `u_event_id` being present and stable in the feed.

---

## 3. Frequency — and the honest status

Designed:

```ini
# deploy/sn-events-sync.timer
OnCalendar=*-*-* 03:00:00 America/Toronto
Persistent=true
RandomizedDelaySec=300
```

`Persistent=true` means a missed run fires on next boot;
`RandomizedDelaySec=300` spreads load if several instances share a source.

**Verified state on this host: not installed.**

```
/etc/systemd/system/sn-events-sync.*   → none
systemctl list-timers | grep sn-events → nothing
crontab -l | grep sn-sync              → nothing
```

So every run to date has been manual. The integration is a working puller, not
yet a scheduled service. Install with:

```bash
sudo cp deploy/sn-events-sync.{service,timer} /etc/systemd/system/
sudo tee /etc/sn-sync.env >/dev/null <<'EOF'
SN_INSTANCE=dev296154.service-now.com
SN_USER=...
SN_PASS=...
EOF
sudo chmod 600 /etc/sn-sync.env
sudo systemctl daemon-reload && sudo systemctl enable --now sn-events-sync.timer
```

---

## 4. The Integration & Health tab, widget by widget (re-verified)

Values measured on 2026-10-28 against the live table.

| Widget | Query | Value | What it tells you | Healthy looks like |
|---|---|---|---|---|
| **Events synced** | *(all rows)* | **11,942** | Total volume held | Non-zero, roughly matching feed size |
| **Mapped (on map)** | `u_location_refISNOTEMPTY` | **9,600** | Rows the Geomap can plot | High proportion of total |
| **Unmapped (no coords)** | `u_location_refISEMPTY` | **2,342** | Feed rows with no lat/lng | Stable, not growing |
| **Missing category** | `u_primary_categoryISEMPTY` | **0** | Derive step ran | **Must stay 0** |
| **Missing start date** | `u_startISEMPTY` | **2** | Cannot appear in any date view | 0 or a handful |
| **Added last 7 days** | `sys_created_on >= daysAgoStart(7)` | **11,942** | Sync is live | Comparable to feed churn |
| **Events per sync batch** | group by `u_batch_id` | **2 batches** | One bar per run | One bar added per schedule tick |

Two honest caveats:

- **"Added last 7 days" reads 11,942** because the whole table was loaded this
  week. It will only become a useful signal in week two. It is not wrong, just
  not yet informative.
- **"Events per sync batch" shows only 2 bars** (`batch-20261028T052304Z` 11,753,
  `restore-final` 189) because `u_batch_id` is overwritten on each write, so it
  reflects the *latest* run per row, not history. For true run history you'd read
  the per-run JSON reports, or adopt Import Sets (which keep import-set rows).
  This is a design limit of the scripted approach, stated rather than hidden.

---

## 5. What ServiceNow calls best practice, and how it differs

The platform's canonical inbound-integration machinery is **Data Source →
Import Set → Transform Map**, driven by a schedule. The pieces exist here even
though we don't use them:

| Component | Table | Present on this instance |
|---|---|---|
| Data sources | `sys_data_source` | **17** (File 8, OIDC 5, LDAP 2, **JDBC 2**) |
| Transform maps | `sys_transform_map` | **14** (119 transform entries) |
| Import sets | `sys_import_set` | **0** |
| Import set runs | `sys_import_set_run` | **0** |
| Scheduled imports | `sys_trigger` | **0** |

So the machinery is installed and has never been used for this data. That is the
single biggest architectural difference between what we built and the platform's
opinion of how a recurring feed should arrive.

### 5a. The Import Set pattern (for a file or API)

```
Data Source  ──▶  Import Set (staging table)  ──▶  Transform Map  ──▶  target table
(sys_data_source)   (sys_import_set + rows)        (coalesce on key)   (u_toronto_event)
        ▲
        └── Scheduled Import (sys_trigger) runs it on a cron-like schedule
```

1. A **Data Source** declares where the file/endpoint is and its format
   (CSV, Excel, JSON, XML), plus load-at-source options.
2. A **Scheduled Import** fires on a schedule and loads the raw data into an
   **Import Set** — an untyped staging table. Rows always land, even if they will
   fail validation, which is the audit trail we lack.
3. A **Transform Map** maps source fields → target fields, with per-field
   transforms (scripts, date formats, coalesce).
4. **Coalesce** fields are the idempotency key: if a coalesce match is found the
   row is **updated**, otherwise **inserted**. This is the platform-native
   equivalent of our `u_event_id` upsert.
5. `sys_import_set_run` keeps a permanent record of every run and its
   insert/update/error counts.

For our feed, the natural fit would be a **File-type data source over HTTPS**
(JSON), not JDBC — the source is a web file, not a database.

### 5b. The JDBC pattern (for a real database)

```
MID Server (JDBC capability) ──JDBC──▶ source DB
        │
        └──▶ Data Source (type=JDBC) ──▶ Import Set ──▶ Transform Map ──▶ target
```

JDBC data sources cannot be reached from the ServiceNow instance directly — the
**MID Server** executes the query from inside your network and returns the rows.
That is the classic reason a MID Server exists for data loading. This instance has
a JDBC-capable MID: `ecc_agent_capability` lists `JDBC` alongside `REST`, `SSH`,
`SNMP`, `WMI`, `PowerShell`, and `gx10-mid` is **Up**.

This pattern would be correct if the Toronto events lived in a MySQL/Postgres
database rather than a published JSON file. It does not apply here — using JDBC
would mean inventing a database to point at.

### 5c. The pattern we actually used

Scripted REST against the **Table API**. Legitimate, and common for
integration-from-outside, but it sits *outside* the platform's import framework:
no import set staging, no transform map, no import-set history, and the schedule
lives on the Linux host rather than in ServiceNow.

| | Scripted Table API (ours) | Import Set | JDBC Data Source |
|---|---|---|---|
| Direction | inbound | inbound | inbound |
| Source type | JSON over HTTPS | file/JSON/XML/CSV/API | database |
| Needs MID Server | no | only for on-prem file paths | **yes** |
| Idempotency | our `u_event_id` upsert | **coalesce** | coalesce |
| Run history | our JSON reports | **`sys_import_set_run`** | `sys_import_set_run` |
| Where the schedule lives | Linux systemd/cron | **ServiceNow** | ServiceNow |
| Unit-testable offline | **yes** (stdlib, no instance) | no | no |
| Handles bad data | aborts before writing (`preflight_widths`) | rows land, transform errors recorded | same as Import Set |
| Precision/timezone control | **explicit, in code** | per-field transform scripts | transform scripts |

### Why this shape was chosen

The feed needed things the declarative path does poorly: a DST-aware timezone
reinterpretation (the feed mislabels local time as `Z`), width pre-flight that
aborts rather than truncating, numeric comparison at column precision, duplicate
collapse, and offline unit tests. All of that is easier to express — and to test —
in a script. The trade-off is that we re-implemented coalesce and lost native
import-set history.

---

## 6. Recommendation

1. **Install the schedule** (§3). Until then this is a manual puller, and the
   "daily 03:00" claim in the docs is design intent, not behaviour.
2. **Keep the scripted REST path** for now — it is tested, idempotent, and encodes
   the timezone and precision rules that took real effort to get right.
3. **If an audit trail of every run is a requirement**, migrate the load to a
   File/JSON data source + transform map and keep the normalisation in transform
   scripts. You gain `sys_import_set_run` history and a ServiceNow-side schedule;
   you lose offline testability.
4. **JDBC is not applicable** unless the events move into a database — at which
   point `gx10-mid` (already Up, JDBC-capable) becomes the right transport.
5. **Do not add a second, parallel import path.** Two mechanisms writing the same
   table with two different idempotency keys is how duplicate-row incidents start.
