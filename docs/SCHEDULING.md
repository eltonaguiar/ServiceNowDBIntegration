# How the schedule works, and how it talks to the database

Two different things get confused in integration work, so this document keeps
them apart:

* **Transport** — how rows physically get from your database into ServiceNow.
* **Trigger** — what makes it happen at 03:00 without a human.

This repo implements **Option 4 (REST push)** for the Toronto events feed and
documents **Option 2 (JDBC pull)** as the alternative. Both are described below,
including exactly which file holds the connection details and what to change
when a URL or password rotates.

---

## 1. What is actually running today

```
  ┌────────────┐   1. GET events.json     ┌──────────────┐
  │ your host  │ ───────────────────────► │ event feed   │
  │ (cron /    │                          │ (public JSON)│
  │  systemd)  │ ◄─────────────────────── └──────────────┘
  │            │
  │  import_   │   2. REST Table API (HTTPS + Basic auth)
  │  events.py │ ───────────────────────► ┌───────────────────┐
  └────────────┘   POST / PATCH / DELETE  │ ServiceNow        │
                                          │ u_toronto_event   │
                                          └───────────────────┘
```

There is **no database connection from ServiceNow**. ServiceNow never dials
out. Your host does the reading and the writing.

### Why this design and not JDBC

| | REST push (implemented) | JDBC pull (alternative) |
|---|---|---|
| ServiceNow needs network access to your DB | no | **yes** (plus a MID Server) |
| Needs a JDBC driver `.jar` on the MID | no | yes |
| Chunking under your control | yes | transform-side only |
| Debugging | local logs | instance logs + MID logs |
| Survives your host being off | **no** | yes |

We chose push because the source was already a public JSON endpoint, the volume
(11,940 rows) fits comfortably, and it needs no MID/driver/firewall work. See
`FINDINGS.md` §2 for the measurement that ruled out the Import Set API on this
instance.

### Where the "database" fits in

`events.json` **is** the export of the events database. The push sync treats it
as the system of record. If you later want ServiceNow to read the database
directly instead of a file, that's the JDBC variant in §4 — same target table,
same identity rules, different transport.

---

## 2. The trigger: making it run at 03:00

### Important: "3am EST" is not a fixed UTC time

Toronto is **EDT (UTC−4)** in October and **EST (UTC−5)** in November. A fixed
UTC cron silently drifts by an hour twice a year.

| Scheduler | Correct approach |
|---|---|
| **systemd timer** (recommended on a Linux host) | `OnCalendar=*-*-* 03:00:00 America/Toronto` — the zone goes **inside** the calendar expression; there is no `Timezone=` key |
| **cron** (cronie / Vixie) | `CRON_TZ=America/Toronto` on the line above the entry |
| **ServiceNow** `scheduled_import_set` | `time_zone = America/Toronto` |
| **GitHub Actions** | ❌ UTC only — `0 8 * * *` is 3am EST but 4am EDT. Avoid for this. |

### Option A — systemd timer (recommended)

`/etc/systemd/system/sn-events-sync.service`

```ini
[Unit]
Description=Sync Toronto events into ServiceNow
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=svc-snsync
WorkingDirectory=/opt/sn-db-repo
EnvironmentFile=/etc/sn-sync.env
ExecStart=/usr/bin/python3 import_events.py \
    --source https://findtorontoevents.ca/events.json \
    --workers 8 \
    --report /var/log/sn-sync/report.json
# Fail loudly rather than leaving a half-finished sync
SuccessExitStatus=0
```

`/etc/systemd/system/sn-events-sync.timer`

```ini
[Unit]
Description=Nightly Toronto events sync at 03:00 America/Toronto

[Timer]
OnCalendar=*-*-* 03:00:00 America/Toronto
Persistent=true          # catch up if the host was off at 03:00
RandomizedDelaySec=300   # avoid hitting the feed at exactly :00:00

[Install]
WantedBy=timers.target
```

