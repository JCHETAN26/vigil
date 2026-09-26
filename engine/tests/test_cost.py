"""Unit tests for the cost meter (design §7). Uses the real shared price table so a drift
between the engine and the Go writer would surface here."""

from __future__ import annotations

import json

import pytest

from engine.runner.cost import CostMeter, _default_prices_path, _normalize_model, default_meter


def test_reads_shared_price_table():
    # The default path resolves to ingest/internal/pricing/prices.json.
    assert _default_prices_path().name == "prices.json"
    meter = default_meter()
    assert meter.has_model("claude-haiku-4-5")


def test_cost_matches_table_rates():
    meter = default_meter()
    # claude-haiku-4-5: 1.0 in / 5.0 out per 1M tokens (from prices.json).
    assert meter.cost("claude-haiku-4-5", 1_000_000, 0) == pytest.approx(1.0)
    assert meter.cost("claude-haiku-4-5", 0, 1_000_000) == pytest.approx(5.0)
    assert meter.cost("claude-haiku-4-5", 500_000, 200_000) == pytest.approx(0.5 + 1.0)


def test_model_normalization():
    meter = default_meter()
    # Dated + prefixed identifiers resolve to the base entry, matching the Go normalizer.
    assert meter.cost("claude-haiku-4-5-20251001", 1_000_000, 0) == pytest.approx(1.0)
    assert meter.cost("anthropic.claude-haiku-4-5", 1_000_000, 0) == pytest.approx(1.0)
    assert _normalize_model("claude-sonnet-5-2024-08-06") == "claude-sonnet-5"


def test_unknown_and_empty_model_cost_zero():
    meter = default_meter()
    assert meter.cost("gpt-9-ultra", 1_000_000, 1_000_000) == 0.0
    assert meter.cost("", 1_000_000, 0) == 0.0
    assert not meter.has_model("gpt-9-ultra")


def test_prices_path_override(tmp_path, monkeypatch):
    custom = tmp_path / "prices.json"
    custom.write_text(
        json.dumps({"prices_as_of": "test", "models": {"m": {"input": 2.0, "output": 4.0}}})
    )
    monkeypatch.setenv("VIGIL_PRICES_PATH", str(custom))
    meter = CostMeter()
    assert meter.prices_as_of == "test"
    assert meter.cost("m", 1_000_000, 1_000_000) == pytest.approx(6.0)


def test_empty_table_rejected(tmp_path):
    p = tmp_path / "prices.json"
    p.write_text(json.dumps({"models": {}}))
    with pytest.raises(ValueError, match="no models"):
        CostMeter(prices_path=p)
