# How this actually talks to ServiceNow

Everything here is the same mechanism: **HTTPS + HTTP Basic authentication
against the ServiceNow Table API**. No SDK, no plugin, no agent. If you can make
an HTTPS request, you can do this — from bash, PowerShell, Python, Postman, or a
phone.

---

## 1. The connection, in one paragraph

```
POST/GET/PATCH/DELETE  https://<instance>/api/now/table/<table>
Authorization: Basic base64(<user>:<password>)
Accept: application/json
Content-Type: application/json   (only when sending a body)
```

That's the entire protocol surface used by this repo. `sn_api.py` is a ~200-line
wrapper over it, using only the Python standard library so there is nothing to
`pip install`.

### Basic authentication, precisely

Basic auth means: concatenate `username`, a colon, and `password`; base64-encode
the result; send it in an `Authorization` header.

```
user:pass  ->  dXNlcjpwYXNz  ->  Authorization: Basic dXNlcjpwYXNz
```

Three consequences worth internalising:

1. **Base64 is encoding, not encryption.** Anyone who sees the header can read
   the password instantly. It is only safe because the transport is HTTPS. Never
   send these calls over plain `http://`.
2. **The password is sent on every single request** — there is no session token.
   That's why it belongs in a header from an env var, never in a URL, never on
   the command line.
3. **The account needs API access**, not just a UI login. A user with no roles
   can often browse the UI and still get `401` from the API.

### Where the credentials live

| Layer | Variable | Set by |
|---|---|---|
| the scripts | `SN_INSTANCE`, `SN_USER`, `SN_PASS` | `sn_api.py` reads them |
| local dev | same three | `.env` in the repo root (**gitignored**) |
| production | same three | `/etc/sn-sync.env`, mode `0600`, read via systemd `EnvironmentFile=` |

Precedence: **process environment wins**, then `.env`. Nothing is hardcoded and
nothing is committed — `git grep` for the password returns nothing.

---

## 2. The exact calls used to build this integration

### 2.1 Create a table — `POST sys_db_object`

A "table" is one row in `sys_db_object`. The platform materialises the physical
table in the same transaction.

**bash / curl (Linux, macOS, WSL, Git Bash):**

```bash
curl -sS -u "$SN_USER:$SN_PASS" \
  -H 'Accept: application/json' -H 'Content-Type: application/json' \
  -X POST \
  -d '{"name":"u_toronto_event","label":"Toronto Event","sys_scope":"global","is_extendable":"false"}' \
  "https://$SN_INSTANCE/api/now/table/sys_db_object"
```

**PowerShell 7 (or Windows PowerShell 5.1):**

```powershell
$pair   = "${env:SN_USER}:${env:SN_PASS}"
$header = @{ Authorization = "Basic " + [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes($pair))
             Accept = "application/json" }

$body = @{
    name          = "u_toronto_event"
    label         = "Toronto Event"
    sys_scope     = "global"
    is_extendable = "false"
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Headers $header -ContentType "application/json" `
  -Uri "https://$env:SN_INSTANCE/api/now/table/sys_db_object" -Body $body
```

> `Invoke-RestMethod` handles the base64 for you if you prefer:
> `-Credential (Get-Credential)` — but then the password is prompted, which is
> useless for a scheduled job. The explicit header above is what a scheduler can
> use.

### 2.2 Create a column — `POST sys_dictionary`

One row per column. `internal_type` is the data type; `max_length` is the width
(and, for `decimal`/`float`, the *precision*).

**bash / curl:**

```bash
curl -sS -u "$SN_USER:$SN_PASS" \
  -H 'Accept: application/json' -H 'Content-Type: application/json' \
  -X POST \
  -d '{"name":"u_toronto_event","element":"u_title","column_label":"Title",
       "internal_type":"string","max_length":"500"}' \
  "https://$SN_INSTANCE/api/now/table/sys_dictionary"
```

**cmd.exe** — quoting is the pain, and `curl.exe` ships with Windows 10+:

```bat
curl.exe -sS -u "%SN_USER%:%SN_PASS%" ^
  -H "Accept: application/json" -H "Content-Type: application/json" ^
  -X POST ^
  -d "{\"name\":\"u_toronto_event\",\"element\":\"u_title\",\"column_label\":\"Title\",\"internal_type\":\"string\",\"max_length\":\"500\"}" ^
  "https://%SN_INSTANCE%/api/now/table/sys_dictionary"
```

> On `cmd.exe` the escaping is genuinely awful. Prefer PowerShell, or better,
> use `create_table.py`, which builds the JSON in Python and has no quoting
> problem at all.

### 2.3 Insert data — `POST /api/now/table/<table>`

```bash
curl -sS -u "$SN_USER:$SN_PASS" \
  -H 'Accept: application/json' -H 'Content-Type: application/json' \
  -X POST \
  -d '{"u_event_id":"abc123","u_title":"Some event","u_lat":"43.667712","u_is_free":"true"}' \
  "https://$SN_INSTANCE/api/now/table/u_toronto_event"