> ⚠️ **`Timezone=` is not a valid `[Timer]` key.** systemd ignores it with
> `Unknown key name 'Timezone' in section 'Timer', ignoring.` and your job runs at
> 03:00 **UTC**. The zone must be appended to the calendar expression itself.
> Verify with `systemd-analyze calendar "*-*-* 03:00:00 America/Toronto"`, which
> should report the next elapse in UTC shifted by your offset
> (3am EDT → `07:00:00 UTC`).

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sn-events-sync.timer
systemctl list-timers sn-events-sync.timer      # confirm next run
journalctl -u sn-events-sync.service -n 50      # read the last run
```

`Persistent=true` is the important line: if the host is down at 03:00, the run
happens at the next boot instead of being skipped for a day.

### Option B — cron

```cron
CRON_TZ=America/Toronto
0 3 * * * /opt/sn-db-repo/run-sync.sh >> /var/log/sn-sync/cron.log 2>&1
```

`run-sync.sh` should `set -euo pipefail` and source the env file:

```bash
#!/usr/bin/env bash
set -euo pipefail
set -a; source /etc/sn-sync.env; set +a
cd /opt/sn-db-repo
exec python3 import_events.py --source https://findtorontoevents.ca/events.json --workers 8
```

### Option C — ServiceNow owns the trigger (`scheduled_import_set`)

Only relevant for the **pull** variants, because ServiceNow must fetch. The
inheritance chain on the instance is:

```
scheduled_import_set  →  scheduled_data_import  →  sysauto ("Scheduled Job")
```

`scheduled_data_import` additionally carries `map` (the transform map),
`partition_method` / `partition_size` (partitioned imports), and
`pre_script` / `post_script` hooks. `sysauto` supplies `run_type`, `run_time`,
`run_daysofweek`, **`time_zone`**, `active`, `run_as`.

So a working record needs:

```json
{
  "data_source": "<sys_id of the data source>",
  "map":         "<sys_id of the transform map>",
  "active":      "true",
  "run_type":    "daily",
  "run_time":    "03:00:00",
  "time_zone":   "America/Toronto",
  "run_as":      "<dedicated integration user>"
}
```

There is **no menu module** for scripted virtual tables on this instance, and
`scheduled_import_set` has no direct time fields — always set the inherited ones
on the `sysauto` parent. Query with
`https://<instance>/api/now/table/scheduled_import_set`.

---

## 3. Where credentials live, and what to change when they rotate

This is the part that causes the most outages, so it is deliberately explicit.

### Single source of truth: one env file

Everything reads the same three variables. Nothing is hardcoded.

| Variable | Used by | Example |
|---|---|---|
| `SN_INSTANCE` | `sn_api.py` → every script | `yourinstance.service-now.com` |
| `SN_USER` | `sn_api.py` | `integration.user` |
| `SN_PASS` | `sn_api.py` | *(secret)* |
| `SN_BASE_URL` | optional override | `https://host/api/now/table` |

Precedence: **process environment wins**, then `.env` in the repo root. Nothing
else is consulted.

```bash
cp .env.example .env          # local development
# production: /etc/sn-sync.env, mode 0600, owned by the service user
sudo install -m 0600 -o svc-snsync -g svc-snsync /dev/null /etc/sn-sync.env
```

### Change matrix — what to edit for each rotation

| What changed | Files to edit | Restart / action needed |
|---|---|---|
| **ServiceNow password** | `/etc/sn-sync.env` (`SN_PASS`) only | none — the next run picks it up |
| **ServiceNow user** | `/etc/sn-sync.env` (`SN_USER`) | none |
| **Instance hostname / PDI expired** | `/etc/sn-sync.env` (`SN_INSTANCE`) | none — but see `REBUILD.md` |
| **Database URL** (JDBC variant) | the `sys_import_jdbc_connection` record (`jdbc_server`, `database_port`, `database_name`) *or* `connection_url` when `manually_set_connection_url=true` — **not** a file in this repo | edit the record; the data source picks it up on the next scheduled run |
| **Database password** (JDBC variant) | the credential record referenced by the data source — set it in the **UI** (see §4.3) | next scheduled run |
| **JDBC driver version** | drop the new `.jar` in the MID's `extlib`, remove the old one | **restart the MID Server** |
| **Events feed URL** | the `--source` argument in the systemd unit / cron line | `systemctl daemon-reload` |
| **Target table name** | `TARGET` in `import_events.py`, plus both spec files | none |
| **Coalesce key** | `KEY_FIELD` in `import_events.py` + spec `u_event_id` | **requires a one-time full reload** — see below |

### Rotating the password safely, in order

1. Create the new password on the ServiceNow user (or create a new user).
2. Update `/etc/sn-sync.env`.
3. Run **once manually** and confirm a clean report:
   ```bash
   sudo -u svc-snsync bash -c 'set -a; source /etc/sn-sync.env; set +a; \
     python3 /opt/sn-db-repo/import_events.py --source <url> --dry-run'
   ```
   `--dry-run` exercises the credential without writing anything.
4. Then revoke the old password.

Doing it in that order means there is never a window where the job is
guaranteed to fail.

### What happens if the credential is wrong

