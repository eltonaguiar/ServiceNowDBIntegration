# Backup and recovery: what actually captures this work

> **The repo in question:** https://github.com/eltonaguiar/ServiceNowDBIntegration
> (branch `main`). Everything below refers to it. If you are reading this from a
> copy under `/tmp`, clone it somewhere durable — `/tmp` does not survive a reboot.

The question: *the data can always be re-pulled from the source feed, but what
about the integration itself — the table, the field definitions, the reports and
the dashboard? Could an update set have captured those, and could this be backed
up through GitHub source linkage?*

Short answer:

| Mechanism | Captures our work? | Verified |
|---|---|---|
| **Repo scripts + specs** (this repo) | **Yes — everything configurable** | yes, rebuilt from scratch repeatedly |
| **Update sets** | **Yes** for schema, reports, PAR dashboards | measured on this instance |
| **GitHub source linkage** (`sys_repo_config`) | **No** — needs a *scoped* app | table exists, not applicable |
| Event data | No (by design — but see §4) | yes |

---

## 1. Update sets already capture most of it

Every record we create carries a `sys_update_name`, which is what marks a record
as update-set-tracked:

```
par_dashboard         par_dashboard_868ff1c4c3589310310abf0d050131cf   scope=global
par_dashboard_widget  par_dashboard_widget_d7921e93f2084ad282aea55...
sys_db_object         sys_db_object_46672d84c39c5310310abf0d050131...
sys_dictionary        sys_dictionary_u_toronto_event_u_primary_cat...
sys_ux_macroponent    ...                                              scope=cd623f2c...
```

Because they are all **global scope**, they are captured automatically. On this
instance the *Default* update set (`9055f6338c9503100a22deb6dd2b4ead`, state
`in progress`) already holds **710 `sys_update_xml` records**, including:

| Record type | Count in the set |
|---|---|
| `sys_dictionary` (our columns) | 50 |
| `sys_db_object` (our tables) | 8 |
| `sys_report` (our reports) | 19 |
| `par_dashboard*` / canvas / widget | 211 / 14 / 191 / 3 |
| `sys_report_map_source` | **0** |
| `cmn_location` | **0** |
| `sys_ux_macroponent` | 0 |

That this is live rather than historical was confirmed directly: widget rows
created at 05:00 and 05:16 during this session appear in `sys_update_xml`
against that set.

### What an update set will NOT carry

- **`sys_report_map_source`** — the Geomap mapping chain. 0 captured. It must be
  rebuilt by `create_map_source.py`.
- **`cmn_location`** — 440 venue records. They are data, so they are not tracked.
  Rebuilt by `backfill_geo_category.py` from the feed.
- **The event rows themselves.** They come from the feed.
- **`sys_ux_macroponent`** — not something we create, so no loss.

### Caveat: you cannot open a clean update set via REST

An attempt to create an update set and point the acting user at it worked at the
record level — the set was created and `sys_user_preference: sys_update_set` was
updated — but subsequent writes **continued to land in Default**. So programmatic
selection of the current update set is not effective over the Table API.

Practical consequence: **start the update set in the UI** (type `sys_update_set.list`
→ New, or use the update-set picker in the header) before making changes you want
grouped. Everything captured in Default is still exportable; it is just mixed in
with the instance's other changes.

### Exporting

UI: open the update set → **Export** → XML, or `Download XML` for a single
record. Everything is readable over REST too, if you want it in git:

```
GET /api/now/table/sys_update_xml?sysparm_query=update_set=9055f6338c9503100a22deb6dd2b4ead
```

Each row carries the full record XML in `payload`.

---

## 2. GitHub source linkage does not apply

`sys_repo_config` exists on the instance — this is the table behind
ServiceNow Studio's *Link to source control* feature. But it links a **scoped
application** to a git repository, syncing that application's metadata.

Our records are `sys_scope = global` (only the out-of-box `Geomap` macroponent
belongs to a scope, and we did not create it). Global-scope records are not part
of an application, so there is nothing for source control to sync.

Making it applicable would mean moving everything into a custom scoped
application — a large change with no benefit here, and it would break the
`create_table.py` / `create_reports.py` / `create_dashboard.py` flow, which
addresses global records directly.

---

## 3. The repo *is* the backup, and it is the one we exercise

Every artifact is generated from a spec by an idempotent script:

| Layer | Script | Spec |
|---|---|---|
| Table + columns | `create_table.py` | `specs/u_toronto_event.json` |
| Reports | `create_reports.py` | `specs/reports_toronto_events.json` |
| Dashboard (tabs, widgets, map) | `create_dashboard.py` | `specs/dashboard_toronto_events.json` |
| Map source chain | `create_map_source.py` | inline |
| Derived columns | `backfill_geo_category.py` | inline |
| Everything, in order | `fresh_instance.sh` | — |

This has been exercised repeatedly during development — the whole dashboard was
destroyed and rebuilt many times, which is why the widget count kept changing.
That is a stronger guarantee than a static export: an update set restores *one
snapshot*, whereas these scripts restore the *current intent* and are re-runnable.

Weights to keep in mind:

- **`specs/templates/*.json` are load-bearing.** They hold the captured native
  `component_props` for each visualization. Lose them and charts either fail
  ("A 'Trend By' must be selected") or render with guessed cosmetics.
- Committed to git, so they are versioned. **Do not regenerate them casually** —
  a template captured from a *different* visualization is exactly what caused
  the earlier rendering bug.

---

## 4. What about the data?

The feed is the source of truth, but two derived layers are expensive to
recompute and worth keeping:

1. **`cmn_location` records** (440). Derived from the feed, but they are also
   matched by name so a re-run is cheap. `backfill_geo_category.py` recreates
   them.
2. **Timestamp correction.** `fix_source_timezone.py` applied a DST-aware shift
   to every row. If the table is ever reloaded from scratch, that script must be
   run **after** the initial import and **before** anyone trusts a date filter.
   It writes a marker (`.migrations/source_timezone_v1.json`) precisely so it
   cannot be applied twice by accident, so a fresh reload needs the marker
   removed.

CSV snapshots of `u_start`/`u_end` taken before that migration live in
`backups/`.

---

## 5. Recommended backup routine

1. **Git is primary.** Commit specs and templates after every change. They are
   small and diffable.
2. **Before any migration**, run `backfill_geo_category.py --dry-run` and
   `fix_source_timezone.py --dry-run` to confirm what would change.
3. **For a platform-side snapshot**, create an update set in the UI, make (or
   re-save) the changes you care about, export it as XML, and attach it to the
   release. Remember it will not include map sources or locations.
4. **Do not rely on the update set alone** for a restore: it cannot recreate
   `sys_report_map_source`, so the map would come back blank.
5. **Recovery order:** schema → reports → dashboard → map source → backfill →
   timezone fix (only on a fresh load) → sync.
