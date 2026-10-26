"""Field-by-field diff of two ``version_manifests`` rows (the "what changed?" step).

Only the expected configuration fields are returned: model, git_sha, params, prompts, tools,
code_ref. Manifests are not supposed to hold secrets (they are hashed agent config), but an
agent could put one in ``params``/``code_ref``/``tools`` by mistake, so values under keys that
look like credentials are replaced by ``[redacted]`` before anything leaves the API.

Redaction limits (reported in every response): it is key-name based only. Free text — prompt
bodies, tool descriptions, string values under innocuous keys — is not scanned, so a secret
pasted into a prompt would be returned as-is. Long strings are truncated to keep responses
bounded.
"""

from __future__ import annotations

import re
from typing import Any

FIELDS = ("model", "git_sha", "params", "prompts", "tools", "code_ref")
MAX_STRING_CHARS = 20_000
REDACTED = "[redacted]"
REDACTION_NOTE = (
    "values under credential-like keys (api_key, secret, password, authorization, "
    "access/auth/refresh/session token, private_key, credential, bearer) are redacted; "
    "free text such as prompt bodies and tool descriptions is NOT scanned"
)
_SECRET_KEY = re.compile(
    r"(api[_-]?key|secret|passw(or)?d|authorization|bearer|credential|private[_-]?key|"
    r"(^|[_-])(auth|access|refresh|session|id)[_-]?token$|^token$)",
    re.IGNORECASE,
)


def _sanitize(value: Any, path: str, redacted: list[str], truncated: list[str]) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            p = f"{path}.{k}"
            if _SECRET_KEY.search(str(k)):
                out[k] = REDACTED
                redacted.append(p)
            else:
                out[k] = _sanitize(v, p, redacted, truncated)
        return out
    if isinstance(value, list):
        return [_sanitize(v, f"{path}[{i}]", redacted, truncated) for i, v in enumerate(value)]
    if isinstance(value, str) and len(value) > MAX_STRING_CHARS:
        truncated.append(path)
        return value[:MAX_STRING_CHARS]
    return value


def sanitize_manifest(row: dict | None) -> tuple[dict | None, list[str], list[str]]:
    """The expected fields of a manifest row, redacted + bounded. Returns
    (manifest | None, redacted_paths, truncated_paths)."""
    if row is None:
        return None, [], []
    redacted: list[str] = []
    truncated: list[str] = []
    out = {f: _sanitize(row.get(f), f, redacted, truncated) for f in FIELDS}
    created = row.get("created_at")
    out["created_at"] = created.isoformat() if hasattr(created, "isoformat") else created
    return out, redacted, truncated


def _dict_diff(a: Any, b: Any) -> dict:
    if not isinstance(a, dict) or not isinstance(b, dict):
        return {"changed": a != b}
    keys = set(a) | set(b)
    return {
        "changed": a != b,
        "added": sorted(k for k in keys if k not in a),
        "removed": sorted(k for k in keys if k not in b),
        "modified": sorted(k for k in keys if k in a and k in b and a[k] != b[k]),
    }


def diff_manifests(base: dict | None, cand: dict | None) -> dict:
    """Per-field change flags between two manifest rows. Pass the RAW rows: the flags are
    computed before redaction/truncation so a change inside a redacted or truncated value is
    still reported (only key names are listed, never values). Dict fields (params, prompts,
    code_ref) also list added/removed/modified keys. A missing manifest makes every field
    'unknown' (``changed: null``) rather than 'changed'."""
    if base is None or cand is None:
        return {f: {"changed": None} for f in FIELDS}
    out = {}
    for f in FIELDS:
        if f in ("params", "prompts", "code_ref"):
            out[f] = _dict_diff(base.get(f), cand.get(f))
        else:
            out[f] = {"changed": base.get(f) != cand.get(f)}
    return out
