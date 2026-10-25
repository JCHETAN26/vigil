"""Unit tests for bench/span_accounting.py: one accounting owner per model call, over stored
span rows. No ClickHouse: the rows are what ``LLM_SPANS_SQL`` returns."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import span_accounting as sa


def _span(trace, span, *, parent="", name="gen_ai.chat claude-haiku-4-5-20251001",
          tokens=(100, 10), cost=0.001, ingested="2026-10-25 07:30:00.000", **over):
    return {"trace_id": trace, "span_id": span, "parent_span_id": parent, "span_name": name,
            "role": "", "cache_hit": 0, "model": "claude-haiku-4-5-20251001",
            "input_tokens": tokens[0], "output_tokens": tokens[1],
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "cost_usd": cost,
            "ingested_at": ingested, **over}


def _native(trace, span, parent, **kw):
    return _span(trace, span, parent=parent, name="anthropic.messages.create", **kw)


def test_clean_run_is_its_plain_sum():
    rows = [_span("t1", "a"), _span("t1", "b", tokens=(50, 5), cost=0.0005)]
    r = sa.reconcile(rows)
    assert r["single_owner"] and r["shadow_spans"] == r["unowned_spans"] == 0
    assert r["canonical"] == r["as_stored"]
    assert r["canonical"]["input_tokens"] == 150 and r["canonical"]["spans"] == 2


def test_pilot_native_children_are_excluded_as_shadows():
    # The pilot's shape (all 121 pairs of run cafae810): the native SDK span is the only usage
    # child of the Vigil span, same model, tokens and cost.
    rows = [_span("t1", "a"), _native("t1", "na", "a"),
            _span("t2", "b", tokens=(7, 3), cost=0.0001),
            _native("t2", "nb", "b", tokens=(7, 3), cost=0.0001)]
    r = sa.reconcile(rows)
    assert not r["single_owner"]
    assert r["shadow_spans"] == 2 and r["unresolved_spans"] == r["unowned_spans"] == 0
    assert r["as_stored"]["input_tokens"] == 2 * r["canonical"]["input_tokens"] == 214
    assert math.isclose(r["canonical"]["cost_usd"], 0.0011)
    assert r["examples"]["shadow"] == ["t1/na", "t2/nb"]


def test_native_child_with_cache_tokens_folded_into_input_is_a_shadow():
    # The native span reports input + prompt-cache tokens as its input_tokens; its cost then
    # differs (priced as plain input), so cost is only compared without cache tokens.
    owner = _span("t1", "a", tokens=(40, 12), cache_creation_input_tokens=5300, cost=0.02)
    r = sa.reconcile([owner, _native("t1", "n", "a", tokens=(5340, 12), cost=0.0054)])
    assert r["shadow_spans"] == 1 and r["unresolved_spans"] == 0


def test_equal_valued_distinct_calls_all_count():
    # Three real calls with identical usage — siblings, and a gen_ai span nested in another
    # gen_ai span (an owner is never a shadow) — are three calls, never deduplicated by value.
    rows = [_span("t1", "a", parent="root"), _span("t1", "b", parent="root"),
            _span("t1", "c", parent="a")]
    r = sa.reconcile(rows)
    assert r["single_owner"] and r["canonical"]["spans"] == 3
    assert r["canonical"]["input_tokens"] == 300 and math.isclose(r["canonical"]["cost_usd"], 0.003)


def test_same_span_id_in_another_trace_is_another_span():
    r = sa.reconcile([_span("t1", "a"), _span("t2", "a")])
    assert r["duplicate_identity_rows"] == 0 and r["canonical"]["spans"] == 2


def test_unmerged_replay_rows_collapse_to_the_newest():
    # ReplacingMergeTree only removes the replayed copy when parts merge; until then a plain
    # sum sees it twice.
    rows = [_span("t1", "a", ingested="2026-10-25 07:30:00.000"),
            _span("t1", "a", ingested="2026-10-25 07:31:00.000", tokens=(100, 11))]
    r = sa.reconcile(rows)
    assert r["duplicate_identity_rows"] == 1 and r["spans"] == 1
    assert r["single_owner"]  # a replay is not a second owner
    assert r["canonical"]["output_tokens"] == 11 and r["as_stored"]["output_tokens"] == 21


def test_unowned_native_span_is_counted_and_flagged():
    # A native span that is NOT nested under a Vigil span may be a separate call: structure
    # cannot prove it a duplicate, so it is kept — and the run is not single-owner.
    rows = [_span("t1", "a", parent="root"), _native("t1", "n", "root")]
    r = sa.reconcile(rows)
    assert not r["single_owner"] and r["unowned_spans"] == 1 and r["shadow_spans"] == 0
    assert r["canonical"]["input_tokens"] == 200


def test_cache_hits_are_not_spend():
    rows = [_span("t1", "a"), _span("t1", "b", cache_hit=1, cost=0.0)]
    r = sa.reconcile(rows)
    assert r["canonical"]["spans"] == 1 and r["cache_hits"]["input_tokens"] == 100


def _unresolved(rows, reason):
    """The rows' reconciliation, asserting nothing was excluded and every nested non-gen_ai
    span stayed counted as unresolved for ``reason``."""
    r = sa.reconcile(rows)
    nested = [x for x in rows if not x["span_name"].startswith("gen_ai.")]
    assert r["shadow_spans"] == 0 and not r["single_owner"]
    assert r["unresolved_reasons"] == {reason: len(nested)}
    assert r["canonical"] == {**r["as_stored"]}  # nothing excluded: an over-count at worst
    return r


@pytest.mark.parametrize("over", [
    {"model": "claude-sonnet-4-5"},  # another model: a distinct nested call
    {"response_model": "claude-sonnet-4-5"},
    {"model": ""},
])
def test_native_child_with_another_model_stays_counted(over):
    owner = _span("t1", "a", response_model="claude-haiku-4-5-20251001")
    _unresolved([owner, _native("t1", "n", "a", **over)], "model_mismatch")


@pytest.mark.parametrize("tokens", [(101, 10), (100, 11), (5400, 10)])
def test_native_child_with_other_tokens_stays_counted(tokens):
    _unresolved([_span("t1", "a"), _native("t1", "n", "a", tokens=tokens)], "usage_mismatch")


def test_native_child_with_other_cost_stays_counted():
    _unresolved([_span("t1", "a"), _native("t1", "n", "a", cost=0.002)], "cost_mismatch")


@pytest.mark.parametrize("name", ["anthropic.messages.stream", "openai.chat", "http.client"])
def test_unknown_nested_instrumentation_is_not_a_proven_duplicate(name):
    # Same values as the parent, but not the known native span: unproven, so counted.
    _unresolved([_span("t1", "a"), _span("t1", "n", parent="a", name=name)],
                "unknown_instrumentation")


def test_native_child_of_a_non_chat_gen_ai_span_is_unresolved():
    owner = _span("t1", "a", name="gen_ai.agent_step")
    _unresolved([owner, _native("t1", "n", "a")], "unknown_instrumentation")


def test_several_children_under_one_owner_all_stay_counted():
    # Two identical native children: no unique pair says which one (if either) is the copy.
    r = _unresolved([_span("t1", "a"), _native("t1", "n1", "a"), _native("t1", "n2", "a")],
                    "several_children")
    assert r["canonical"]["spans"] == 3 and r["unresolved"]["spans"] == 2
    # A matching native child next to an unknown nested span is not unique either.
    _unresolved([_span("t1", "a"), _native("t1", "n", "a"),
                 _span("t1", "x", parent="a", name="tool.llm")], "several_children")


def test_identical_independent_calls_with_their_own_native_children():
    # Two real calls with identical usage, each with its own matching native child: both
    # calls count once; only the two proven copies are excluded.
    rows = [_span("t1", "a", parent="root"), _native("t1", "na", "a"),
            _span("t1", "b", parent="root"), _native("t1", "nb", "b")]
    r = sa.reconcile(rows)
    assert r["shadow_spans"] == 2 and r["unresolved_spans"] == 0
    assert r["canonical"]["spans"] == 2 and r["canonical"]["input_tokens"] == 200


def test_cli_refuses_to_overwrite(tmp_path):
    out = tmp_path / "acct.json"
    out.write_text(json.dumps({"kept": True}))
    with pytest.raises(SystemExit, match="refusing to overwrite"):
        sa.main(["run-id", "--out", str(out)])
    assert json.loads(out.read_text()) == {"kept": True}
