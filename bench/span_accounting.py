"""LLM spend in ClickHouse spans, with one accounting owner per model call.

Every model call Vigil records is a ``gen_ai.*`` span written by the SDK wrapper (or
``vigil.llm_call``): the *owner* of that call's tokens and cost. anthropic>=1.17 can add a
span of its own per API call (``anthropic.messages.create``, instrumentation scope
``com.anthropic.sdk.python``) with the same ``gen_ai.usage.*`` attributes; the ingest consumer
prices any span that carries them. Under ``vigil.wrap`` before the SDK fix, that native span is
a CHILD of the Vigil span, so a plain ``sum(...)`` over the run's spans counted each call
twice — the HotpotQA pilot run cafae810 (docs/HOTPOTQA_VALIDATION.md).

``reconcile`` returns canonical totals from the stored rows without rewriting anything:

- Rows are first collapsed per span identity ``(trace_id, span_id)``, keeping the newest
  ``ingested_at``: ``spans`` is a ReplacingMergeTree keyed on it, which removes replayed
  copies only when parts merge, so an un-merged replay would otherwise be summed twice.
- Only one shape is proven to be the same call recorded twice, and only it is excluded: a
  *shadow* is an ``anthropic.messages.create`` span whose direct parent is a
  ``gen_ai.chat*`` span of the same trace, that is the ONLY non-``gen_ai.*`` usage span under
  that parent (a unique one-to-one pair), with the same model, the same output tokens,
  compatible input/cache tokens and (without cache tokens) the same cost.
- Every other non-``gen_ai.*`` usage span stays in the canonical totals. Under a
  ``gen_ai.*`` parent it is *unresolved*, with a reason: ``unknown_instrumentation`` (another
  span name, or a parent that is not a chat span), ``several_children`` (no unique pair:
  which one, if any, is the copy?), ``model_mismatch``, ``usage_mismatch`` or
  ``cost_mismatch``. A nested span that differs may be a genuine distinct call, so counting it
  can only over-count, never hide spend. With no ``gen_ai.*`` parent it is *unowned* (e.g. a
  native span that is a sibling of a Vigil span): structure cannot tell it from a separate
  call, so it is kept too.
- Nothing is excluded on name or equal values alone: distinct calls with identical usage are
  distinct owner spans and all count.
- Cache hits (``cache_hit = 1``, the engine's dev cache) cost nothing; they are reported but
  are not spend.

``single_owner`` is the property a run must have: no shadow, unresolved or unowned span.
Historical runs recorded before the fix fail it; their stored rows and any report summing them
stay inflated unless read through this reconciliation, and an unresolved or unowned span means
even the canonical total may still hold a duplicate that structure could not prove.

    engine/.venv/bin/python bench/span_accounting.py RUN_ID [--version V ...] [--postgres]
        [--out FILE]

Read-only: SELECTs through ``engine.api.clickhouse.ClickHouseClient`` (and Postgres with
``--postgres``); prints JSON, and writes it to ``--out`` only if that file does not exist.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))

OWNER_PREFIX = "gen_ai."
OWNER_CHAT_PREFIX = "gen_ai.chat"  # the vigil.wrap span name
NATIVE_SPAN = "anthropic.messages.create"  # anthropic>=1.17, scope com.anthropic.sdk.python
EXAMPLES = 5

LLM_SPANS_SQL = (
    "SELECT trace_id, span_id, parent_span_id, span_name, role, cache_hit, "
    "gen_ai_request_model AS model, gen_ai_response_model AS response_model, "
    "gen_ai_usage_input_tokens AS input_tokens, gen_ai_usage_output_tokens AS output_tokens, "
    "gen_ai_usage_cache_creation_input_tokens AS cache_creation_input_tokens, "
    "gen_ai_usage_cache_read_input_tokens AS cache_read_input_tokens, cost_usd, "
    "toString(ingested_at) AS ingested_at "
    "FROM spans WHERE eval_run_id = {run:String} AND agent_version = {ver:String} "
    "AND (gen_ai_request_model != '' OR gen_ai_response_model != '' "
    "OR gen_ai_usage_input_tokens > 0 OR gen_ai_usage_output_tokens > 0 OR cost_usd > 0)"
)

_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                 "cache_read_input_tokens")


def _totals(rows: Iterable[dict]) -> dict:
    rows = list(rows)
    out = {f: sum(int(r.get(f) or 0) for r in rows) for f in _TOKEN_FIELDS}
    out["cost_usd"] = math.fsum(float(r.get("cost_usd") or 0) for r in rows)
    out["spans"] = len(rows)
    return out


def _key(r: dict) -> tuple[str, str]:
    return (str(r["trace_id"]), str(r["span_id"]))


def _ref(r: dict) -> str:
    return f"{r['trace_id']}/{r['span_id']}"


def dedupe(rows: Iterable[dict]) -> tuple[list[dict], int]:
    """One row per span identity (newest ``ingested_at``), and how many stored rows repeated one."""
    best: dict[tuple[str, str], dict] = {}
    n = 0
    for r in rows:
        n += 1
        k = _key(r)
        if k not in best or str(r.get("ingested_at") or "") > str(best[k].get("ingested_at") or ""):
            best[k] = r
    return list(best.values()), n - len(best)


def _int(r: dict, field: str) -> int:
    return int(r.get(field) or 0)


def _shadow_mismatch(native: dict, owner: dict) -> str | None:
    """Why ``native`` is not provably ``owner``'s call recorded again, or None if it is.

    Same request model (and response model where both have one), same output tokens, and
    compatible input/cache tokens: the native span folds prompt-cache tokens into its
    ``gen_ai.usage.input_tokens`` (its cache attributes are not promoted to columns), while
    the Vigil span keeps them apart; or both carry the same cache columns and input. Without
    cache tokens the consumer prices the same tokens of the same model, so the cost must agree.
    """
    req = (str(native.get("model") or ""), str(owner.get("model") or ""))
    resp = (str(native.get("response_model") or ""), str(owner.get("response_model") or ""))
    if not req[0] or req[0] != req[1] or (all(resp) and resp[0] != resp[1]):
        return "model_mismatch"
    if _int(native, "output_tokens") != _int(owner, "output_tokens"):
        return "usage_mismatch"
    o_in = _int(owner, "input_tokens")
    o_cache = (_int(owner, "cache_creation_input_tokens"), _int(owner, "cache_read_input_tokens"))
    n_cache = (_int(native, "cache_creation_input_tokens"), _int(native, "cache_read_input_tokens"))
    n_in = _int(native, "input_tokens")
    folded = n_cache == (0, 0) and n_in == o_in + sum(o_cache)
    separate = n_cache == o_cache and n_in == o_in
    if not (folded or separate):
        return "usage_mismatch"
    if o_cache == (0, 0) and not math.isclose(float(native.get("cost_usd") or 0),
                                              float(owner.get("cost_usd") or 0),
                                              rel_tol=1e-9, abs_tol=1e-12):
        return "cost_mismatch"
    return None


def reconcile(rows: Iterable[dict]) -> dict:
    """Canonical LLM spend of a set of stored span rows (see the module doc)."""
    rows = list(rows)
    spans, duplicate_rows = dedupe(rows)
    owners = {_key(r): r for r in spans if str(r["span_name"]).startswith(OWNER_PREFIX)}
    unowned: list[dict] = []
    children: dict[tuple[str, str], list[dict]] = {}
    for r in spans:
        if _key(r) in owners:
            continue
        parent = (str(r["trace_id"]), str(r.get("parent_span_id") or ""))
        if parent in owners:
            children.setdefault(parent, []).append(r)
        else:
            unowned.append(r)
    shadows: list[dict] = []
    unresolved: list[tuple[dict, str]] = []
    for parent, kids in children.items():
        owner = owners[parent]
        for r in kids:
            if len(kids) > 1:  # not a unique one-to-one pair: which one (if any) is the copy?
                reason = "several_children"
            elif r["span_name"] != NATIVE_SPAN or not str(owner["span_name"]).startswith(
                    OWNER_CHAT_PREFIX):
                reason = "unknown_instrumentation"
            else:
                reason = _shadow_mismatch(r, owner)
            if reason is None:
                shadows.append(r)
            else:
                unresolved.append((r, reason))
    shadow_keys = {_key(r) for r in shadows}
    counted = [r for r in spans if _key(r) not in shadow_keys]
    spend = [r for r in counted if not _int(r, "cache_hit")]
    reasons: dict[str, int] = {}
    for _, reason in unresolved:
        reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "stored_rows": len(rows),
        "duplicate_identity_rows": duplicate_rows,
        "spans": len(spans),
        "owner_spans": len(owners),
        "shadow_spans": len(shadows),
        "unresolved_spans": len(unresolved),
        "unresolved_reasons": dict(sorted(reasons.items())),
        "unowned_spans": len(unowned),
        "single_owner": not shadows and not unresolved and not unowned,
        "roles": sorted({str(r.get("role") or "") for r in counted} - {""}),
        "models": sorted({str(r.get("model") or "") for r in counted} - {""}),
        # What a plain sum over the stored rows reports (the pre-fix triage query).
        "as_stored": _totals(rows),
        "shadow": _totals(shadows),
        "unresolved": _totals(r for r, _ in unresolved),
        "cache_hits": _totals(r for r in counted if _int(r, "cache_hit")),
        # Everything but proven shadows, one row per identity, cache hits excluded.
        "canonical": _totals(spend),
        "examples": {"shadow": [_ref(r) for r in shadows[:EXAMPLES]],
                     "unresolved": [f"{_ref(r)} ({reason})" for r, reason in
                                    unresolved[:EXAMPLES]],
                     "unowned": [_ref(r) for r in unowned[:EXAMPLES]]},
    }


def fetch_llm_spans(ch, run_id: str, version: str) -> list[dict]:
    return ch.query(LLM_SPANS_SQL, {"run": run_id, "ver": version})


def _versions(ch, run_id: str) -> list[str]:
    return [r["agent_version"] for r in ch.query(
        "SELECT DISTINCT agent_version FROM spans WHERE eval_run_id = {run:String} "
        "ORDER BY agent_version", {"run": run_id})]


def _postgres_ok_totals(run_id: str) -> dict[str, dict]:
    """Per version: the OK units' tokens/cost, attempts, and statuses from Postgres."""
    import psycopg
    from engine.config import pg_dsn

    with psycopg.connect(pg_dsn()) as conn:
        cur = conn.execute(
            "SELECT agent_version, status, count(*), coalesce(sum(input_tokens), 0), "
            "coalesce(sum(output_tokens), 0), coalesce(sum(cost_usd), 0), "
            "coalesce(max(attempts), 0) FROM eval_case_results WHERE run_id = %s "
            "GROUP BY agent_version, status", (run_id,))
        out: dict[str, dict] = {}
        for ver, status, n, i, o, c, att in cur:
            v = out.setdefault(ver, {"statuses": {}, "ok": None, "max_attempts": 0})
            v["statuses"][status] = n
            v["max_attempts"] = max(v["max_attempts"], int(att))
            if status == "ok":
                v["ok"] = {"units": n, "input_tokens": int(i), "output_tokens": int(o),
                           "cost_usd": float(c)}
        return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser("span_accounting", description=__doc__.split("\n\n")[0])
    ap.add_argument("run_id")
    ap.add_argument("--version", action="append", help="agent version(s); default: all")
    ap.add_argument("--postgres", action="store_true",
                    help="also read the OK units' totals from Postgres for comparison")
    ap.add_argument("--out", help="write the JSON here (refused if the file exists)")
    args = ap.parse_args(argv)
    out = Path(args.out) if args.out else None
    if out is not None and out.exists():
        raise SystemExit(f"{out} exists; refusing to overwrite evidence")

    from engine.api.clickhouse import ClickHouseClient

    ch = ClickHouseClient()
    report = {"run_id": args.run_id, "versions": {}}
    for ver in args.version or _versions(ch, args.run_id):
        report["versions"][ver] = reconcile(fetch_llm_spans(ch, args.run_id, ver))
    if args.postgres:
        report["postgres"] = _postgres_ok_totals(args.run_id)
    text = json.dumps(report, indent=2, sort_keys=True)
    if out is not None:
        with out.open("x") as f:
            f.write(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
