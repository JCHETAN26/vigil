"""Measure the dashboard API's query latency (p50/p95/p99) for the endpoints the dashboard hits.

Runs against a live API (default the local read-only API on 127.0.0.1:8080); it discovers a run,
a trace, and a same-suite pair to exercise every endpoint, times N requests each, and writes a
reproducible report (raw JSON + Markdown) under bench/results/perf/.

    make -C engine api            # in one shell (serves 127.0.0.1:8080)
    python bench/api_latency.py   # in another
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def _get(url: str) -> object:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _percentile(sorted_ms: list[float], pct: float) -> float:
    if not sorted_ms:
        return 0.0
    k = (len(sorted_ms) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_ms) - 1)
    return sorted_ms[lo] + (sorted_ms[hi] - sorted_ms[lo]) * (k - lo)


def measure(name: str, url: str, n: int, warmup: int = 3) -> dict:
    for _ in range(warmup):
        _get(url)
    samples: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter()
        _get(url)
        samples.append((time.perf_counter() - t0) * 1000.0)
    s = sorted(samples)
    return {
        "endpoint": name,
        "n": n,
        "p50_ms": round(_percentile(s, 50), 2),
        "p95_ms": round(_percentile(s, 95), 2),
        "p99_ms": round(_percentile(s, 99), 2),
        "max_ms": round(s[-1], 2),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser("api_latency")
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--out", default="bench/results/perf")
    args = ap.parse_args(argv)
    base = args.base.rstrip("/")

    runs = _get(f"{base}/runs?limit=200")["runs"]
    if not runs:
        raise SystemExit("no runs to measure against")
    run_id = runs[0]["id"]
    cases = _get(f"{base}/runs/{run_id}/cases?limit=200")["cases"]
    trace_id = next((c["trace_id"] for c in cases if c.get("trace_id")), None)

    by_suite: dict[str, list[str]] = {}
    for r in runs:
        by_suite.setdefault(r["suite"], []).append(r["id"])
    pair = next((ids for ids in by_suite.values() if len(ids) >= 2), None)

    targets = [
        ("GET /runs", f"{base}/runs?limit=100"),
        ("GET /runs/{id}", f"{base}/runs/{run_id}"),
        ("GET /runs/{id}/cases", f"{base}/runs/{run_id}/cases?limit=200"),
    ]
    if trace_id:
        targets.append(("GET /traces/{trace_id}", f"{base}/traces/{trace_id}"))
    if pair:
        targets.append(("GET /compare", f"{base}/compare?baseline={pair[0]}&candidate={pair[1]}"))

    results = [measure(name, url, args.n) for name, url in targets]

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    raw = {
        "base": base,
        "n": args.n,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "results": results,
    }
    (out / "latency.json").write_text(json.dumps(raw, indent=2))

    lines = [
        "# Dashboard API query latency",
        "",
        f"_Base {base} · {args.n} requests/endpoint (3 warmup) · local read-only API._",
        "",
        "| endpoint | p50 (ms) | p95 (ms) | p99 (ms) | max (ms) |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['endpoint']} | {r['p50_ms']} | {r['p95_ms']} | {r['p99_ms']} | {r['max_ms']} |"
        )
    worst_p95 = max((r["p95_ms"] for r in results), default=0.0)
    lines += ["", f"**Worst-endpoint p95: {worst_p95} ms.**", ""]
    (out / "latency.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nraw -> {out / 'latency.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
