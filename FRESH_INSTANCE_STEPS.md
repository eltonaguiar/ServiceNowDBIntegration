# Fresh instance: step-by-step

Everything you need to stand this integration up against a brand-new ServiceNow
instance. Two paths: the script (fast) or the commands by hand (when you want to
see each step).

Both produce the same result. Both are safe to re-run.

---

## Path A — the script (recommended)

```bash
git clone https://github.com/eltonaguiar/ServiceNowDBIntegration.git
cd ServiceNowDBIntegration
chmod +x fresh_instance.sh

# read-only: proves the credentials work, writes nothing
./fresh_instance.sh --check

# build: tables, schema proof, sample load, idempotency proof
./fresh_instance.sh

# when you're happy: load the whole feed
./fresh_instance.sh --full
```

It prompts for the three things it can't guess:

```
ServiceNow instance hostname (e.g. dev123456.service-now.com): ...
Username: ...
Password for admin (input hidden): ...
```

Or pre-fill them and skip the prompts:

```bash
export SN_INSTANCE=dev123456.service-now.com
export SN_USER=admin
export SN_PASS='...'          # single quotes: protects $ ( ) # ! &
./fresh_instance.sh --full
```

| Flag | Effect |
|---|---|
| `--check` | connectivity + table probe only. No writes **to ServiceNow** (it does create a local `.env`, mode 0600). Use this first. |
| *(none)* | tables, schema verification, 100-row sample load, idempotency proof |
| `--full` | adds the complete feed load and a convergence re-run |
| `--with-tests` | adds `tests/*` — ⚠️ these **clear and repopulate** the target table |
| `--help` | usage |

At the end it prints the three things that must still be done by hand (ACLs, a
dedicated user, the schedule).

---

## Path B — by hand

Same stages, one command at a time. Useful for understanding, or when the script
fails somewhere and you want to isolate it.

### 0. Prerequisites

- Python 3.8+ (standard library only — **nothing to `pip install`**)
- Outbound HTTPS to the instance and to the feed URL
- An **admin** account on the instance (creating tables needs admin)

```bash
git clone https://github.com/eltonaguiar/ServiceNowDBIntegration.git
cd ServiceNowDBIntegration
```

### 1. Credentials

```bash
cp .env.example .env
chmod 600 .env
$EDITOR .env
```

```dotenv
# .env  -- gitignored, never committed
SN_INSTANCE=dev123456.service-now.com     # hostname only: no https://, no trailing /
SN_USER=admin                             # needs admin for sys_db_object writes
SN_PASS='...'                             # see the quoting note below
```

> **Quote the password.** `SN_PASS=p@ss$word` expands `$word` to nothing inside
> a `.env` sourced by a shell. Use single quotes, or escape it. The Python
> loader in `sn_api.py` reads the value literally, but a shell `source` does not
> — so single quotes are the safe habit for both.

Verify before going further:

```bash
python3 sn_api.py count sys_user
```

A number means credentials work. `401` means wrong password or no API access;
`403` means the account lacks the role.

### 2. Create the target table

```bash
python3 create_table.py specs/u_toronto_event.json --dry-run   # see the plan, write nothing
python3 create_table.py specs/u_toronto_event.json             # apply it
```

Expected tail:

```
RESULT: 22/22 fields present
Next: python verify_table.py u_toronto_event
```

This creates the table **and all 22 columns**. It is idempotent — a second run
reports `already exists, skipping` and then checks for drift.

### 3. (Optional) staging table — pull variant only

```bash
python3 create_table.py specs/u_ev_stage.json
```

Only needed if you intend the JDBC/JSON-pull path. Harmless otherwise.

### 4. Prove the schema

```bash
python3 verify_table.py u_toronto_event --ignore u_price_amount
```

Expected: `RESULT: PASS -- 21/21 columns verified`

> This is the single most valuable check. It writes a probe row, reads it back,
> compares every field exactly, then deletes it. A schema can look perfect and
> still truncate or round silently — the probe is what catches that.
> `--ignore u_price_amount` because that column is *intentionally* 2dp currency.

### 5. Prove idempotency before loading everything

```bash
# 100 rows
python3 import_events.py --source https://findtorontoevents.ca/events.json \
    --limit 100 --workers 10 --batch-id sample-1 --no-sweep

# the same 100 rows again -- MUST report "inserted 0"
python3 import_events.py --source https://findtorontoevents.ca/events.json \
    --limit 100 --workers 10 --batch-id sample-2 --no-sweep
```

If the second run inserts anything, the natural key is not guarding. **Stop** and
read `docs/FINDINGS.md` — a full load would duplicate the entire feed.

### 6. Full load

```bash
python3 import_events.py --source https://findtorontoevents.ca/events.json \
    --workers 10 --report reports/full-1.json
```