`sn_api.py` raises a friendly error before doing anything:

```
SN_USER / SN_PASS are not set.
  cp .env.example .env   and fill it in, or export them.
```

and on a bad password the HTTP layer raises `401 Unauthorized` on the first
call, before any rows are touched — the sync is atomic per row, so a 401 mid-run
leaves already-written rows correct and simply marks the run as failed (exit 1).

### Why `--dry-run` is the rotation smoke test

It performs a **preflight width check against the live dictionary** and prints
the first three payloads. If the credential or instance is wrong, it fails
there. It writes nothing.

---

## 4. The JDBC alternative (ServiceNow pulls from SQL directly)

Use this only if ServiceNow must reach the database itself. Everything here is
**not** implemented in this repo — it is the officially recommended pull pattern,
documented so the choice is informed.

### 4.1 The pieces

| Piece | Where |
|---|---|
| JDBC driver `.jar` (MariaDB driver for MySQL) | MID Server `agent/extlib/` |
| Connection record | `sys_import_jdbc_connection` — `jdbc_server`, `database_port`, `database_name`, `jdbc_user_name`, `jdbc_password` |
| Data source | `sys_data_source` — type `JDBC`, **`mid_server` = the MID**, `format`, SQL |
| Staging table | auto-created import set table |
| Transform map | `sys_transform_map` + `sys_transform_entry`, coalesce on the external ID |
| Schedule | `scheduled_import_set` (see §2 Option C) |

The driver list on the instance already offers `org.mariadb.jdbc.Driver` for
MySQL, which confirms the platform expects the MariaDB driver for MySQL sources.

### 4.2 The SQL

```sql
SELECT id, title, date, location, lat, lng, status
FROM   events
WHERE  last_updated >= ?
ORDER BY last_updated
```

Best practices that matter here:

* **Explicit column list, never `SELECT *`** — a new column upstream otherwise
  breaks the mapping.
* **`ORDER BY`** — makes failures reproducible.
* **Incremental `WHERE`** — full-table extracts every night are how people hit
  transaction timeouts. For the first load, bound it by date range in phases,
  then switch to a delta predicate.
* **Match types and widths to the target columns** before the first run, or you
  will spend a day chasing silent truncation.

### 4.3 Database password rotation (JDBC)

The database password is **not** in this repo and cannot be set over REST — the
same class of restriction as ACLs. Set it in the UI:

1. **System Import Sets → Data Sources** → open the JDBC data source.
2. Open the referenced **credential** record → **Set Password**.
3. Save, then **Test Load 20 Records** on the data source.

Verify with the data source's own test, not by querying the credential table —
password columns always read back as `********` and prove nothing.

### 4.4 What breaks most often

| Symptom | Cause |
|---|---|
| Import never starts | MID cannot route to the DB host, or wrong port |
| `No suitable driver` | `.jar` missing from MID `extlib`, or MID not restarted |
| Empty staging table | SQL returned nothing — check the `WHERE` and the bind values |
| Rows in staging, none in target | transform map missing/inactive, or every row failed coercion |
| Duplicates on re-run | no coalesce field |

---

## 5. The operational checklist (aligned with ServiceNow's Now Create guidance)

The *Now Create* "Integration with External Solutions" success pack recommends a
sequence rather than an ad-hoc build. This repo follows it:

| Stage | Now Create intent | Here |
|---|---|---|
| 1. Design the interface | agree payload, key, frequency, ownership | `docs/FINDINGS.md`, README Part 1 |
| 2. Build the schema | target table first | `specs/*.json` + `create_table.py` |
| 3. Prove the schema | validate types and widths | `verify_table.py` (round-trip) |
| 4. Build the transport | push or pull | `import_events.py` |
| 5. Prove the transport | small loads, then full | `tests/test_loads.py` |
| 6. Secure | ACLs, dedicated user | README Part 5, `create_acl.py` |
| 7. Schedule | timezone, catch-up, alerting | §2 above |
| 8. Operate | monitoring, cleanup, documented owner | §6 below |

Their leading practices that this implementation honours explicitly:

* **Dedicated integration user**, minimum roles — never a personal admin.
* **Stable external key for coalesce** — `u_event_id`; measured 11,940 unique of
  11,945 rows, so the 5 duplicates are canonicalised deterministically.
* **Test before scheduling** — `--dry-run`, then 100 rows, then full.
* **Match field widths and types before loading** — the preflight exists because
  this was violated and cost a debugging cycle.
* **Avoid full extracts where a delta is possible** — noted as the next
  improvement for this feed (§6).
