# Dashboards and read-only views: what to build, and what you can script

The question: *"can you programmatically create a set of dashboards showing a good
mix of data — starting on this week — and what does ServiceNow best practice
actually recommend for read-only viewing?"*

Short answer: **script the reports, assemble the dashboard once by hand.** Reports
are ordinary records and fully automatable; the modern dashboard container is a
UI Builder artefact and is not.

---

## 1. The options, and what each is for

| Option | What it is | Scriptable? | Use it when |
|---|---|---|---|
| **Reports** (`sys_report`) | A saved query + visualisation. 733 already on this instance. | ✅ **verified** | Always — this is where the analytical content lives |
| **Dashboard** (`par_dashboard`) | The container that lays reports out on a canvas | ❌ see §2 | You want them side by side |
| **UI Builder** (`sys_ux_page`) | Custom task-driven experience with your own components | ⚠️ technically, but authored in the UI | Users need to *act*, not just look |
| **Service Portal** (`sp_page`) | Self-service portal page | ⚠️ widgets are code | External / unauthenticated audiences |
| **UI Pages** (`sys_ui_page`) | Legacy Jelly pages | ⚠️ | Almost never. Legacy. |
| **Homepages** (`sys_home`) | Legacy landing pages | ⚠️ | Almost never. Legacy. |

All of those tables exist on this instance, so availability is not the constraint
— the constraint is which one is *supported* for this job.

---

## 2. Why the dashboard container is not scriptable (the important finding)

I dumped the schema. `par_dashboard` has nine columns:

```
experience            reference   Experience
grid                  integer     Grid
sys_ux_app_config     reference   UX App Configuration
sys_ux_app_route      reference   UXF Page Route
sys_ux_macroponent    reference   UXF Page Macroponent
sys_ux_screen         reference   UX Screen
sys_ux_screen_type    reference   UX Screen Collection
```

**Five of the seven real columns point at UI Framework artefacts.** A dashboard
is a set of `sys_ux_*` records, each carrying JSON component configuration. To
create one over REST you would have to fabricate a screen, a screen type, an app
route, a macroponent and an app config, then wire the dashboard to all of them.

That is not "programmatic" in any useful sense: it is reverse-engineering an
internal format that is free to change between releases. It would break on
upgrade and nobody could maintain it.

The legacy path (`pa_dashboards`) is not an escape either — it now carries
`experience_dashboard`, `should_migrate_permissions` and `no_bulk_migration`,
i.e. it is the deprecated model being folded into PAR.

**So the supported split is:**

```
reports    →  scripted, version-controlled, re-runnable, testable
dashboard  →  assembled once in the UI, takes ~2 minutes,
              then automatically shows every report you script
```

That is also the honest reading of ServiceNow's own guidance: for read-only
operational visibility, use **Reports + Dashboards**. UI Builder is for
write-oriented experiences. UI Pages are legacy.

---

## 3. What was actually built

`specs/reports_toronto_events.json` + `create_reports.py` create **12 reports** on
`u_toronto_event`:

```bash
python create_reports.py specs/reports_toronto_events.json --dry-run
python create_reports.py specs/reports_toronto_events.json
```

Verified output: `RESULT: 12/12 reports present on u_toronto_event`, and a re-run
reports `created 0 · skipped 12 · failed 0` — idempotent, matched by title.

| Report | Type | Answers |
|---|---|---|
| **This Week** | single score | what's on right now (351 today) |
| **Upcoming** | single score | everything still ahead (2,433) |
| **Next 7 Days** | single score | rolling forward window (276) |
| **Free vs Paid** | donut | cost mix (10,376 free of 11,940) |
| **By Source** | bar | which feed/scraper contributed what |
| **By Status** | donut | will surface cancellations the moment the feed carries them |
| **By Duration Category** | pie | single vs multi-day |
| **Multi-Day** | single score | 878 |
| **Upcoming By Source** | horizontal bar | who is supplying what's actually ahead |
| **Added This Week** | single score | did the sync add anything, or was it a no-op |
| **No Coordinates** | single score | quality metric; 2,341 rows |
| **No Start Date** | single score | should stay near zero |

The two **data-quality** reports are the ones people forget to build. `u_lat` is
empty on 2,341 rows — without a metric, you discover that when a map report
silently renders blank months later.

---

## 4. "Start on this week" — use relative filters, never frozen dates

A report filtered to fixed dates is stale the following Monday. Use the relative
date functions so the content maintains itself:

| Intent | Encoded query | Measured today |
|---|---|---|
| this calendar week | `u_start>=javascript:gs.beginningOfThisWeek()^u_start<=javascript:gs.endOfThisWeek()` | **351** |
| rolling 7 days ahead | `u_start>=javascript:gs.now()^u_start<=javascript:gs.daysAgoStart(-7)` | **276** |
| anything upcoming | `u_start>javascript:gs.now()` | **2,433** |
| created by the sync this week | `sys_created_on>=javascript:gs.beginningOfThisWeek()` | varies |

Prefer **Next 7 Days** over **This Week** as the headline number: "this week"
collapses to zero every Monday morning, which looks like an outage. A rolling
window is what a planner actually wants.

If the dashboard should *default* to this week, set the dashboard's own date
filter rather than baking it into each report — then one control changes the whole
canvas.

---

## 5. Assembling the dashboard (one time, ~2 minutes)

1. **All → Dashboards → New dashboard**
2. Name it `Toronto Events`, pick the **Events** audience/roles
3. For each report: open it → **Add to dashboard** → choose `Toronto Events`
4. Arrange on the canvas. Suggested layout:

```
┌──────────────┬──────────────┬──────────────┐
│  This Week   │  Next 7 Days │   Upcoming   │   ← three KPIs across the top
├──────────────┴──────────────┴──────────────┤
│        By Source (bar)                     │
├─────────────────────┬──────────────────────┤
│  Free vs Paid donut │  By Status donut     │
├─────────────────────┴──────────────────────┤
│  Data quality row: no coords · no start date│
└────────────────────────────────────────────┘
```

When you add a new report later, `create_reports.py` creates it and you drop it
on the canvas — the container never needs rebuilding.

---

## 6. Permissions

Reports and dashboards inherit the table's ACLs, so **Part 5 of the README comes
first**. A dashboard is not a security boundary: if `u_toronto_event` is readable
by everyone (which it is until you add ACLs), every report on it is too.

Set the dashboard's audience to the same role that can read the table — `itil`
in our setup — so the two do not drift.

---

## 7. What not to do

| Anti-pattern | Why |
|---|---|
| UI Page (Jelly) for a data view | Legacy tech, no upgrade path, no reason to choose it |
| UI Builder for a read-only list | You get a macroponent to maintain instead of a report; only worth it if users must *act* |
| Hard-coded date ranges in reports | Stale content that silently looks like an outage |
| A dashboard as the only definition of the content | The reports are the durable, scriptable artefact — keep them in git |
| Assuming a dashboard enforces security | It does not; table ACLs do |
| `SELECT *`-style "show everything" reports | Every column the sync will break your layout |

---

## 8. Cleanup

Every report written by this script is listed in the spec, so removal is
symmetric:

```bash
python create_reports.py specs/reports_toronto_events.json --delete
```

It deletes only the titles named in the spec, and only from the spec's table.
