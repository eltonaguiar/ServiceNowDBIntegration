# Security model: what a non-admin can actually do

Unlike the rest of these docs, this page is **entirely empirical**. Every row
below was tested by authenticating as a real non-admin user against a live
instance, not inferred from ACL definitions.

## The test subject

| | |
|---|---|
| User | `svc.report.test` (sys_id `57dbb948c3189310310abf0d0501317e`) |
| Assigned roles | `itil`, `report_admin` — **no `admin`** |
| Effective roles | **57**, because both are containers (see below) |

## Verified results

| Operation | Result | Why |
|---|---|---|
| Authenticate over REST | ✅ OK | after fixing two flags (below) |
| **Read** `u_toronto_event` | ✅ **11,940 rows** | wildcard `*` read ACL permits it |
| **Create a report** (`sys_report`) | ✅ **succeeded** | granted by `report_admin` |
| Create a report *without* `report_admin` | ❌ **403** | proves `report_admin` is the gate |
| **Create a table** (`sys_db_object`) | ❌ **403** | non-admins cannot do DDL |
| **Insert a row** into `u_toronto_event` | ❌ **403** | see "the write surprise" |

## Three findings that contradict common assumptions

### 1. A table with no ACLs is **not** writable by everyone

Earlier chapters of this repo claimed "no ACLs means readable *and* writable by
any authenticated user". **The writable half is wrong.**

`u_toronto_event` has zero table-specific ACLs, `super_class` is empty, and yet a
non-admin insert returns:

```
HTTP 403  ACL Exception Insert Failed due to security constraints
```

The reason: there are **28 wildcard ACLs named `*`** on the instance. They cover
`read`, `create`, `write`, `delete` and apply to every table. The read ones allow;
the create/write ones do not (most are scripted against
`gs.getProperty('glide.sm.default_mode')`). So a custom table inherits a
restrictive baseline rather than being wide open.

**Practical consequence:** the nightly sync **cannot run as this user**. A
least-privilege integration account needs an explicit create/write ACL on the
target table, or a role the wildcard ACLs accept.

### 2. `com.snc.par.coreui.report_create.enabled` is not the real gate here

The `sys_report` create ACL is scripted:

```js
var answer = false;
var isCreateReportEnabled = gs.getProperty('com.snc.par.coreui.report_create.enabled','false');
if (isCreateReportEnabled == 'true') { ...role checks... }
```

The property is **`false`**, so that ACL denies. Yet a non-admin with
`report_admin` created a report successfully. There is a **second** create ACL on
`sys_report` with an empty script, and ServiceNow grants access if **any** matching
ACL allows. The empty one wins; the property is inert on this instance.

Do not rely on that property to control report creation. Grant or withhold
`report_admin` instead — that demonstrably does control it.

### 3. "itil + report_admin" is not a small grant

Both are **containers**:

```
report_admin contains 8 roles  -> report_global, report_group, report_publisher,
                                  report_scheduler, gauge_maker,
                                  analytics_filter_admin, report_alias_admin,
                                  viz_admin
itil        contains 26 roles  -> cmdb_read, sn_incident_write,
                                  agent_workspace_user, sn_problem_write,
                                  cmdb_query_builder, sn_request_write,
                                  sn_sow.sow_user, ...
```

Two assigned roles became **57 effective roles**. `itil` alone grants
`sn_incident_write` and `sn_request_write` — it is not a read-only role.

Choose roles by what they **contain** (`sys_user_role_contains`), never by name.

## Two flags that silently block REST authentication

Setting a password through the REST API appears to succeed and then does not
work. Two things must also be true:

| Field | Why it matters |
|---|---|
| `password_needs_reset` | **`true` blocks API authentication entirely.** A UI password-set with "must change password" ticked leaves this set. Clear it. |
| `internal_integration_user` | Set `true` for service accounts — exempts them from interactive-login and password-rotation rules. |

Also note the field is **`web_service_access_only`**, not `web_service_access`;
writing the wrong name is silently ignored.

And fundamentally: **`user_password` cannot be usefully set over REST.** A PATCH
returns `200`, and the next login returns `401`. Set passwords in the UI. This
was also observed for MID Server users earlier in the project.

## Least-privilege recipe that actually works

1. Create the user, set `internal_integration_user=true`, `password_needs_reset=false`
2. Set the password **in the UI**
3. Grant a role that satisfies create/write on the target table — the wildcard
   ACLs will otherwise deny writes regardless of `import_admin`
4. Do **not** grant `itil` unless the account genuinely needs incident/request
   write access
5. Grant `report_admin` only if the account must create reports
6. Verify with the same tests used here — read, one insert, one report create

## Cleanup

The test user remains on the instance as a working example:

```
sys_user  svc.report.test   57dbb948c3189310310abf0d0501317e
```

To remove it: delete that user record (and the `sys_user_has_role` rows it owns).