* **Document the user, the query, the key** — this file.

---

## 6. Known gaps / next improvements

Honest list, not a wish list:

1. **The feed is a full extract every night.** 11,940 rows, ~36 s at steady
   state because unchanged rows are skipped. That's fine at this size; if the
   feed grows past ~100k rows, switch to a `?since=` delta.
2. **No post-run alerting.** A failed run exits non-zero and logs, but nothing
   pages anyone. Add `OnFailure=` to the systemd unit.
3. **Staging table `u_ev_stage` is unused** by the push path — it exists for the
   JDBC/pull variant. Safe to delete if you never take that route.
4. **ACLs still manual.** Platform restriction, not a design choice.
5. **No retry/backoff.** A transient 500 marks the row as an error and the run
   exits 1; the next nightly run repairs it because the sync is idempotent.

---

## 7. Official references and the practices taken from them

Delivery guidance (not product docs) lives in ServiceNow's **Now Create**
methodology, published in the Best Practices Library:

* Portal: <https://mynow.servicenow.com/now/best-practices/home>
  (moved from the older Now Create / nowlearning path around Dec 2025)
* Relevant Success Pack: **"Integration with External Solutions"**, under
  *Now Platform Success Packs*. It supplies the implementation sequence, RACI,
  workshop decks and architecture considerations for connecting external systems.
* Companion assets: *Integrations Workshop Presentation*, *Integrations Planning
  Template*.

Product documentation (Zurich family) for the pieces used here:

| Topic | Where |
|---|---|
| Import Sets, data sources | System Import Sets / Data and Automation |
| **JDBC data source** | Integrate applications → System Import Sets. Requires a transform map; MID Server recommended for network/security |
| Scheduled Data Imports | System Import Sets → Administration → Scheduled Imports (`scheduled_import_set`) |
| Learning | ServiceNow Developer "Learn Integrations on the Now Platform" series |

### The JDBC practices that matter most (Option 2)

1. **Route through a MID Server.** Instance-to-database direct connections are
   possible but not preferred — MID gives you the firewall path, the driver
   location, and auditable egress.
2. **Import Set + transform map, not direct Table API writes, for bulk.**
   Coalescing, mapping scripts, error handling and audit come for free.
3. **Coalesce on a stable external key.** Non-negotiable; without it every run
   duplicates.
4. **Load incrementally.** Use a `WHERE last_updated >= ?` predicate rather than
   a full extract; for the initial historical load, bound by date range in
   phases so a single transaction cannot time out.
5. **Control volume.** Raise the JDBC probe result-set row limit where safe,
   enable concurrent imports when order doesn't matter, schedule off-peak, and
   **clean up old import set rows** — staging tables grow without bound.
6. **Run as a dedicated integration user** with minimum roles (`import_admin`
   class), never a personal admin.
7. **Test before scheduling:** *Test Connection* → *Load 20 Records* → validate
   the transform with real data → check import set log and transform history →
   then activate the schedule.
8. **SQL hygiene:** explicit column lists (never `SELECT *`), `ORDER BY` for
   determinism, and types/widths matched to the target before the first run.
9. **Operational hygiene:** pre/post scripts where you need incremental logic or
   chaining, and monitor import set runs, ECC queue (MID), and system logs.
10. **Know when *not* to use JDBC.** If the source can push, or already exposes
    a file/HTTP endpoint, that is usually simpler than maintaining a MID plus a
    driver. JDBC earns its complexity when the source is an on-prem database you
    are not allowed to modify.

### How this repo maps to that checklist

| Practice | Status here |
|---|---|
| MID Server preferred | n/a — push architecture, no inbound path needed |
| Import Set + transform | ⚠️ not used: the Import Set API is absent on this instance (`FINDINGS.md` §1). Identity is enforced client-side instead |
| Stable coalesce key | ✅ `u_event_id`, with source duplicates canonicalised deterministically |
| Incremental loads | ⚠️ full extract, but unchanged rows are skipped (~36 s steady state). Switch to `?since=` past ~100k rows |
| Dedicated integration user | ✅ documented in `REBUILD.md` §3.2 |
| Test before scheduling | ✅ `--dry-run` → `--limit 100` → full → `tests/` |
| SQL hygiene | ✅ (JDBC path) explicit columns, `ORDER BY`, incremental predicate in §4.2 |
| Staging cleanup | ⚠️ `u_ev_stage` unused by the push path; delete it if you never take the pull route |
| Monitoring | ⚠️ non-zero exit + report JSON; no alerting yet (`OnFailure=` is the next step) |
