# Presenting Vigil (interview / demo brief)

Short talking points for showing Vigil to someone else. Every figure here is already recorded in
the repo (README "Results so far", `demo/evidence/hotpotqa-pilot/`,
`docs/HOTPOTQA_VALIDATION.md`); nothing below adds a new measurement. The live walkthrough is in
[demo/README.md](../demo/README.md) §3.

## One-sentence pitch

Vigil traces AI agents, runs them over evaluation suites, and uses paired statistical comparisons to decide
whether a new agent version is a regression, a non-regression or *incomplete*, and the CLI and
dashboard give the same verdict because they call the same gate code.

## Real evidence vs synthetic walkthrough: say which is which

| | what it is | what you can claim |
|---|---|---|
| **REAL** HotpotQA pilot `cafae810` | 20 dev questions × 2 versions (retrieval top_k 5 → 1) × 1 trial, dated Haiku 4.5, 40/40 units OK, $0.252951. Frozen bundle; `make demo-evidence` recomputes the verdict offline and refuses tampered files. | The gate found no gated regression (`passed`, `TokenF1`), while the informational AllGoldRetrieved fell 0.30. Exploratory: low power, **not** evidence that the two configs are equivalent. |
| **SYNTHETIC** fixture (`vigil-demo-synthetic-*`) | 12 invented cases, 3 runs, invented scores and traces, no model calls, seeded straight into the real Postgres/ClickHouse. | It shows the UI, the API and the gate working on stored data: one `regression`, one `no_regression`, one `incomplete`. Its numbers measure nothing. |

The dashboard labels synthetic data itself (demo notice, SYNTHETIC badges, run notes).
`make demo-smoke` checks the synthetic walkthrough in a real browser; the real pilot is checked
offline, so a fresh clone needs neither the original database nor an API key.

## Architecture and the trade-offs behind it

- **Ingest path:** Python SDK on OpenTelemetry → Go OTLP receiver (gRPC + HTTP) → Kafka-API queue
  → writer → ClickHouse. The queue buffers bursts and decouples receiving from storage;
  backpressure can propagate when storage slows. Delivery is at-least-once, not exactly-once;
  this is not a lossless-delivery guarantee under every failure or retention condition.
  A replay that re-forms a different offset range can double-count the rollups until a rebuild
  (documented in `docs/plans/status.md`).
- **Two stores:** ClickHouse for spans (append-heavy, aggregated), Postgres for suites, runs,
  versions and per-trial results (relational, transactional). This split is why the demo seeder
  has to preflight both stores and refuse any partial or conflicting set.
- **One gate** (`engine/engine/gate.py`) behind `engine regress`, the API and the evidence
  verifier. Sides are run + explicit agent version, never averaged across versions. Failed trials
  score 0 on quality metrics; cost and latency use successful trials only. The default confidence
  bound is a paired Student-t interval, with percentile and BCa bootstrap alternatives. Missing
  data cannot produce a passing verdict; adverse gated evidence can still report `regression`.
  The trade-off is
  a conservative gate: with small samples it often says "no evidence of regression", which is not
  the same as "safe".
- **Evidence bundles:** `regress --save` exports inputs, policy and verdict with checksums, and the
  verdict is recomputed offline. The checksums are unsigned: they detect edits but do not prove
  where the data came from.
- **Read-only API, local only:** the API connects with read-only database roles and binds
  loopback, with no authentication. That is acceptable for a local demo; it is not a deployment.

## The double-cost incident (a good story to tell)

The real pilot's ClickHouse span totals came out **exactly twice** the Postgres unit costs. Cause:
anthropic SDK ≥ 1.17 emits its own native OpenTelemetry span for each call, and because
`vigil.wrap` made its span current, the native span landed as a child carrying the same usage. The
SDK unit tests used duck-typed stubs with no native tracing, so they could not catch it. The
original pilot summary had marked the doubled sums as passing.

The fix began in `0e68bd9`: `vigil.wrap` now owns each model call alone, and
`bench/span_accounting.py` reconciles historical rows *without rewriting them*. The later demo
review also fixed client copies so they retain their wrapper and cache settings. Reconciliation
pairs all 121 native copies with their Vigil spans and reproduces the Postgres totals exactly.
The frozen evidence carries that reconciliation, and the offline verifier cross-checks it. Lesson
worth saying out loud: a check that says `[ok]` is only as good as what it compares against;
reconcile two independent sources.

## Limitations to state before being asked

- One real triage pilot, 20 questions × 1 trial: exploratory and uncalibrated.
- The synthetic demo skips the agent, SDK, receiver, queue and writer (seeded replay data).
- Validated on the owner's local stack with Kafka 4.1 instead of the pinned Redpanda. Paid model
  suites were not re-run on the final code.
- Local only: no auth, no TLS, nothing deployed. Planned but not built: TypeScript SDK,
  LLM-as-judge scorers, investigation agent, MCP server, Helm/Terraform.
- Hosted CI runs the offline suites, a minimal-install evidence check and a route-mocked browser
  suite; the stack-backed and paid gates run only on the owner's machine.

## CV bullets (truthful; no invented impact)

- Built Vigil, an agent evaluation and regression-detection platform: Go OpenTelemetry ingest
  through a Kafka-API queue into ClickHouse, a Python eval engine on Postgres, and a Next.js
  dashboard.
- Designed a paired statistical regression gate with Student-t and bootstrap confidence methods,
  shared by the CLI, the API and an offline evidence
  verifier, which refuses a passing verdict when the required evidence is incomplete.
- Ran a real HotpotQA RAG triage pilot (40 model-backed units, $0.25) and shipped its frozen,
  checksum-pinned evidence with an offline verifier that rejects tampered bundles.
- Found and fixed span cost double counting caused by the provider SDK's native tracing, and wrote
  a non-destructive reconciliation that reproduces the stored unit costs exactly.
- Built a no-key demo: a labelled synthetic fixture seeded idempotently into the real stores, a
  launcher, and a real-browser smoke check whose expectations are derived from the fixture and the
  production gate.

Do not add speed-ups, user counts, accuracy gains or cost savings: none were measured.
