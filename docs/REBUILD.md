# Rebuild from scratch

For the day the PDI expires, the instance is cloned, or someone deletes the
table. The design goal is: **nothing important lives only in ServiceNow.**

Everything that matters is a text file in this repo:

| What | Where it lives | Recreatable? |
|---|---|---|
| Table definition | `specs/*.json` | ✅ `create_table.py` |
| Column types and widths | `specs/*.json` | ✅ `create_table.py` |
| Data | the upstream feed | ✅ `import_events.py` |
| Sync logic (identity, sweep, dedupe) | `import_events.py` | ✅ already in git |
| Tests | `tests/` | ✅ already in git |
| **ACLs** | nowhere | ❌ UI only, 2 records |
| **Schedule** | systemd/cron on the host | ⚠️ one unit file / one cron line |
| **Integration user + roles** | nowhere | ⚠️ UI, ~2 minutes |
| **Data source / transform map** (pull variant only) | nowhere | ❌ UI |

So a full rebuild is roughly **10 minutes of commands plus 5 minutes of clicking**.

---

## 1. Before you start

You need:

- [ ] The new instance hostname (e.g. `dev123456.service-now.com`)
- [ ] An admin username and password on it
- [ ] Outbound HTTPS from the machine running the scripts
- [ ] The upstream feed URL

Nothing else. No backup file of the old instance is required — the table and all
its data are regenerated from the spec and the feed.

---

## 2. The rebuild, in order

```bash
# 0. Get the code (it's already in git — this is the whole backup)
git clone https://github.com/eltonaguiar/ServiceNowDBIntegration.git
cd ServiceNowDBIntegration

# 1. Point at the new instance
cp .env.example .env
#    edit .env: SN_INSTANCE / SN_USER / SN_PASS
```

```bash
# 2. Create the target table + all 22 columns (idempotent, ~20 s)
python create_table.py specs/u_toronto_event.json

# 3. Create the staging table (only needed for the pull variant; harmless to skip)
python create_table.py specs/u_ev_stage.json

# 4. PROVE the schema before loading anything
python verify_table.py u_toronto_event --ignore u_price_amount
#    expect: RESULT: PASS -- 21/21 columns verified
```

```bash
# 5. Load the data (first load ~4 min at 10 workers)
python import_events.py --source https://findtorontoevents.ca/events.json \
    --workers 10 --report reports/rebuild-1.json

# 6. Prove idempotency on the new instance -- run it again
python import_events.py --source https://findtorontoevents.ca/events.json \
    --workers 10 --report reports/rebuild-2.json
#    expect: inserted 0, updated 0, unchanged 11940
```

```bash
# 7. Full behavioural suite against the new instance
python tests/test_sync.py --size 200 --workers 6
python tests/test_loads.py --size 100 --loads 4
#    expect: ALL TESTS PASSED / ALL LOAD TESTS PASSED
```

That is the functional rebuild. Steps 2–6 are copy-paste; only the `.env` edit
differs between instances.

---

## 3. What you must redo by hand

These are the four things that cannot be scripted, with the reason:

### 3.1 ACLs (2 records, ~2 minutes)

Not scriptable: `POST sys_security_acl` returns
`403 ACL Exception Insert Failed due to security constraints` even for a user
holding `security_admin`, because elevation is a browser-session concept.

1. Avatar → **Elevate roles** → tick `security_admin` → OK
2. **System Security → Access Control (ACL) → New**, twice:

| Name | Operation | Requires role |
|---|---|---|
| `u_toronto_event` | `read` | `itil` |
| `u_toronto_event` | `write` | `admin` |

Run `python create_acl.py u_toronto_event` to have it print these exact steps,
and to confirm the restriction is still in force.

### 3.2 Integration user (if you were not using admin)

Create a dedicated user, assign minimum roles, set the password in the UI (REST
writes to password fields are unhashed and produce `401`). Then update
`SN_USER` / `SN_PASS`.

### 3.3 The schedule

The unit files are in `docs/SCHEDULING.md` §2. On a new host:

