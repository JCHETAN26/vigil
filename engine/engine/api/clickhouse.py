"""Thin read-only ClickHouse HTTP client with server-side parameter binding.

Request values are passed as ClickHouse query parameters (`param_<name>` + `{name:Type}`
placeholders in the SQL), so a value can never be spliced into the query text — the injection
boundary is the HTTP layer, not string formatting. The client also forbids anything but a
single read statement as defense in depth.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request

_FORBIDDEN = (";", " insert ", " alter ", " drop ", " delete ", " create ", " truncate ")


class ClickHouseError(RuntimeError):
    pass


class ClickHouseClient:
    """Minimal HTTP client. `query` returns rows as a list of dicts (JSONEachRow)."""

    def __init__(self, host=None, port=None, database=None, user=None, password=None, timeout=15.0):
        self.host = host or os.getenv("CLICKHOUSE_HOST", "127.0.0.1")
        self.port = int(port or os.getenv("CLICKHOUSE_HTTP_PORT", "8123"))
        self.database = database or os.getenv("CLICKHOUSE_DB", "vigil")
        # Prefer the read-only user for the dashboard API; fall back to the admin user only when
        # no RO user is configured (see deploy/create_readonly_users.sh).
        self.user = user or os.getenv("CLICKHOUSE_RO_USER") or os.getenv("CLICKHOUSE_USER", "vigil")
        if password is not None:
            self.password = password
        elif os.getenv("CLICKHOUSE_RO_USER"):
            self.password = os.getenv("CLICKHOUSE_RO_PASSWORD", "")
        else:
            self.password = os.getenv("CLICKHOUSE_PASSWORD", "")
        self.timeout = timeout

    def query(self, sql: str, params: dict[str, object] | None = None) -> list[dict]:
        low = sql.lower()
        if any(tok in low for tok in _FORBIDDEN):
            raise ClickHouseError("only single read statements are allowed")
        params = params or {}
        # ClickHouse binds {name:Type} placeholders from param_<name> query args — the values
        # are never concatenated into the SQL text.
        qs = {"database": self.database, **{f"param_{k}": str(v) for k, v in params.items()}}
        url = f"http://{self.host}:{self.port}/?" + urllib.parse.urlencode(qs)
        body = (sql + "\nFORMAT JSONEachRow").encode()
        req = urllib.request.Request(url, data=body, method="POST")
        token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                text = resp.read().decode()
        except urllib.error.HTTPError as exc:  # 4xx/5xx from ClickHouse
            raise ClickHouseError(exc.read().decode()[:400]) from exc
        return [json.loads(line) for line in text.splitlines() if line.strip()]