Measured: **~230 s** for 11,945 records at 10 workers.

Then run it again to confirm zero writes:

```bash
python3 import_events.py --source https://findtorontoevents.ca/events.json \
    --workers 10 --report reports/full-2.json
```

Expected: `inserted 0`, `updated 0`, `unchanged 11940`.

### 7. Tests (optional)

```bash
python3 -u tests/test_sync.py  --size 200 --workers 6
python3 -u tests/test_loads.py --size 100 --loads 5
```

⚠️ Both **clear and repopulate** `u_toronto_event`. Never run them concurrently
against the same instance — they will clobber each other and produce false
failures. After testing, re-run step 6 to restore the full dataset.

### 8. Confirm the numbers

```bash
python3 sn_api.py count u_toronto_event
```

Expect **11940**, not 11945. The feed contains 11,945 records but only 11,940
distinct `id` values; the canonicaliser plus dedupe guard collapse the 5
duplicates. This is correct behaviour, and `docs/FINDINGS.md` §3 defect 3
explains why the alternative — letting feed order decide the winner — never
converges.

---

## What no script can do for you

| Task | Why it's manual |
|---|---|
| **ACLs** | `POST sys_security_acl` returns `403 security constraints` even with `security_admin`; elevation is a browser-session concept |
| **Password for a dedicated user** | REST writes to password fields are unhashed and always 401 |
| **The schedule** | Lives on your host (systemd/cron), not in the instance |

### ACLs, exactly

A brand-new table has **no ACLs**, which means every authenticated user can read
*and write* it. Fix that immediately:

1. Avatar (top right) → **Elevate roles** → tick **`security_admin`** → OK
2. **System Security → Access Control (ACL) → New**

| Name | Operation | Requires role |
|---|---|---|
| `u_toronto_event` | `read` | `itil` |
| `u_toronto_event` | `write` | `admin` |

Leave the script condition empty — empty script + role requirement means
"allow if the user holds this role". `python create_acl.py u_toronto_event`
prints these instructions and confirms the restriction still applies.

### The schedule, exactly

```bash
sudo cp deploy/sn-events-sync.service deploy/sn-events-sync.timer /etc/systemd/system/
sudo install -m 0600 /dev/null /etc/sn-sync.env
$EDITOR /etc/sn-sync.env                 # SN_INSTANCE / SN_USER / SN_PASS
sudo systemctl daemon-reload
sudo systemctl enable --now sn-events-sync.timer
systemctl list-timers sn-events-sync.timer
```

Fires at **03:00 America/Toronto**, catches up after downtime
(`Persistent=true`), and handles EST/EDT automatically.

> The timezone must be **inside** `OnCalendar`. There is no `Timezone=` key in
> `[Timer]` — systemd ignores it and your job runs at 03:00 **UTC**. Confirm with
> `systemd-analyze calendar "*-*-* 03:00:00 America/Toronto"` (3am EDT should
> report `07:00:00 UTC`).

---

## Parameters you can change

Environment variables, all optional except the first three:

| Variable | Default | Notes |
|---|---|---|
| `SN_INSTANCE` | — | hostname only |
| `SN_USER` | — | admin required for schema creation |
| `SN_PASS` | — | prompted if unset |
| `SN_BASE_URL` | `https://$SN_INSTANCE/api/now/table` | only if you front the API differently |
| `FEED_URL` | `https://findtorontoevents.ca/events.json` | must be a flat array with `id` |
| `TARGET_TABLE` | `u_toronto_event` | must match the spec filename |
| `STAGING_TABLE` | `u_ev_stage` | pull variant |
| `WORKERS` | `10` | parallel writers |
| `SAMPLE_SIZE` | `100` | rows in the idempotency proof |

If your source has different field names, edit **`FIELD_MAP`** at the top of
`import_events.py` — that's the single place source keys are mapped to columns.
The spec files control types and widths.

---

## Checklist

| # | Check | Command | Expected |
|---|---|---|---|
| 1 | credentials | `python3 sn_api.py count sys_user` | a number |
| 2 | table exists | `python3 sn_api.py probe u_toronto_event` | `exists` |
| 3 | 22 columns | `python3 create_table.py specs/u_toronto_event.json` | `22/22 fields present` |
| 4 | round trip | `python3 verify_table.py u_toronto_event --ignore u_price_amount` | `PASS -- 21/21` |
| 5 | idempotent | second `import_events.py` run | `inserted 0 updated 0` |
| 6 | row count | `python3 sn_api.py count u_toronto_event` | `11940` |
| 7 | ACLs | UI → Access Control (ACL) | 2 records |
| 8 | schedule armed | `systemctl list-timers` | next run 03:00 local |
