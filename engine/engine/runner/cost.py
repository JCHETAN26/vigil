"""Cost meter (design doc §7): turns per-call token usage into USD, reading the **same
``prices.json`` the Go writer uses** (``ingest/internal/pricing/prices.json``) so there is
exactly one price table in the system and the engine's ``eval_case_results.cost_usd`` agrees
with the writer's ``spans.cost_usd`` by construction.

This is a Python port of ``ingest/internal/pricing``: same normalization (strip an
``anthropic.``/``openai.`` prefix and a trailing date suffix so dated model ids resolve to
their base entry) and the same "unknown model costs 0" behavior.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path

# -YYYYMMDD or -YYYY-MM-DD, matching ingest/internal/pricing/pricing.go.
_RE_DATE8 = re.compile(r"-\d{8}$")
_RE_DATE_DASH = re.compile(r"-\d{4}-\d{2}-\d{2}$")


def _default_prices_path() -> Path:
    """Locate ``ingest/internal/pricing/prices.json`` relative to the repo root. This file is
    ``engine/engine/runner/cost.py`` → repo root is three parents up from ``engine/engine``."""
    override = os.getenv("VIGIL_PRICES_PATH")
    if override:
        return Path(override)
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "ingest" / "internal" / "pricing" / "prices.json"


def _normalize_model(model: str) -> str:
    m = model.strip().lower()
    if "." in m:
        prefix = m.split(".", 1)[0]
        if prefix in ("anthropic", "openai"):
            m = m.split(".", 1)[1]
    m = _RE_DATE_DASH.sub("", m)
    m = _RE_DATE8.sub("", m)
    return m


class CostMeter:
    """Prices token usage from the shared table. Unknown/empty models cost 0 (matching the
    writer), so a pricing gap never fails a run — it shows up as a zero-cost outlier instead."""

    def __init__(self, prices_path: Path | None = None):
        self.path = prices_path or _default_prices_path()
        table = json.loads(self.path.read_text())
        models = table.get("models") or {}
        if not models:
            raise ValueError(f"{self.path} has no models")
        self.prices_as_of: str = table.get("prices_as_of", "")
        # {normalized_model -> (input, output, cache_write_5m, cache_read) per 1M tokens}.
        # cache_* default to 0 for any model that predates cache pricing in the table.
        self._prices: dict[str, tuple[float, float, float, float]] = {
            name: (
                float(p["input"]),
                float(p["output"]),
                float(p.get("cache_write_5m", 0.0)),
                float(p.get("cache_read", 0.0)),
            )
            for name, p in models.items()
        }

    def cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """USD cost for ``input_tokens``/``output_tokens`` at ``model``'s rate. Unknown or
        empty model → 0.0. The zero-cache case of :meth:`cost_with_cache`."""
        return self.cost_with_cache(model, input_tokens, output_tokens, 0, 0)

    def cost_with_cache(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_write_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> float:
        """USD cost for a call that used prompt caching. Anthropic reports cache-write and
        cache-read tokens separately from ordinary (uncached) input tokens and prices them at
        the model's ``cache_write_5m`` and ``cache_read`` rates; output is unchanged. Unknown or
        empty model → 0.0 (matching the Go writer)."""
        base = _normalize_model(model or "")
        if not base:
            return 0.0
        rate = self._prices.get(base)
        if rate is None:
            return 0.0
        in_rate, out_rate, cache_write_rate, cache_read_rate = rate
        return (
            input_tokens / 1e6 * in_rate
            + cache_write_tokens / 1e6 * cache_write_rate
            + cache_read_tokens / 1e6 * cache_read_rate
            + output_tokens / 1e6 * out_rate
        )

    def has_model(self, model: str) -> bool:
        return _normalize_model(model or "") in self._prices


@lru_cache(maxsize=1)
def default_meter() -> CostMeter:
    """A process-wide cost meter over the default price table."""
    return CostMeter()
