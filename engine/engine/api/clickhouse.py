"""Thin read-only ClickHouse HTTP client with server-side parameter binding.

Request values are passed as ClickHouse query parameters (`param_<name>` + `{name:Type}`
placeholders in the SQL), so a value can never be spliced into the query text — the injection
boundary is the HTTP layer, not string formatting. The client also forbids anything but a
single read statement as defense in depth (the real boundary is the `readonly=1` ClickHouse
user, deploy/create_readonly_users.sh).

The read guard is deliberately conservative, not a SQL parser: it blanks out string literals
and quoted identifiers, then requires the statement to start with SELECT or WITH and to contain
no statement separator, no comment, and no write/DDL keyword as a whole word anywhere. A legal
read that trips it (e.g. a column literally named `update`) must quote the identifier.
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

# Whole-word keywords that never belong in a dashboard read. Matched case-insensitively against
# the SQL with literals/quoted identifiers removed, so any whitespace (tab, newline) around them
# and their position (including the very start) are irrelevant.
_FORBIDDEN_WORDS = (
    "insert", "alter", "drop", "delete", "create", "truncate", "rename", "attach", "detach",
    "optimize", "grant", "revoke", "kill", "system", "update", "exchange", "undrop", "set",
    "use", "outfile", "backup", "restore", "move",
)
_FORBIDDEN_RE = re.compile(r"\b(" + "|".join(_FORBIDDEN_WORDS) + r")\b", re.IGNORECASE)
_LEADING_RE = re.compile(r"\s*(select|with)\b", re.IGNORECASE)
_PARAM_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_QUOTES = ("'", '"', "`")


class ClickHouseError(RuntimeError):
    pass


def _strip_quoted(sql: str) -> str:
    """Replace the contents of '...', "..." and `...` (with backslash and doubled-quote
    escapes) by a space, so keyword/separator checks only see SQL structure. An unterminated
    quote is refused rather than guessed at."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch not in _QUOTES:
            out.append(ch)
            i += 1
            continue
        j = i + 1
        while j < n:
            if sql[j] == "\\":
                j += 2
                continue
            if sql[j] == ch:
                if j + 1 < n and sql[j + 1] == ch:  # doubled quote escape
                    j += 2
                    continue
                break
            j += 1
        if j >= n:
            raise ClickHouseError("unterminated quoted literal")
        out.append(" ")
        i = j + 1
    return "".join(out)


def check_read_only(sql: str) -> None:
    """Raise ClickHouseError unless ``sql`` is a single SELECT/WITH statement (see module doc)."""
    bare = _strip_quoted(sql)
    if not _LEADING_RE.match(bare):
        raise ClickHouseError("only single read statements are allowed (SELECT/WITH)")
    if ";" in bare:
        raise ClickHouseError("only single read statements are allowed (no ';')")
    if "--" in bare or "/*" in bare or "#" in bare:
        raise ClickHouseError("only single read statements are allowed (no comments)")
    m = _FORBIDDEN_RE.search(bare)
    if m:
        raise ClickHouseError(f"only single read statements are allowed ({m.group(1).upper()})")


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
        check_read_only(sql)  # before any network I/O
        params = params or {}
        for k in params:
            if not _PARAM_NAME_RE.match(k):
                raise ClickHouseError(f"invalid parameter name {k!r}")
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
            try:
                detail = exc.read().decode(errors="replace")[:400]
            except Exception:  # noqa: BLE001 — the body is best-effort context only
                detail = ""
            raise ClickHouseError(f"HTTP {exc.code}: {detail}".rstrip(": ")) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:  # refused / DNS / timeout
            reason = getattr(exc, "reason", exc)
            raise ClickHouseError(f"clickhouse unreachable: {reason}") from exc
        try:
            return [json.loads(line) for line in text.splitlines() if line.strip()]
        except ValueError as exc:
            raise ClickHouseError("malformed JSONEachRow response") from exc
