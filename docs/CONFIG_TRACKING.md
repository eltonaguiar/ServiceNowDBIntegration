# Tracking config you created by script

The worry: *if the table, reports and dashboard were built by a script instead of
by hand in Studio, can you still track changes — in GitHub, or in an update set?*

**Short answer: yes to both, and one of them is already happening.** But neither
is automatic in the way you'd hope, so this documents what exists, what's
missing, and the workarounds people actually use.

---

## 1. Correcting the premise: scripting does *not* disable update sets

Update-set capture depends on two things — the record has a `sys_update_name`,
and it lives in a scope the platform tracks. It does **not** depend on how the
record was created. A REST insert captures exactly like a form save.

Measured on this instance, on records that were all created over the Table API:

| Evidence | Value |
|---|---|
| `sys_update_xml` rows total | **960** (was 710 earlier the same session) |
| … ours, in the **Default** update set | table+columns, 19 `sys_report`, every `par_dashboard*` / canvas / tab / widget |
| `sys_update_version` rows | **4,066** |
| … for `sys_dictionary_u_toronto_event*` | **30** |
| … for `sys_dictionary_u_toronto_event_u_primary_category` | 1 (the create) |
| `sys_audit` rows | **60,795** |

So `u_primary_category` — a column created by `backfill_geo_category.py` calling
the REST API — has a version record with `sys_created_on = 2026-10-28 05:05:14`
and `sys_created_by = admin`. The platform tracked it fine.

**What scripting genuinely costs you** is not tracking. It's these four things:

1. **Capture lands in `Default`**, mixed with every other change on the instance.
   Trying to open a clean set over REST did not work: I created an update set and
   pointed `sys_user_preference: sys_update_set` at it, and writes kept landing
   in Default. **The set has to be selected in the UI first.**
2. **No application scope** → `sys_app` = 0 and `sys_repo_config` = 0 on this
   instance. ServiceNow Studio's *Link to source control* needs a scoped
   application; global records aren't part of one, so there's nothing to sync.
3. **The scripts are in git, the records are not.** Git knows what you *intended*
   to create; it does not know what the instance actually holds.
4. **No import-set run history** for the data (`sys_import_set_run` = 0 — we
   don't use import sets), so integration run evidence lives in our JSON reports.

---

## 2. Workarounds, in the order I'd adopt them

### a. GitOps around the scripts — *tracks intent* (already done here)

Specs live in git; apply scripts are idempotent; a dry run tells you whether the
instance still matches the spec. This is the cheapest and it already works.

Add the drift gate (the missing half):

```bash
# fail if the live instance has drifted from the committed spec
python3 create_table.py   specs/u_toronto_event.json --dry-run
python3 create_reports.py specs/reports_toronto_events.json --dry-run
python3 create_dashboard.py specs/dashboard_toronto_events.json --dry-run
```

- **Buys:** reviewable history of intent, PR diffs, reproducible rebuild.
- **Misses:** changes made in the UI, which the spec knows nothing about.

### b. Export update-set XML into git — *tracks actual config*

The authoritative record of what the platform holds, including changes made by
hand. `sys_update_xml` stores the full record XML in `payload`.

```bash
curl -s -u "$SN_USER:$SN_PASS" \
  "https://$SN_INSTANCE/api/now/table/sys_update_xml?sysparm_query=update_set=$US&sysparm_fields=name,type,payload"
```

Write one file per record into `backups/updatesets/<date>/` and commit. Diffing
XML is noisy, but it is the truth.

- **Buys:** captures UI drift, and everything an update set would capture.
- **Misses:** `sys_report_map_source`, `cmn_location`, event data (never captured).

### c. Use the per-record versioning that already exists

`sys_update_version` keeps a version per tracked record. On a record's form,
*Show versions* / the `sys_update_version_list.do` view gives the history. This is
free and already populated for our columns — use it to answer "who changed this
field definition and when".

### d. Move into a scoped custom application → Studio source control

This is ServiceNow's **official** answer for "config in git". Create an
application (`sys_app`), put the table/reports/dashboard inside that scope, then
*Link to source control* (`sys_repo_config`) points the app at a git repo and
pushes/pulls metadata.

- **Buys:** real git workflow for instance config, the thing you're asking for.
- **Costs:** invasive here. Our records are global scope; moving them means
  creating the app, re-scoping the table, and re-creating dependent records.
  `create_table.py` etc. address global records, so the scripts would need
  rework.

### e. ServiceNow SDK (`now-sdk` / Fluent)

The modern CLI path: metadata is authored as source files in git, built and
deployed by the SDK, no UI required. Best choice for a **new** scoped app; it is
a rewrite if adopted for existing global artefacts.

### f. Classic ALM: remote update sets

Export a set as XML → load into `sys_remote_update_set` → retrieve, preview,
commit on the target. Table exists here but is unused (0 rows). This is the
pre-SDK enterprise deployment path, and it still works.

### g. Drift detection as the practical control

You don't strictly need to version the *records* if you can prove the instance
still matches the spec. (a) executed on a schedule is enough for most teams: a CI
job that runs the dry-runs and alerts on non-zero diff catches "someone edited it
by hand" within a day.

---

## 3. What I'd do for this repo

1. **Add the drift gate to CI** (§2a) — the single highest-value addition, ~10
   lines of YAML.
2. **Nightly: dump `sys_update_xml` for our record names into `backups/` and
   commit.** Gives git history of real platform config without restructuring.
3. **Stay global scope** unless the organisation mandates Studio source control.
   Migrating to a scoped app to gain git linkage is a big change, and the specs
   already give you what the app-linkage would: reviewable, reproducible config.
4. **Do not mix** hand-editing in the UI with scripted rebuilds. If someone edits
   a widget in the UI, the next `--update` silently overwrites it. The dry-run
   gate is what makes that visible before it happens.

---

## 4. Cheat sheet

| Want | Use | Status here |
|---|---|---|
| History of *intent* | git + specs + apply scripts | ✅ in use |
| Drift alarm | `--dry-run` in CI | ❌ not set up |
| History of *actual* config | `sys_update_xml` → git | ⚠️ exists, not exported |
| Who changed a field/record | `sys_update_version`, `sys_audit` | ✅ populated |
| Native git linkage | scoped app + `sys_repo_config` | ❌ no scoped app (`sys_app`=0) |
| Metadata-as-source | `now-sdk` | ❌ not adopted |
| Deployment between instances | remote update sets | ❌ unused (`sys_remote_update_set`=0) |
| History of *data* | `sys_audit` | ✅ 60,795 rows |
