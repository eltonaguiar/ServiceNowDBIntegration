#!/usr/bin/env python3
"""
sn_api.py -- minimal ServiceNow Table API client (Python standard library only).

No external dependencies. No credentials in source: everything comes from
environment variables, optionally seeded from a gitignored `.env` file.

Why this exists
---------------
Creating ServiceNow tables programmatically is just inserts into two metadata
tables:

    sys_db_object   -> one row per TABLE
    sys_dictionary  -> one row per COLUMN

Reading and writing data is a third:

    /api/now/table/<table>   -> the actual records

That's the whole model. Everything in this module is a thin wrapper over those
three surfaces.

Usage
-----
    from sn_api import ServiceNow

    sn = ServiceNow()                       # reads SN_INSTANCE / SN_USER / SN_PASS
    sn.create_table("u_demo", "Demo")
    sn.create_column("u_demo", "u_name", "Name", "string", max_length=100)

    sn.insert("u_demo", {"u_name": "hello"})
    sn.count("u_demo")                      # -> 1

CLI:
    python sn_api.py count sys_user
    python sn_api.py columns u_demo
    python sn_api.py probe sys_user u_demo does_not_exist
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ENV_FILE = Path(__file__).resolve().parent / ".env"

# Field types verified working against a real instance. See README for the
# decimal-precision trap.
KNOWN_TYPES = {
    "string",
    "boolean",
    "integer",
    "decimal",
    "float",
    "glide_date_time",
    "url",
    "GUID",
    "email",
    "phone_number",
    "long",
    "longint",
    "reference",
    "choice",
}


class SNError(RuntimeError):
    """Raised when the instance returns a non-2xx response."""

    def __init__(self, status: int, message: str, payload: Optional[dict] = None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.payload = payload or {}


def load_env_file(path: Path = ENV_FILE) -> None:
    """Seed os.environ from a simple KEY=VALUE file. Existing vars win."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class ServiceNow:
    """Thin, dependency-free client for the ServiceNow Table API."""

    def __init__(
        self,
        instance: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        timeout: int = 60,
    ):
        load_env_file()

        raw_instance = (instance or os.environ.get("SN_INSTANCE", "")).strip()
        if not raw_instance:
            raise SystemExit(
                "SN_INSTANCE is not set.\n"
                "  cp .env.example .env   and fill it in, or export SN_INSTANCE=..."
            )

        # Tolerate 'https://host' or 'host/'
        raw_instance = raw_instance.replace("https://", "").replace("http://", "")
        self.instance = raw_instance.rstrip("/")

        base_override = os.environ.get("SN_BASE_URL", "").strip()
        self.base_url = (
            base_override.rstrip("/")
            if base_override
            else f"https://{self.instance}/api/now/table"
        )

        self.user = user or os.environ.get("SN_USER", "")
        self.password = password or os.environ.get("SN_PASS", "")
        if not self.user or not self.password:
            raise SystemExit(
                "SN_USER / SN_PASS are not set.\n"
                "  cp .env.example .env   and fill it in, or export them."
            )

        self.timeout = timeout
        token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        self._auth_header = f"Basic {token}"

    # ------------------------------------------------------------------ core

    def _request(
        self,
        method: str,
        table: str,
        *,
        sys_id: Optional[str] = None,
        body: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> Tuple[int, Any, Dict[str, str]]:
        url = f"{self.base_url}/{table}"
        if sys_id:
            url += f"/{sys_id}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)

        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", self._auth_header)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                headers = {k.lower(): v for k, v in resp.headers.items()}
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
            status = exc.code
            try:
                payload = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                payload = {"error": {"message": raw.decode("utf-8", "replace")[:400]}}
            message = (
                payload.get("error", {}).get("detail")
                or payload.get("error", {}).get("message")
                or "request failed"
            )
            raise SNError(status, message, payload) from None

        if not raw:
            return status, None, headers
        try:
            return status, json.loads(raw), headers
        except json.JSONDecodeError:
            return status, raw.decode("utf-8", "replace"), headers

    # ------------------------------------------------------------- read path

    def query(
        self,
        table: str,
        encoded_query: Optional[str] = None,
        fields: Optional[Iterable[str]] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order_by: Optional[str] = None,
        display_value: Optional[str] = None,
    ) -> List[dict]:
        """Read rows. `encoded_query` uses ServiceNow syntax: a^b^c, LIKE, etc."""
        if order_by:
            encoded_query = (
                f"{encoded_query}^ORDERBY{order_by}" if encoded_query else f"ORDERBY{order_by}"
            )
        params = {
            "sysparm_query": encoded_query,
            "sysparm_fields": ",".join(fields) if fields else None,
            "sysparm_limit": limit,
            "sysparm_offset": offset,
            "sysparm_display_value": display_value,
        }
        _, payload, _ = self._request("GET", table, params=params)
        return payload.get("result", []) if isinstance(payload, dict) else []

    def get(self, table: str, sys_id: str, fields: Optional[Iterable[str]] = None) -> dict:
        params = {"sysparm_fields": ",".join(fields) if fields else None}
        _, payload, _ = self._request("GET", table, sys_id=sys_id, params=params)
        return payload.get("result", {})

    def count(self, table: str, encoded_query: Optional[str] = None) -> Optional[int]:
        """
        Row count via the X-Total-Count header -- costs one record, not N.

        Returns None if the instance did not send the header.
        """
        _, _, headers = self._request(
            "GET",
            table,
            params={"sysparm_query": encoded_query, "sysparm_fields": "sys_id", "sysparm_limit": 1},
        )
        raw = headers.get("x-total-count")
        return int(raw) if raw and raw.isdigit() else None

    def table_exists(self, table: str) -> bool:
        """
        True if the table is queryable.

        The Table API answers HTTP 400 for a table that does not exist and
        200 for one that does -- absence is diagnosed by the error, not by
        an empty result set.
        """
        try:
            self._request("GET", table, params={"sysparm_fields": "sys_id", "sysparm_limit": 1})
            return True
        except SNError as exc:
            if exc.status == 400:
                return False
            raise

    # ------------------------------------------------------------ write path

    def insert(self, table: str, record: dict) -> dict:
        _, payload, _ = self._request("POST", table, body=record)
        return payload.get("result", {})

    def update(self, table: str, sys_id: str, record: dict) -> dict:
        _, payload, _ = self._request("PATCH", table, sys_id=sys_id, body=record)
        return payload.get("result", {})

    def delete(self, table: str, sys_id: str) -> None:
        self._request("DELETE", table, sys_id=sys_id)

    # ------------------------------------------------------ schema (metadata)

    def create_table(
        self,
        name: str,
        label: str,
        scope: str = "global",
        extendable: bool = False,
    ) -> dict:
        """
        Create a table by inserting into sys_db_object.

        The instance materialises the physical table inside the same
        transaction, so the table is queryable immediately afterwards.
        `name` is permanent -- renaming later is painful.
        """
        if not name.startswith("u_") and scope == "global":
            print(f"  ! note: global custom tables conventionally start with 'u_' (got {name!r})")
        if self.table_exists(name):
            print(f"  = table {name} already exists, skipping create")
            return {"name": name, "already_existed": True}

        body = {
            "name": name,
            "label": label,
            "sys_scope": scope,
            "is_extendable": "true" if extendable else "false",
        }
        record = self.insert("sys_db_object", body)
        print(f"  + created table {name} (sys_id={record.get('sys_id')})")
        return record

    def columns(self, table: str) -> List[dict]:
        """Custom columns for a table, ordered by element name."""
        rows = self.query(
            "sys_dictionary",
            encoded_query=f"name={table}^NelementISNOTEMPTY^ORDERBYelement",
            fields=["sys_id", "element", "column_label", "internal_type", "max_length"],
            limit=500,
        )
        out = []
        for row in rows:
            itype = row.get("internal_type")
            if isinstance(itype, dict):
                itype = itype.get("value")
            out.append(
                {
                    "sys_id": row.get("sys_id"),
                    "element": row.get("element"),
                    "label": row.get("column_label"),
                    "type": itype,
                    "max_length": row.get("max_length"),
                }
            )
        return out

    def create_column(
        self,
        table: str,
        element: str,
        label: str,
        type: str,
        max_length: Optional[int] = None,
    ) -> dict:
        """
        Create a column by inserting into sys_dictionary.

        `max_length` matters more than it looks: for `decimal` and `float`
        it governs stored precision. A decimal with max_length=15 keeps only
        two decimal places. See README "The precision trap".
        """
        if type not in KNOWN_TYPES:
            print(f"  ! warning: {type!r} not in the verified type list {sorted(KNOWN_TYPES)}")

        existing = {c["element"] for c in self.columns(table)}
        if element in existing:
            print(f"  = column {element} already exists, skipping")
            return {"element": element, "already_existed": True}

        body: Dict[str, Any] = {
            "name": table,
            "element": element,
            "column_label": label,
            "internal_type": type,
        }
        if max_length:
            body["max_length"] = str(max_length)

        record = self.insert("sys_dictionary", body)
        print(f"  + {element:<24} {type:<16} {max_length or '-':<6} {label}")
        return record


# --------------------------------------------------------------------- CLI


def _cli(argv: List[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2

    sn = ServiceNow()
    cmd, args = argv[1], argv[2:]

    if cmd == "count":
        for table in args:
            try:
                print(f"{table:<40} {sn.count(table)}")
            except SNError as exc:
                print(f"{table:<40} ERROR {exc}")

    elif cmd == "probe":
        for table in args:
            print(f"{table:<40} {'exists' if sn.table_exists(table) else 'MISSING'}")

    elif cmd == "columns":
        for table in args:
            print(f"# {table}")
            for col in sn.columns(table):
                print(f"  {col['element']:<24} {str(col['type']):<16} {str(col['max_length'] or '-'):<6} {col['label']}")

    else:
        print(f"unknown command {cmd!r}")
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
