# REAL exploratory HotpotQA pilot — frozen evidence

**What this is:** the evidence bundle of one real run of the HotpotQA agent with real model calls,
exported by `python -m engine regress --save` and copied here byte for byte. Verify it offline
with `make demo-evidence` (no database, no API key). Workflow and full discussion:
[docs/HOTPOTQA_VALIDATION.md](../../../docs/HOTPOTQA_VALIDATION.md) §8–§9.

| | |
|---|---|
| run | `cafae810-f2e9-4c50-b34d-cfac8b18c509`, suite `hotpotqa-triage-pilot-27300e54a989` |
| source | commit `125c94d302ad4c75e5267993d9725b148fb1d9b7` |
| model | `claude-haiku-4-5-20251001` (dated Haiku 4.5) |
| design | 20 dev questions × 2 versions × 1 trial = **40 units, all OK**; concurrency 1, cache off |
| change | baseline `top_k5` (`c2eda30a0214…`) → candidate `top_k1` (`4b6ff74526af…`): `retrieval_top_k` 5 → 1 |
| unit cost (Postgres) | baseline $0.146567 + candidate $0.106384 = **$0.252951** |
| gate verdict | **`no_regression`** (default policy: gated `passed` and `TokenF1`), 20 paired cases |
| gated metrics | passed Δ −0.050 (bound +0.036), TokenF1 Δ −0.027 (bound +0.028): neither regressed |
| informational | AllGoldRetrieved Δ **−0.30** (bound −0.079): lower at top_k 1; not gated |
| exported | 2026-10-25T07:31:27Z under Python 3.12.11, NumPy 2.5.4, SciPy 1.18.1 (SciPy not in the bundle metadata, which predates that field; confirmed on the export environment) |

**What it is not.** One exploratory run, 20 questions, one trial: low power and uncalibrated.
`no_regression` means the gate found no evidence of a gated drop at this size, **not** that
top_k 1 is equivalent to top_k 5. Nothing here is a quality, latency or cost claim about either
configuration. An earlier attempt the same day (`45e1b86b`) was stopped by a provider usage cap
before any model output (40 failed units, $0); it is a separate run and not a quality result.

## Files

- `bundle/` — the five exported files, unchanged: `inputs.json` (run/version identities, the
  suite's case ids, every scored (case, trial) row with status, scores, per-unit cost/latency and
  trace id), `policy.json`, `verdict.json`, `metadata.json` (provenance and both version
  manifests: system prompt, tool schema, params), `SHA256SUMS`. No captured transcripts or
  answers, no credentials. `metadata.json` contains absolute paths of the exporting machine; they
  are not secrets, and editing them would break the checksums, so they are kept.
- `provenance.json` — the sha256 of each bundle file (the bundle's own SHA256SUMS is unsigned),
  where it came from, and the claims `make demo-evidence` checks against the recomputed verdict.
- `span-accounting.json` — the pilot's stored LLM spans, reconciled. Every model call had been
  stored twice (the Vigil span and the anthropic SDK's native child span with the same usage;
  fixed in `0e68bd9`), so a plain sum over the stored spans doubles the spend (237 824 / $0.2931
  and 155 398 / $0.2128 — the figures the original pilot summary showed as `[ok]` under the old
  check). `bench/span_accounting.reconcile` pairs all 121 native copies with their Vigil spans
  and returns exactly the per-unit Postgres totals above; the raw rows were not rewritten. The
  record is derived from the reconcile output and root-cause census taken read-only on the
  owner's stack on 2026-10-25 (provenance inside); the verifier cross-checks it against the
  bundle's unit costs.

The pilot's other outputs (plan, unit tables, verify reports, logs, the original `summary.md`)
stay in the owner's validation workspace; the original summary is not copied here because it
marks the doubled span sums as passing.