```

```powershell
$row = @{ u_event_id = "abc123"; u_title = "Some event"; u_lat = "43.667712"; u_is_free = "true" }
Invoke-RestMethod -Method Post -Headers $header -ContentType "application/json" `
  -Uri "https://$env:SN_INSTANCE/api/now/table/u_toronto_event" -Body ($row | ConvertTo-Json)
```

### 2.4 Read, update, delete

| Operation | Verb | Path |
|---|---|---|
| read one | `GET` | `/api/now/table/u_toronto_event/<sys_id>` |
| read many | `GET` | `/api/now/table/u_toronto_event?sysparm_query=...&sysparm_limit=1000` |
| count | `GET` | same, then read the `X-Total-Count` response header |
| update | `PATCH` | `/api/now/table/u_toronto_event/<sys_id>` |
| delete | `DELETE` | `/api/now/table/u_toronto_event/<sys_id>` |

Booleans are sent as the **strings** `"true"` / `"false"`. Date/times are
`YYYY-MM-DD HH:MM:SS`.

---

## 3. Alternatives to curl

Nothing about this integration requires curl. Pick whatever you can schedule and
debug.

| Tool | Use when | Notes |
|---|---|---|
| **`sn_api.py` / the repo scripts** | anything scheduled | builds JSON safely, no shell quoting, friendly errors |
| **curl** | quick probes, health checks | available on Linux, macOS, Windows 10+ |
| **PowerShell `Invoke-RestMethod`** | Windows servers, Task Scheduler | returns objects, not strings |
| **Python `requests`** | larger jobs | `sn_api.py` avoids the dependency deliberately |
| **Postman / Insomnia** | exploring an API interactively | good for capturing a working call, bad for scheduling |
| **VS Code REST Client** (`.http` files) | keeping requests in the repo | version-controllable |
| **ServiceNow REST API Explorer** | discovering an endpoint | in-product, at `/api_explorer.do`; also generates OAuth tokens |
| **ServiceNow Scripted REST / Scheduled Job** | the pull direction only | runs *inside* the instance, so no auth needed but no outbound to your LAN |

### Choosing between them

* **Scheduled job** → Python script (best error handling) or PowerShell (if the
  host is Windows and that's what ops knows).
* **Interactive debugging** → Postman or REST API Explorer.
* **"Is it up?" from a terminal** → curl, one line.

---

## 4. Setting the environment variables per shell

The scripts never read a password from anywhere else, so getting these three
variables into the environment is the whole setup step.

**bash / zsh (Linux, macOS, WSL, Git Bash)**

```bash
export SN_INSTANCE=yourinstance.service-now.com
export SN_USER=integration.user
export SN_PASS='...'          # single quotes: protects $ ( ) # etc.
```

**PowerShell**

```powershell
$env:SN_INSTANCE = "yourinstance.service-now.com"
$env:SN_USER     = "integration.user"
$env:SN_PASS     = "..."
```

Persist for the current user only:

```powershell
[Environment]::SetEnvironmentVariable("SN_PASS", "...", "User")
```

**cmd.exe**

```bat
set SN_INSTANCE=yourinstance.service-now.com
set SN_USER=integration.user
set SN_PASS=...
```

**A file instead (recommended for servers)**

```bash
# .env  (gitignored)  or  /etc/sn-sync.env  (mode 0600)
SN_INSTANCE=yourinstance.service-now.com
SN_USER=integration.user
SN_PASS=...
```

`sn_api.py` loads `.env` automatically if present. systemd loads
`/etc/sn-sync.env` via `EnvironmentFile=`.

> **Why not put credentials in the URL?** `https://user:pass@host/...` leaks into
> shell history, `ps` output, proxy logs and error messages. The header form
> leaks into none of those. This is the single most common shortcut that turns
> into an incident.

---

## 5. Authentication failures and what they actually mean

| Response | Meaning | Fix |
|---|---|---|
| `401 Unauthorized` | wrong user/password, **or** the account has no API access | test the same credentials in a browser; then check roles |
| `403 Forbidden` with `ACL Exception … security constraints` | write to a protected table without an elevated session | UI only — avatar → Elevate roles → `security_admin` |
| `403` on a normal table | ACL denies that role | check `sys_security_acl` for the table and operation |
| `400 Invalid table <name>` | the table does not exist | that's the *expected* way to detect absence |
| `405 Method not Supported` | the API resource doesn't exist at that path | e.g. the Import Set API is absent on this instance |
| Works in browser, `401` from script | MFA/SSO on the account, or the user is federated | use a local integration user with web-service access |
| Worked yesterday, `401` today | password rotated, or the PDI expired | see `REBUILD.md` |

Two role notes that cost time if unknown:

* Creating tables/columns effectively needs `admin` on a stock instance.
* ACL writes need `security_admin` **and** an elevated browser session — REST
  can never do it, no matter which role you hold.

---

## 6. Minimal end-to-end check

Three commands tell you whether everything above is wired correctly:

```bash
# 1. credentials + instance reachable (no writes)
python sn_api.py count sys_user

# 2. table exists and is readable
python sn_api.py probe u_toronto_event

# 3. columns exist and a value survives a round trip
python verify_table.py u_toronto_event
```

If 1 fails → auth/env. If 2 fails → table missing (run `create_table.py`). If 3
fails → schema problem (read the DRIFT lines; see `FINDINGS.md`).
