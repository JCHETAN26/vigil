"""Vigil eval engine: runs a versioned agent under test over a dataset, scores each case,
records per-case results in Postgres, and links every result back to its ClickHouse trace.

See docs/design/eval-engine.md."""
