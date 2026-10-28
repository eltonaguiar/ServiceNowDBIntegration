#!/usr/bin/env python3
"""
create_acl.py -- attempt to create table ACLs over REST, and explain the
                 platform limit when it fails.

    python create_acl.py u_toronto_event --read-role itil --write-role admin

Not a bug in this script
------------------------
Writing to `sys_security_acl` requires an **elevated security_admin session**.
That elevation is a browser/UI concept -- the REST API has no equivalent, so
the request is refused even for a user who already holds the `security_admin`
role. Verified: `admin` held `security_admin`, the insert still returned

    HTTP 403  "ACL Exception Insert Failed due to security constraints"

So this script's job is mostly to save you the debugging detour: it detects the
refusal, tells you exactly why, and prints the three UI steps that work.

Everything else about a table can be created programmatically. ACLs are the
one exception.
"""

from __future__ import annotations

import argparse
import sys

from sn_api import SNError, ServiceNow

ELEVATION_HELP = """
ACLs cannot be created over REST. This is a platform restriction, not an error
in the request or the credentials.

Do this instead (about two minutes):

  1. Click your avatar (top right) -> "Elevate roles" -> tick `security_admin` -> OK.
     Elevation is per-session; it expires on logout or timeout.

  2. System Security -> Access Control (ACL) -> New

       Type         : record
       Name         : {table}
       Operation    : read
       Active       : true
       Requires role: {read_role}

  3. Repeat for the write ACL:

       Type         : record
       Name         : {table}
       Operation    : write
       Active       : true
       Requires role: {write_role}

Leave the script condition empty. An empty script plus a role requirement means
"allow if the user holds this role".

Why split read and write: the only writer should ever be your transform map or
import job. If you leave them merged, every ITIL user can edit imported data by
hand and your next sync will fight them.

Also worth knowing: a table with NO ACLs at all is not locked down -- ServiceNow
grants access when no ACL matches. So an un-ACL'd table is readable AND
writable by any authenticated user.
"""


def ensure_role(sn: ServiceNow, name: str) -> str | None:
    rows = sn.query("sys_user_role", encoded_query=f"name={name}", fields=["sys_id", "name"], limit=1)
    return rows[0]["sys_id"] if rows else None


def try_create(sn: ServiceNow, table: str, operation: str, role_name: str) -> tuple[bool, str]:
    existing = sn.query(
        "sys_security_acl",
        encoded_query=f"name={table}^operation={operation}",
        fields=["sys_id"],
        limit=1,
    )
    if existing:
        return True, "already exists"

    role_id = ensure_role(sn, role_name)
    if role_id is None:
        return False, f"role {role_name!r} not found on this instance"

    body = {
        "name": table,
        "operation": operation,
        "active": "true",
        "type": "record",
        "requires_role": role_id,
        "description": f"{operation} access to {table}",
    }
    try:
        record = sn.insert("sys_security_acl", body)
    except SNError as exc:
        if exc.status == 403 and "security constraints" in str(exc).lower():
            return False, "blocked: requires an elevated security_admin session (UI only)"
        return False, str(exc)

    return True, f"created sys_id={record.get('sys_id')}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create read/write ACLs, or explain why it fails.")
    parser.add_argument("table", help="table name, e.g. u_toronto_event")
    parser.add_argument("--read-role", default="itil", help="role required to read (default: itil)")
    parser.add_argument(
        "--write-role", default="admin", help="role required to write (default: admin)"
    )
    args = parser.parse_args(argv)

    sn = ServiceNow()

    print(f"table      : {args.table}")
    print(f"read role  : {args.read_role}")
    print(f"write role : {args.write_role}")
    print()

    if not sn.table_exists(args.table):
        print(f"ERROR: table {args.table} does not exist")
        return 1

    all_ok = True
    for operation, role_name in (("read", args.read_role), ("write", args.write_role)):
        ok, message = try_create(sn, args.table, operation, role_name)
        print(f"  {operation:<6} requires {role_name:<12} -> {message}")
        all_ok = all_ok and ok

    print()
    if all_ok:
        existing = sn.query(
            "sys_security_acl",
            encoded_query=f"name={args.table}",
            fields=["name", "operation", "active", "requires_role"],
            limit=20,
        )
        print("ACLs on this table:")
        for row in existing:
            role = row.get("requires_role")
            role = role.get("display_value") if isinstance(role, dict) else role
            print(f"  {row.get('name')}  {row.get('operation')}  active={row.get('active')}  role={role or '(any)'}")
        return 0

    print(ELEVATION_HELP.format(table=args.table, read_role=args.read_role, write_role=args.write_role))
    return 1


if __name__ == "__main__":
    sys.exit(main())
