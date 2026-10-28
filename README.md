# ServiceNowDBIntegration

Scripts and a guide for **bringing an external database into ServiceNow** — and for
**creating the tables it lands in, programmatically** instead of clicking through the UI.

Built and validated against a real (non-production) ServiceNow instance. Every
number and error message in this README was observed, not guessed.

---

## Table of contents

1. [What this repo gives you](#1-what-this-repo-gives-you)
2. [The mental model](#2-the-mental-model)
3. [Part 1 — How do you get data *into* ServiceNow?](#part-1--how-do-you-get-data-into-servicenow)
   - [The five options, with pros and cons](#the-five-options-with-pros-and-cons)
   - [How to choose](#how-to-choose-a-decision-order)
4. [Part 2 — Creating tables programmatically](#part-2--creating-tables-programmatically)
5. [Part 3 — Field types, and the precision trap](#part-3--field-types-and-the-precision-trap)
6. [Part 4 — Verify, don't assume](#part-4--verify-dont-assume)
7. [Part 5 — ACLs (the one thing you must do in the UI)](#part-5--acls-the-one-thing-you-must-do-in-the-ui)
8. [Part 6 — Troubleshooting](#part-6--troubleshooting)
9. [Command cheat sheet](#command-cheat-sheet)
10. [Repo layout](#repo-layout)
11. [Security](#security)
12. [What was validated](#what-was-validated)

### Deeper documentation

| Document | Read it when you need to… |
|---|---|
| [`docs/SCHEDULING.md`](docs/SCHEDULING.md) | run it nightly at 03:00, or change a database URL / credential |
| [`docs/HOW_IT_TALKS_TO_SERVICENOW.md`](docs/HOW_IT_TALKS_TO_SERVICENOW.md) | understand the auth, or copy a curl / PowerShell example |
| [`docs/REBUILD.md`](docs/REBUILD.md) | rebuild everything after an instance expires |
| [`FRESH_INSTANCE_STEPS.md`](FRESH_INSTANCE_STEPS.md) | stand this up against a brand-new instance |
| [`docs/FINDINGS.md`](docs/FINDINGS.md) | see what was tested, what broke, and how it was debugged |

---

## 1. What this repo gives you

| File | Purpose |
|---|---|
| `sn_api.py` | Tiny ServiceNow Table API client. Standard library only — no `pip install`. |
| `create_table.py` | Creates a table **and all its columns** from a JSON spec. Idempotent. |
| `verify_table.py` | Writes a probe row, reads it back, compares every value, deletes it. |
| `create_acl.py` | Tries to create ACLs, then explains why it can't and what to do instead. |
| `specs/u_toronto_event.json` | A complete, working 21-field spec you can copy. |
| `.env.example` | Template for credentials. The real `.env` is gitignored. |

**You do not need to be a ServiceNow developer to use this.** You need an
instance URL, a username, and a password.

> ### ELI5 — What is this, really?
> ServiceNow is a big online filing cabinet. Everything in it is a **table** —
> a sheet with rows and columns, like a spreadsheet.
>
> Normally a human clicks "New Table", then clicks "New Column" twenty-one
> times. That's slow, and impossible to repeat exactly.
>
> This repo does the same clicks, but written down as a script. You describe the
> table once in a text file, run one command, and it appears. Run it again and
> nothing breaks — it only adds what's missing.

---

## 2. The mental model

Three things, and that's the whole system:

```
sys_db_object     ← one row per TABLE      ("here is a cupboard called u_toronto_event")
sys_dictionary    ← one row per COLUMN     ("this cupboard has a shelf called u_title")
/api/now/table/…  ← the actual DATA        ("here are the things on the shelf")
```

Creating a table in ServiceNow is **not** a special "create" operation. It is an
`INSERT` into `sys_db_object`. Creating a column is an `INSERT` into
`sys_dictionary`. Adding a record is a `POST` to `/api/now/table/<name>`.

Once you see that, the whole thing stops being magic.

> ### ELI5 — the three layers
> Think of a **library**.
>
> - `sys_db_object` is the **card catalogue**: a list of which bookcases exist.
> - `sys_dictionary` is the **label on each shelf**: what's on it and what shape it holds.
> - `/api/now/table/…` is the **books themselves**.
>
> To build a new bookcase you add a card to the catalogue. The bookcase appears.
> You never "build furniture" — you write a card. That's why a script can do it.

---

# Part 1 — How do you get data *into* ServiceNow?

Before any code, pick the mechanism. This is the decision that matters most,
and it's the one people get wrong.

The first question is always the same: **can the ServiceNow instance reach your
database directly?**

Almost always the answer is **no**. ServiceNow lives in a vendor's cloud; your
database lives on your machine, or on a shared host, or behind a home router.
There is no route between them.

So every option collapses into one of two shapes:

```
PULL   something on YOUR side dials out and brings data in
PUSH   your script dials ServiceNow and hands data over
```

Push needs nothing but an internet connection and an API key. Pull needs a
**MID Server** — a small agent you run on your own network that ServiceNow can
give orders to.

> ### ELI5 — Pull vs Push
> You want groceries in your kitchen.
>
> **Pull** = you hire a delivery driver (the MID Server) who sits in your
> neighbourhood, and the shop phones the driver to go fetch things. Works, but
> now you employ a driver.
>
> **Push** = you walk to the shop and carry the bags home yourself. No driver,
> no salary, no scheduling — but *you* have to remember to go.
>
> Most small integrations should be Push. Pull earns its keep when the data must
> stay behind a firewall you refuse to open, or when the vendor insists on
> driving.

---

## The five options, with pros and cons

Click any option to expand it. The comparison table and decision order stay
visible below — collapsing is only to keep the page readable.

<details>
<summary><strong>Option 1 — Remote Table (a "scripted virtual table")</strong> &nbsp;·&nbsp; live view, nothing stored</summary>

Looks like a real table in ServiceNow. Has columns, shows in lists, supports
ACLs. But the rows aren't stored anywhere — a **script** produces them fresh
every time someone looks.

It's an `INSERT` into `sys_script_vtable`, with a script shaped like:

```js
(function executeQuery(v_table, v_query) {
    v_table.addRow({ sys_id: 'abc123', title: 'Some event' });
})(v_table, v_query);
```

**The catch nobody expects:** the script runs *on the ServiceNow instance*, so it
**cannot open a database connection**. To use a Remote Table over your MySQL
database you must first stand up an HTTP endpoint that queries the DB and returns
JSON, then have the script call it. That endpoint is work you were probably
trying to avoid.

| | |
|---|---|
| ✅ Live data — no staleness | ❌ Needs an HTTP shim in front of your DB anyway |
| ✅ No copy to keep in sync | ❌ Read-only (writes need you to implement them) |
| ✅ Supports table **and** field-level ACLs | ❌ No business rules, so no server-side validation |
| ✅ Good for reference/lookup data | ❌ `is_extendable = false` — no table hierarchy |
| | ❌ Can't be used as a CMDB CI |
| | ❌ ACLs are re-evaluated per row — slow on large result sets |
| | ❌ Your script must honour the user's filter itself, or everyone sees everything |

> ### ELI5 — Remote Table
> A window in the wall of your house that looks into the neighbour's kitchen.
> You're not copying their food into your fridge — you're just *looking*.
>
> Always current, takes no space. But you can't eat what you see, you can't
> reorganise it, and if the neighbour's window is shut (no HTTP endpoint), you
> see nothing at all.

</details>

---

<details>
<summary><strong>Option 2 — Import Set, pulling over JDBC</strong> &nbsp;·&nbsp; the enterprise pattern</summary>

A **data source** of type JDBC connects to your database, reads a table, drops
the rows into a staging area, then a **transform map** copies them into a real
ServiceNow table. Runs on a schedule.

Needs: a JDBC driver (a `.jar` file) installed on the MID Server, plus the
connection details.

| | |
|---|---|
| ✅ The "real enterprise" pattern — this is how most production integrations work | ❌ Needs a JDBC driver `.jar` dropped into the MID's `extlib` folder |
| ✅ Target is a real table: ACLs, reports, relationships, business rules | ❌ Needs a reachable network path from MID → DB on port 3306 |
| ✅ Handles millions of rows | ❌ More moving parts than a push |
| ✅ Coalescing makes re-runs safe | ❌ Snapshot, not live |

> ### ELI5 — JDBC pull
> You hire the delivery driver (MID Server) and give them a key to the warehouse
> (the JDBC driver). Every night they drive over, load a van, and stack the boxes
> in your garage (the staging table). A robot then unpacks each box onto the
> correct shelf (the transform map).
>
> Proper, reliable, and a lot of setup. Worth it at scale.

</details>

---

<details>
<summary><strong>Option 3 — Import Set, pulling a JSON/CSV file over HTTP</strong> &nbsp;·&nbsp; no driver needed</summary>

Same as Option 2, but instead of JDBC the data source downloads a **file from a
URL**. ServiceNow natively supports JSON and CSV, and you can route the download
through the MID Server.

| | |
|---|---|
| ✅ No JDBC driver needed | ❌ The whole file is fetched in one go — no byte-range chunking |
| ✅ JSON is supported out of the box | ❌ A large file is one big transaction |
| ✅ Can be scheduled | ❌ Needs the file to be parseable as a flat array of objects |
| ✅ Exercises the MID Server if you route through it | ❌ Nested arrays become awkward |

> ### ELI5 — JSON file pull
> Instead of a key to the warehouse, the driver just downloads a spreadsheet from
> a website. Simpler — no key needed. But they download the *entire* spreadsheet
> every time, even if only three rows changed.

</details>

---

<details>
<summary><strong>Option 4 — Import Set, pushed via the REST Import Set API</strong> &nbsp;·&nbsp; cheapest, easiest to debug</summary>

Your own script — Python, PHP, whatever you already have — slices the data into
batches and posts them:

```
POST /api/now/import/<staging_table>
```

| | |
|---|---|
| ✅ **No MID Server, no JDBC driver, no firewall change** | ❌ You own the scheduling |
| ✅ You control batch size exactly — true chunking | ❌ If your machine is off, nothing arrives |
| ✅ Easy to make idempotent with a coalesce key | ❌ Still needs a transform map |
| ✅ Easiest to debug: it's just an HTTP POST | |
| ✅ Works with any data source you can read | |

> ### ELI5 — REST push
> You carry the bags to the shop yourself, in trips. Twenty small trips instead
> of one huge one. If one trip fails, you just do that trip again — nothing is
> lost and nothing is duplicated (thanks to the coalesce key).
>
> Cheapest and simplest option. The trade-off is that remembering to go is now
> your job.

</details>

---

<details>
<summary><strong>Option 5 — MID Server custom probe + sensor</strong> &nbsp;·&nbsp; most power, steepest curve</summary>

Write your own **probe** that runs on the MID (SSH, PowerShell, a script), and a
**sensor** that parses its output. This is the most powerful AND most
low-level option — you're programming the Discovery engine directly.

| | |
|---|---|
| ✅ Runs anywhere the MID can reach, using protocols ServiceNow doesn't natively speak | ❌ You write and maintain the parsing logic |
| ✅ Full control over exactly what is collected | ❌ Steepest learning curve |
| ✅ Teaches you how Discovery actually works under the hood | ❌ Not really "an integration" — more like remote scripting |

> ### ELI5 — Custom probe
> You don't use a delivery driver at all. You teach your own robot to climb
> through a window, count the boxes, and write the count on a clipboard.
>
> Incredibly flexible. Also now you own a robot.

</details>

---

### What is *not* available by default

Two things people assume exist and often don't:

- **IntegrationHub action steps** (drag-and-drop "REST", "SOAP", "SSH" steps in
  Flow Designer). These require the IntegrationHub subscription. On a basic
  developer instance the Flow Designer palette may contain only "Copy Restarted
  Activity Flow Values" and "Restart a Container" — no HTTP step at all.
- **Orchestration activities** ("Run SSH Command", "Run PowerShell") inside
  classic Workflow. Requires the Orchestration plugin, which is not installed by
  default.

The workaround for both: call `RESTMessageV2` from a **Script Include**, a
**Business Rule**, a **Scheduled Job**, or a Flow's **Script step**. That path is
always available and unrestricted.

> ### ELI5 — the missing buttons
> The fancy pre-built connector buttons are a premium feature. Without them you
> write a few lines of JavaScript that does the same HTTP call by hand. Slightly
> more typing, zero extra cost.

---

## How to choose: a decision order

Answer these in order and stop at the first match.

| # | Question | If yes → |
|---|---|---|
| 1 | Do you need it as a **CMDB CI**, joined to other records, or reported on? | **Options 2 / 3 / 4** — you need a real table, never a Remote Table |
| 2 | Can you add ~20 lines of code on your own machine? | **Option 4 (push)** — cheapest by a wide margin |
| 3 | Must the data be **live**, and is it small reference data? | **Option 1 (Remote Table)** |
| 4 | Is the source a system you cannot modify? | **Option 2 (JDBC)** or **Option 5 (probe)** |
| 5 | Is the row count in the hundreds of thousands+? | **Option 2 (JDBC)** |

### The comparison, all at once

| | Remote Table | JDBC pull | JSON pull | **REST push** | Custom probe |
|---|---|---|---|---|---|
| Real table (ACLs, reports, relations) | ❌ | ✅ | ✅ | ✅ | ✅ |
| Needs MID Server | ❌ | ✅ | optional | ❌ | ✅ |
| Needs extra driver | ❌ | ✅ | ❌ | ❌ | depends |
| Live vs snapshot | **live** | snapshot | snapshot | snapshot | snapshot |
| Chunkable | n/a | yes | transform only | **yes, naturally** | yes |
| Effort | high | high | medium | **low** | high |
| Beginner friendly | ❌ | ❌ | ✅ | **✅** | ❌ |

> ### ELI5 — the summary
> If you just want data in ServiceNow and you can write a Python script, **push
> it** (Option 4). It is the shortest path from A to B and the easiest to debug.
>
> Reach for the MID Server and JDBC when somebody else owns the database and you
> are not allowed to touch it.
>
> Reach for a Remote Table only when "now" really must mean now.

---

# Part 2 — Creating tables programmatically

## First: which of the five options is this?

**None of them — and that's deliberate.** Part 2 is the *destination* half of
every option. Options 2, 3, 4 and 5 all need a real ServiceNow table to land in,
and that table is exactly what this section builds. Whichever transport you
picked in Part 1 plugs into it unchanged.

The worked example — `u_toronto_event` — is aimed at **Option 3 (JSON pull)** and
works identically for **Option 4 (REST push)**. What it is deliberately *not* is
a Remote Table:

| A Remote Table would… | Why that's wrong for this data |
|---|---|
| need an HTTP shim in front of MySQL | more work than just pushing (Option 4) |
| be read-only | the transform map has to write rows |
| have no business rules | 2,341 of 11,945 rows have null coordinates needing handling |
| refuse to participate in the CMDB | blocks ever relating an event to a CI |
| re-check ACLs on every row | noticeable at 11,945 rows |
| not survive a ServiceNow restart | nothing is stored — it re-runs the script every query |

And why Option 3/4 rather than Option 2 (JDBC)?

- The source is **already** a public JSON endpoint. No driver, no port 3306, no
  network path to arrange.
- 11,945 rows fits comfortably in one fetch — no chunking needed.
- Both land in the same table, so switching later costs nothing.

### What this repo automates, and what it doesn't

| Step | Automated here? | How |
|---|---|---|
| Create the target table | ✅ | `create_table.py` |
| Create all 21 columns | ✅ | `create_table.py` |
| Prove the columns store what you send | ✅ | `verify_table.py` |
| Row counts and field inspection | ✅ | `sn_api.py` |
| Create ACLs | ❌ | UI only — platform restriction, see [Part 5](#part-5--acls-the-one-thing-you-must-do-in-the-ui) |
| Create the data source | ❌ | ServiceNow form (automatable via `POST sys_data_source` — not attempted in this repo) |
| Create the transform map | ❌ | ServiceNow form |
| Schedule the import | ❌ | ServiceNow form (`scheduled_import_set`) |

The bottom four are one-time setup through forms, and they encode decisions you
want to be deliberate about — which fields map where, what coalesces, how often
it runs. Writing the table first means those forms have something to point at.

> ### ELI5 — what this section actually is
> Part 1 was about **how the boxes get delivered**. This section is about **where
> you put the shelves**.
>
> You build the shelves once. Then any delivery method — van, courier, or you
> carrying them — puts boxes on the same shelves.
>
> The shelves are described in a text file. The script builds them. If you run it
> twice, it notices the shelves already exist and builds nothing new.

## Setup (once)

```bash
cp .env.example .env
# edit .env: SN_INSTANCE, SN_USER, SN_PASS
```

`.env` is gitignored. Credentials never appear in the source, in the command
line, or in your shell history.

> Use a **dedicated integration user** once you're past experimenting. Creating
> tables and columns needs `admin` on most instances; a narrower setup is
> discussed in [Security](#security).

## Quickstart

```bash
# 1. See the plan without changing anything
python create_table.py specs/u_toronto_event.json --dry-run

# 2. Create it
python create_table.py specs/u_toronto_event.json

# 3. Prove the columns actually store what you send
python verify_table.py u_toronto_event
```

Expected output from step 3:

```
RESULT: PASS -- 21/21 columns verified
```

## Writing your own spec

A spec is a JSON file describing one table:

```json
{
  "table": "u_my_data",
  "label": "My Data",
  "scope": "global",
  "fields": [
    { "element": "u_external_id", "label": "External ID", "type": "string", "max_length": 64 },
    { "element": "u_name",        "label": "Name",        "type": "string", "max_length": 255 },
    { "element": "u_amount",      "label": "Amount",      "type": "decimal", "max_length": 15 },
    { "element": "u_occurred",    "label": "Occurred",    "type": "glide_date_time" },
    { "element": "u_is_active",   "label": "Is Active",   "type": "boolean" }
  ]
}
```

Rules worth knowing:

- **`table` is permanent.** Renaming a table afterwards is painful. Choose once.
- **Custom global tables start with `u_`.** Columns in them start with `u_` too.
  This is convention, and the platform's tooling assumes it.
- **Give every row an external ID.** You will need a stable, unique key to
  coalesce on later, so imports update instead of duplicating.
- **Idempotent by design.** Re-running creates only what's missing and reports
  drift. It never drops anything.

> ### ELI5 — the spec file
> It's a shopping list. "I want a cupboard called `u_my_data` with a shelf for
> names, a shelf for amounts, a shelf for dates."
>
> You write the list once. The script goes shopping. If you run it twice, it sees
> you already own the cupboard and the shelves, and buys nothing — it doesn't
> build a second cupboard.

---

# Part 3 — Field types, and the precision trap

## Type reference

These types are verified working via the Table API:

| `type` | Meaning | Notes |
|---|---|---|
| `string` | Text | **Always set `max_length`.** Silently truncates beyond it. |
| `boolean` | True/False | Send as the strings `"true"` / `"false"`. |
| `integer` | Whole number | |
| `decimal` | Fixed-point number | **Precision comes from `max_length`** — see below. |
| `float` | Floating-point number | Same `max_length` rule. Use for coordinates. |
| `glide_date_time` | Date and time | Format `YYYY-MM-DD HH:MM:SS` |
| `url` | Hyperlink | `max_length` 500 is typical |
| `email` | Email address | |
| `phone_number` | Phone | |
| `GUID` | 32-char sys_id | Auto-created as `sys_id` |

## ⚠️ The precision trap

This one is worth the whole README.

On a real instance, a column declared like this:

```json
{ "element": "u_lat", "label": "Latitude", "type": "decimal", "max_length": 15 }
```

accepted `43.667712` and returned **`43.67`**.

No error. No warning. The dictionary was valid, the insert succeeded, and the
value was silently rounded to two decimal places.

The fix — `max_length` controls precision, not just length:

```json
{ "element": "u_lat", "label": "Latitude", "type": "float", "max_length": 40 }
```

Re-tested: `43.667712` in, `43.667712` out.

**The general rule: after creating any numeric column, run `verify_table.py`.**
Nobody catches this by reading the schema. You catch it by round-tripping a value
with more than two decimal places.

> ### ELI5 — the precision trap
> You ask for a shelf "15 units wide" for your ruler collection. The shelf gets
> built, everything fits — but the shop quietly decided that 15 means "two
> decimal places", so it throws away the rest of your measurements.
>
> `43.667712` becomes `43.67`. Your map pin moves by a kilometre and nothing told
> you.
>
> The lesson: a schema that *looks* fine isn't proof. You have to put a real
> value in and read it back out.

---

# Part 4 — Verify, don't assume

`verify_table.py` does four things:

1. Lists the table's columns.
2. Builds a probe row with a representative value per type.
3. Inserts it, reads it back, and compares **exactly**.
4. Deletes the probe row.

```bash
python verify_table.py u_toronto_event
python verify_table.py u_toronto_event --keep   # leave the row for inspection
```

Failure looks like this — and this is what success is measured against:

```
DRIFT  u_lat   decimal   sent='43.667712'   got='43.67'
RESULT: FAIL -- 1 column(s) did not round-trip
```

Exit code is `0` on pass, `1` on failure — so you can wire it into CI.

### When rounding is what you wanted

A currency column *should* keep two decimal places. The probe value `43.667712`
will therefore always "fail" against a money field — correctly configured or not.
Declare that intent instead of ignoring the failure:

```bash
python verify_table.py u_toronto_event --ignore u_price_amount
#   ignoring (expected to round): u_price_amount
#   RESULT: PASS -- 20/20 columns verified
```

Use `--ignore` only for columns where rounding is the *design*, never to make a
red result go green.

> ### ELI5 — why bother testing a table
> Imagine buying a suitcase online. The listing says "fits 20 kg". It arrives,
> looks right, feels right.
>
> Then you pack 20 kg and the zip bursts.
>
> The listing was the schema. Packing it was the round-trip test. Only one of
> those tells you the truth.
>
> Do this once per table. It costs ten seconds and saves you discovering
> corrupted data six weeks later, in a report, in front of someone important.

---

# Part 5 — ACLs (the one thing you must do in the UI)

By default, **a new table has no ACLs — and no ACLs means access granted.** Your
brand-new table is readable *and writable* by any authenticated user. That
surprises people.

Creating ACLs over REST **does not work**. Verified:

```
POST /api/now/table/sys_security_acl
  {"name":"u_toronto_event","operation":"read","requires_role":"<itil>"}

→ HTTP 403
  "ACL Exception Insert Failed due to security constraints"
```

The `admin` user already held the `security_admin` role, and it still failed.
ACL writes require an **elevated session**, which is a browser concept — REST
has no equivalent. `create_acl.py` will confirm this on your instance and print
the fix:

```bash
python create_acl.py u_toronto_event --read-role itil --write-role admin
```

## Doing it in the UI

1. Click your avatar (top right) → **Elevate roles** → tick **`security_admin`** → OK.
   *(Per-session; expires on logout or timeout.)*
2. **System Security → Access Control (ACL) → New**

   | Field | Read ACL | Write ACL |
   |---|---|---|
   | Type | `record` | `record` |
   | Name | `u_toronto_event` | `u_toronto_event` |
   | Operation | `read` | `write` |
   | Active | ✅ | ✅ |
   | Requires role | `itil` | `admin` |

3. Leave the script condition empty. Empty script + role requirement means
   "allow if the user holds this role".

**Why split read and write?** The only writer should ever be your import job. If
everyone can write, hand-edits and your next sync will fight each other and both
will lose — silently.

**Field-level ACLs** work the same way, with a name like
`u_toronto_event.u_description`. Useful when one field is more sensitive than the
rest of the record.

> ### ELI5 — ACLs
> An ACL is a guest list on a door.
>
> A new table arrives with **no door and no list** — anyone wandering past can
> walk in, read the files, and rearrange the furniture.
>
> You can add the guest list by writing a script — except the platform won't let
> a script touch the guest list. It only accepts one written by a person who has
> formally announced "I am acting as a security administrator today" by clicking
> **Elevate roles**. That's a deliberate safety catch: it stops an automation bug
> from quietly opening every door on the system.

---

# Part 6 — Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `400` from `/api/now/table/<name>` | Table doesn't exist | That's the *expected* way to detect absence, not an error to chase |
| `403 ACL Exception Insert Failed due to security constraints` | ACL write without elevated session | UI only — see Part 5 |
| Number comes back rounded to 2 dp | `decimal`/`float` with small `max_length` | Raise `max_length` to 40; re-verify |
| Text is cut off | `max_length` too small | Widen it. Existing data stays truncated. |
| `401 Unauthorized` on every call | Wrong password, or an account with no web-service access | Test with the same creds in a browser first |
| Table exists but has no columns | Table created, columns failed | Re-run `create_table.py` — it's idempotent and reports what's missing |
| Column created but empty on every row | Column name doesn't match your source key | Names are matched literally; check case |
| Two imports produced duplicates | No coalesce key | Add a unique external ID column and coalesce on it |
| Everything is slow on a Remote Table | Per-row ACL evaluation | Snapshot into a real table instead |

---

# Command cheat sheet

```bash
# --- credentials -----------------------------------------------------------
cp .env.example .env                 # then edit it

# --- schema ----------------------------------------------------------------
python create_table.py specs/my.json --dry-run     # plan only
python create_table.py specs/my.json               # create table + columns
python verify_table.py u_my_table                  # round-trip test
python verify_table.py u_my_table --keep           # leave the probe row
python verify_table.py u_my_table --ignore u_amount  # currency: rounding is intended

# --- inspect ---------------------------------------------------------------
python sn_api.py probe sys_user u_my_table no_such_table
python sn_api.py count sys_user
python sn_api.py columns u_my_table

# --- security --------------------------------------------------------------
python create_acl.py u_my_table --read-role itil --write-role admin
```

Raw `curl` equivalents, if you prefer to see the metal:

```bash
# row count without downloading rows
curl -s -D- -o /dev/null -u "$SN_USER:$SN_PASS" \
  "https://$SN_INSTANCE/api/now/table/sys_user?sysparm_fields=sys_id&sysparm_limit=1" \
  | grep -i '^x-total-count'

# read
curl -s -u "$SN_USER:$SN_PASS" -H 'Accept: application/json' \
  "https://$SN_INSTANCE/api/now/table/u_my_table?sysparm_query=ORDERBYu_name&sysparm_limit=5"

# create a column
curl -s -u "$SN_USER:$SN_PASS" -H 'Accept: application/json' \
  -H 'Content-Type: application/json' -X POST \
  -d '{"name":"u_my_table","element":"u_note","column_label":"Note","internal_type":"string","max_length":"500"}' \
  "https://$SN_INSTANCE/api/now/table/sys_dictionary"
```

## `sysparm_query` — the encoded query language

Every list read uses it. It's a URL-encoded string where `^` means AND:

| Operator | Meaning | Encoded form |
|---|---|---|
| `^` | AND | `%5E` |
| `=` | equals | `%3D` |
| `LIKE` | contains | `LIKE` |
| `STARTSWITH` | begins with | `STARTSWITH` |
| `ISNOTEMPTY` | not null | `ISNOTEMPTY` |
| `IN` | in list | `IN` |
| `ORDERBY` | sort | `ORDERBYelement` |

Example: `name=u_my_table^NelementISNOTEMPTY^ORDERBYelement`
→ "rows for this table, where element isn't blank, sorted by element".

---

## Repo layout

```
.
├── README.md
├── .env.example              # template; real .env is gitignored
├── .gitignore
├── sn_api.py                 # the client everything else uses
├── create_table.py           # spec → table + columns (idempotent)
├── verify_table.py           # round-trip proof: write, read back, compare
├── create_acl.py             # attempts ACLs; explains the platform limit
├── fresh_instance.sh         # one-shot bootstrap: prompts or pre-fill, then builds
├── FRESH_INSTANCE_STEPS.md   # copy-paste guide (script path and manual path)
├── import_events.py          # the sync: upsert + batch stamp + sweep + dedupe
├── deploy/
│   ├── sn-events-sync.service   # systemd unit (03:00 America/Toronto)
│   ├── sn-events-sync.timer
│   └── run-sync.sh              # cron wrapper
├── specs/
│   ├── u_toronto_event.json  # target table, 22 fields
│   └── u_ev_stage.json       # staging table (pull variant)
├── tests/
│   ├── test_sync.py          # 27 behavioural assertions
│   └── test_loads.py         # repeated-load / duplicate-detection harness
└── docs/
    ├── SCHEDULING.md         # triggers, timezone, credential rotation
    ├── HOW_IT_TALKS_TO_SERVICENOW.md  # auth + curl/PowerShell examples
    ├── REBUILD.md            # disaster recovery in ~15 minutes
    └── FINDINGS.md           # what broke, how it was found, test evidence
```

---

# Security

- **No credentials in this repo.** Everything comes from environment variables,
  optionally seeded from a gitignored `.env`.
- **No credentials on the command line.** `curl -u` puts them in your shell
  history and in `ps` output; the scripts use an `Authorization` header instead.
- **Use a dedicated integration user.** Not your admin account, once you're past
  learning.
- **Minimum viable roles.** Creating tables and columns effectively needs
  `admin` on most instances. If that's too much, the alternatives are
  `personalize_dictionary` plus the relevant import roles — narrower, but you
  may hit ACL walls on some operations.
- **ACLs are not optional.** A table with no ACLs is open to every authenticated
  user, for read *and* write. Do Part 5.

> ### ELI5 — the password rule
> Think of a password as a house key.
>
> Don't leave a copy taped inside the front-door frame (hardcoded in a file
> someone might publish). Don't shout it while walking down the street (typed
> into a command that other programs can read).
>
> Keep it in one locked drawer (`.env`, gitignored) and give the script a way to
> open that drawer. That's all the `.env` pattern is.

---

# What was validated

Everything below was observed on a live instance, not inferred:

| Claim | Evidence |
|---|---|
| Table created via `POST sys_db_object` | `u_toronto_event`, sys_id returned, immediately queryable |
| 21 columns created via `POST sys_dictionary` | 21/21 returned HTTP 201 |
| Re-running `create_table.py` is safe | second run: 21/21 "already exists, skipping", reconcile 21/21 ok |
| `decimal` + `max_length=15` truncates to 2 dp | `43.667712` → `43.67` (reproduced twice: lat/lng, then `u_price_amount`) |
| `float` + `max_length=40` preserves precision | `43.667712` → `43.667712` |
| Round-trip verification catches it | `verify_table.py` flagged the drift automatically, exit code 1 |
| ACL write blocked over REST | HTTP 403 "security constraints", with `security_admin` already held |
| Import Set REST API reachable | `POST /api/now/import/<table>` → HTTP 400 on empty body (handler live) |
| JSON is a supported import format | `sys_data_source` format choices include JSON; extension whitelist includes `json` |
| Remote Tables support table + field ACLs | 20 ACLs found on `v_st_*` virtual tables |
| Remote Tables are scripted, not JDBC-backed | `sys_script_vtable.script` contains `executeQuery` bodies; no JDBC binding |
| No menu module for scripted virtual tables | `sys_app_module` search returned none; navigate via `sys_script_vtable.list` |

---

## Licence / status

Personal reference implementation. No warranty. Read the code before running it
against anything that matters — particularly `create_table.py`, which writes to
`sys_dictionary` and is therefore capable of changing your instance's schema.