```bash
sudo cp deploy/sn-events-sync.service /etc/systemd/system/
sudo cp deploy/sn-events-sync.timer   /etc/systemd/system/
sudo install -m 0600 /dev/null /etc/sn-sync.env   # then fill it in
sudo systemctl daemon-reload && sudo systemctl enable --now sn-events-sync.timer
systemctl list-timers sn-events-sync.timer
```

If you were using cron on the old host and the host is unchanged, **nothing to
do** — it keeps working, just point it at the new `.env`.

### 3.4 Pull variant only: data source, transform map, MID

If you were on JDBC/JSON pull rather than push, those are UI objects. Recreate in
this order: credential → JDBC connection → data source (tick **Use MID Server**)
→ import set table → transform map with **coalesce on `u_event_id`** →
`scheduled_import_set`. See `docs/SCHEDULING.md` §4.

---

## 4. Verification checklist

Tick every line or the rebuild is not done:

| # | Check | Command | Expected |
|---|---|---|---|
| 1 | credential works | `python sn_api.py count sys_user` | a number, no error |
| 2 | table exists | `python sn_api.py probe u_toronto_event` | `exists` |
| 3 | 22 columns present | `python create_table.py specs/u_toronto_event.json` | `22/22 fields present` |
| 4 | values survive a round trip | `python verify_table.py u_toronto_event --ignore u_price_amount` | `PASS -- 21/21` |
| 5 | row count matches unique keys | `python sn_api.py count u_toronto_event` | `11940` |
| 6 | no duplicate keys | `tests/test_loads.py` phase 5 | `every event id appears exactly once` |
| 7 | re-run writes nothing | second `import_events.py` run | `inserted 0 updated 0` |
| 8 | ACLs exist | UI list, or `create_acl.py` output | 2 records |
| 9 | schedule armed | `systemctl list-timers` | next run at 03:00 local |
| 10 | non-admin user sees what they should | log in as a test user | read yes, write no |

Check 5 expecting exactly **11940** is not a typo: the feed contains 11,945
records but only 11,940 distinct `id` values, and the canonicaliser plus dedupe
guard collapse the 5 duplicates. See `FINDINGS.md` §3 defect 3.

---

## 5. Making a rebuild cheaper next time

Already done, because this document is the output of asking "what would hurt if
we had to start over?":

* **Schema is data, not clicks.** Both tables live in `specs/*.json`. There is no
  "remember to add that column" step.
* **`create_table.py` is idempotent and reports drift.** Running it against
  *any* instance converges the schema, so it doubles as a repair tool. Use
  `--fix-drift` to apply width/type corrections.
* **`--dry-run` on everything.** You can rehearse a rebuild without touching the
  instance.
* **Credentials are externalised.** Nothing in git, so a rebuild never involves
  recovering a secret from history.
* **The verification is a script, not a checklist in someone's head.**
  `verify_table.py` and `tests/` run identically against any instance.
* **The sync is convergent, not incremental-only.** Even a total data loss is
  repaired by one full run — there is no "we lost the delta log" scenario.

### Deliberately *not* automated, and why

* **ACLs** — platform restriction, not laziness.
* **Data source + transform map** (pull path) — could be built with
  `POST sys_data_source` / `POST sys_transform_map` (the metadata write is
  permitted; verified), but it was not worth building for a path we did not
  take. If you standardise on pull, script it and delete this caveat.
* **Schedule** — lives on the host, not in the instance. systemd unit files are
  in git so restoring it is a copy, not a design exercise.

---

## 6. Estimated timings (measured, not guessed)

| Step | Time |
|---|---|
| clone + `.env` | 1 min |
| `create_table.py` ×2 | ~40 s |
| `verify_table.py` | ~5 s |
| first full load (11,945 inserts, 10 workers) | **230 s** measured |
| convergence re-run | ~36 s measured |
| test suites | ~4 min |
| ACLs + integration user (UI) | ~5 min |
| systemd timer | ~2 min |
| **total** | **~12–15 min** |
