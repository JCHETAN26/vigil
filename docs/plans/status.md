# Vigil — project status (resume guide)

_Last updated: 2026-10-25. Purpose: let a fresh session resume without the prior chat._

Vigil is an agent reliability + evaluation platform: it traces agents (OpenTelemetry →
ClickHouse), runs them over datasets, scores them, and detects regressions between agent
versions (CLI, API, dashboard). Diagnosing them (investigation agent) is still planned.
Portfolio project — code quality, tests, and measured results matter more than feature count
(see `CLAUDE.md`).

## Snapshot (2026-10-25)

- **Two open pull requests, neither merged, no GitHub approval recorded:**
  - [PR #1](https://github.com/JCHETAN26/vigil/pull/1), branch
    `improve/reliable-regression-triage`: the reliable regression triage milestone below plus
    the demo-readiness core fixes (gate refuses a pass with no compared gated metric, run-version
    discovery API + run-detail compare action, explicit NumPy/SciPy dependencies with no silent
    normal fallback, ClickHouse calls off the event loop).
  - [PR #2](https://github.com/JCHETAN26/vigil/pull/2), branch `validate/hotpotqa-real-agent`
    (stacked on PR #1): the real HotpotQA validation tooling, its exploratory pilot, the
    single-owner span accounting fix (`0e68bd9`) and the portfolio demo (`demo/`, below).
  - Hosted CI: PR #2's earlier head `0e68bd9` passed the three jobs CI had then
    ([run 40208224559](https://github.com/JCHETAN26/vigil/actions/runs/40208224559)); PR #1's
    head `466765f` passed all three jobs
    ([run 40251722382](https://github.com/JCHETAN26/vigil/actions/runs/40251722382)). These are
    results for those exact heads only. For PR #2's latest head (demo, review fixes, the added
    `evidence` job) see its [checks](https://github.com/JCHETAN26/vigil/pull/2/checks).

### Current verification (2026-10-25, manager, local)

- `make test-all` passed: SDK 51, engine 287 offline + 10 live API, hello_agent 5,
  hotpotqa_agent 22, tau2 2, bench 162, demo 36, Go units + integration, dashboard typecheck.
  **Paid model-call suites were not run.** After the chart dependency change below, `make
  test-all`, all 31 route-mocked Chrome tests, the production build and ruff on changed files
  passed again.
- Real stack: the SYNTHETIC seed imported 1 suite, 12 cases, 4 manifests, 3 runs, 6 run
  versions, 68 results and 270 spans; a second seed wrote nothing; the read-only check gave the
  three expected verdicts; real-database conflict tests (Postgres mismatch, span mismatch,
  partial span set) were refused with both stores unchanged. The 3 pre-existing runs (240
  results) kept exact checksums and span counts/tokens (a cost float aggregate differed only in
  summation order, 9e-17).
- Evidence: the original 121 native/Vigil pairs still reconcile to $0.252951; the committed
  bundle is byte-exact; the minimal SDK + `engine[api]` verifier and tamper refusal passed.
- Final real-Chrome walkthrough **passed** (after the chart fix and a restart) against the
  actual API (127.0.0.1:8092) and dashboard (3318) through the same-origin proxy, no mocks: all
  three synthetic scenario verdicts with a visible Recharts SVG and bar paths on each, their
  manifest diffs, the candidate `syn-q02` affected-case trace with its top-1 retrieval detail,
  and the original real pilot's `no_regression` comparison, with zero page errors and zero
  failed requests.
- **Blank metrics chart, found in the final visual review and fixed:** the compare page's bar
  chart rendered as an empty box. The App Router renders with Next's vendored React 19, and
  Recharts 2.12.7 identified its chart children with react-is 16 (which does not recognise
  React 19 elements) and relied on React 18 element-time `defaultProps`. Fix: Recharts
  2.12.7 → 2.15.4 (exact pin; React 19 support), no other dependency change. New route-mocked
  regression test checks a real SVG with one bar per metric, negative bars below and positive
  above the zero line. It failed against 2.12.7 and passes on 2.15.4; the chart is verified live
  in the final walkthrough above.
- Earlier test counts in this file and in `docs/REGRESSION_TRIAGE.md` §7 are dated history
  (2026-10-23).
- **Real HotpotQA pilot done** (2026-10-25, run `cafae810`, 40/40 units, $0.252951):
  `no_regression`, exploratory only — `docs/HOTPOTQA_VALIDATION.md` §9. No usage-cap blocker is
  current. The larger paid comparison (base100 × 3) still needs separate owner approval and is not
  needed for the demo.
- **Portfolio demo** (`demo/README.md`): `make demo-evidence` re-verifies the committed REAL
  pilot bundle offline (no DB, no key); `make demo` seeds a small SYNTHETIC fixture (12 cases,
  regression / no_regression / incomplete, invented data, no model calls) into the real stores and
  starts the real read-only API and dashboard on loopback ports. Local only; nothing is deployed.
- Components: `ingest/` (Go), `sdk-python/`, `engine/` (Python eval engine + read-only
  FastAPI), `dashboard/` (Next.js), `agents/` (`hello_agent`, `hotpotqa_agent`,
  `tau2_retail_agent`), `bench/`, `demo/`, `deploy/` (Compose only), `docs/`.
- **Not implemented** (planned): `sdk-ts/` (placeholder `.gitkeep`), LLM-as-judge scorers,
  investigation agent, MCP server, Helm/Terraform, any public deployment or API authentication.
- Design docs: `docs/design/data-model.md` (ingest/ClickHouse), `docs/design/eval-engine.md`
  (the eval engine), `docs/REGRESSION_TRIAGE.md` (triage journey), `engine/TRIAGE_API.md`
  (API contract), `docs/TESTING.md` (test gates). This file is the living status/plan.

## Reliable regression triage (PR #1; first validated 2026-10-23)

What the branch adds (walkthrough and commands: `docs/REGRESSION_TRIAGE.md`):
- **One gate** (`engine/engine/gate.py`) used by `engine regress` and the API's `/compare`,
  `/compare/cases`, `/versions/diff`. Sides are run + one explicit agent version (same run
  allowed with two versions; never averaged across versions). Shared defaults: gated `passed`
  (threshold 0.05) and `TokenF1` (0.03). Failed trials score 0 on quality metrics. Verdict is
  `regression` / `no_regression` / `incomplete`, and incomplete is never shown as a pass.
- **Evidence bundles:** `engine regress --save DIR` (never overwrites) and offline
  `bench/regression_verdict.py reproduce DIR` (checksums + full recompute). The checksums are
  unsigned, so they detect edits but do not authenticate where the data came from.
- **Dashboard compare page:** verdict banner, affected cases (filters, paging) with
  baseline and candidate trace links per trial, version config diff with redaction note.
- **Ingest writer reliability:** in-process same-token retry and a global in-flight byte budget
  (`ingest/internal/consumer/retry.go`, `VIGIL_WRITER_*` in `.env.example`). Readiness tracks
  owned work, per partition: a partition's failure counts until that partition flushes or is
  revoked/lost, and is never cleared by another partition (the failed handoff is logged). A
  record whose DLQ publish failed stalls its partition, so the offset never advances past it.
  The writer's compose `stop_grace_period` is 75 s (shutdown flush ≤ 15 s + DLQ drain ≤ 10 s +
  revoke retry window, default 25 s and < 30 s). Retries are bounded by that window, permanent
  insert errors included (they are retried like transient ones, because error classification
  is deferred). A failed handoff or crash replays from the last commit. Dedup across a restart
  holds only if the replay reforms the identical offset range and ClickHouse's dedup window
  still covers it; otherwise the range gets a new token and the MVs double-count until the
  data-model §3.7 rebuild. This is not exactly-once. The receiver maps a non-enqueue error
  to gRPC Internal even when the client has cancelled.
- CORS allow-list (`VIGIL_DASHBOARD_ORIGINS`), and test gates split into `test-infra` and
  `test-paid` (`docs/TESTING.md`). Default ports agree: UI 127.0.0.1:3000, API 8080, and the
  default CORS origins are the :3000 UI (`make dev`). Isolated runs override all of them
  together (example: API 8087, UI 3308, `docs/REGRESSION_TRIAGE.md` §6).

**Validation, 2026-10-23: synthetic check. Not an AI-quality or performance result.** This
synthetic check exercised the real orchestrator → OTLP → **Kafka 4.1** (not the pinned
Redpanda) → writer → ClickHouse path once, with 160 results (40 cases × 2 versions × 2 trials).
It is not a general correctness proof. That original evaluation ran before the final review
fixes. After the fixes, the API, CLI and real Chrome checks were repeated successfully on the
stored results of that run (re-query and re-export only; no new eval runs and no model or
provider calls): CLI and API agreed in both the regression and the improvement direction,
version paging returned 160 stable, unique version rows, all 160 trace links resolved to real
traces with spans, invalid selections were refused, and Chrome comparison, version diff and
trace navigation passed. A format-2 bundle was exported from clean committed code `15a5f5c9`
and reproduced offline, and a tampered copy was rejected. An earlier version-1 interim export is
not final evidence. No artifacts are committed.

Final local checks after the review fixes (details: `docs/REGRESSION_TRIAGE.md` §7):
`make test-all REQUIRE_DB=1` passed: SDK 34, engine 236, hello 5, hotpot 8, tau2 2, bench 83,
Go all, dashboard typecheck. Integration: 8 passed, 1 skipped (no HotpotQA run). Paid
model-call suites were **not run** (no API key). `ruff check`, `go vet -tags=integration ./...`
and `go test -race ./...` passed. Dashboard `build:isolated` passed. All 18 route-mocked Chrome
tests passed, including mutation-checked regressions for the stale-retry, multi-version paging
and incomplete-guard fixes. Review fixes in the gate: a missing gated metric is reported apart
from a failed metric that scores zero, an all-failed side makes the comparison incomplete,
`/compare/cases` with no results returns 200 with an empty case list and an incomplete verdict, UUIDs are canonicalized, and the
bootstrap runs in a thread pool. `npm audit` on production dependencies reports 0
vulnerabilities (Next 15.5.28, Playwright 1.58.2, direct `postcss` 8.5.31, `source-map-js`
1.2.2, `next.postcss` override 8.5.31).

The test counts and checks above are as of 2026-10-23 (they have grown since; CI on the PRs is
the current record). **Open before closing the milestone:** owner review and merge of
[PR #1](https://github.com/JCHETAN26/vigil/pull/1); see its
[checks](https://github.com/JCHETAN26/vigil/pull/1/checks) for current CI status.

## HotpotQA real-agent validation and demo (PR #2, 2026-10-25)

- **Pilot** (`bench/hotpotqa_triage.py pilot`, run `cafae810`, source `125c94d`): 20 dev
  questions × 1 trial × 2 versions (top_k 5 → 1), dated Haiku 4.5, 40/40 OK, $0.252951,
  `no_regression` on the gated `passed`/`TokenF1`; informational AllGoldRetrieved −0.30.
  Exploratory, not an equivalence claim. An earlier attempt (`45e1b86b`) hit a provider usage
  cap before any output and is not a result. Details: `docs/HOTPOTQA_VALIDATION.md` §9.
- **Span double counting found by the pilot, fixed in `0e68bd9`:** anthropic ≥ 1.17's native
  span duplicated every Vigil LLM span's usage. `vigil.wrap` now owns each call alone; copies made
  with `with_options`/`copy`/`with_middleware` stay wrapped (the engine's dev cache keeps its
  store on copies); `bench/span_accounting.py` reconciles historical rows without rewriting them
  and flags nested `gen_ai.*` usage spans as unresolved. Reconciled pilot record:
  `demo/evidence/hotpotqa-pilot/span-accounting.json`.
- **Demo** (`demo/`): frozen pilot bundle (byte-identical, pinned checksums), offline verifier
  with tamper refusal, SYNTHETIC fixture + idempotent seeder that refuses conflicts, launcher,
  dashboard demo notice + SYNTHETIC labels + optional same-origin API proxy, and a CI job that
  re-verifies the evidence from a minimal SDK + `engine[api]` install.

**Known limitations:** the full `npm audit` still reports 3 moderate, dev-only advisories in the
Tailwind 3 chain. npm's suggested fix is a Tailwind 4 migration, which is deferred and not
planned for this milestone. The synthetic check used Kafka 4.1, not the pinned Redpanda. Paid suites were not
run on the final code. Ingest dedup across restarts is limited as described above.

## Current validation environment (2026-10-25)

- The PR #1/#2 work, the HotpotQA pilot and the demo checks ran natively over SSH on an
  **x86_64 Linux** host (the IdeaPad), in the worktree `/home/grv22/projects/vigil-work/repo`.
  A separate checkout on an external SSD is read-only for this work: never write to or remount it.
- Its own local stack for checks: Kafka 4.1 (not the pinned Redpanda), ClickHouse, Postgres,
  with read-only roles; Python 3.12.11 (one shared `engine/.venv`), Node 22, Google Chrome for the
  browser checks. The disk is shared and was nearly full earlier; re-check free space before any
  eval run (the 1 GB floor applies).
- **Product defaults stay portable** (loopback ports 8080/3000, Compose stack, uv venvs): nothing
  in the repo assumes either machine. Whether the Oracle Cloud machine below is still in use is
  not recorded here; nothing has been decommissioned by this work.

## History: Oracle Cloud environment (migrated 2026-09-27)

_As recorded on 2026-09-27; the cloud measurements below are dated history. The statement that
nothing runs on the IdeaPad was true then and is superseded by the section above._

**Primary environment then: the owner's Oracle Cloud ARM machine** (Ubuntu 22.04, aarch64, 4
cores, 23 GB RAM, ~40 GB free disk, no GPU; not shared). The IdeaPad (x86_64, shared) was retired
from Vigil at that point.

- **Toolchain (user-space, no sudo):** Go 1.27.1 in `~/.local/go`, Node 20 in `~/.local/node`,
  uv in `~/.local/bin` with uv-managed CPython 3.12.14; PATH line in `~/.bashrc`. `make` and
  `build-essential` come from apt.
- **Stack:** `cd ingest && make up` builds the ingest image natively as arm64; all upstream
  images (Redpanda, ClickHouse, Postgres, Redis) have arm64 variants. `make smoke` passes.
  Fresh `.env` (new passwords) incl. `POSTGRES_RO_*`/`CLICKHOUSE_RO_*`; RO users created by
  `deploy/create_readonly_users.sh`.
- **Data migrated from the IdeaPad** (`~/vigil-export.tar.gz`, checksums verified):
  - Postgres `pg_restore --no-owner` into the empty DB — every table matches the manifest
    (version_manifests 3, eval_suites 3, eval_cases 130, eval_runs 4, eval_run_versions 4,
    eval_case_results 360, schema_migrations 2).
  - ClickHouse: `spans` 3,591 rows restored (+1 smoke span from this machine). **Restore
    note:** `trace_index` and `agent_version_stats_daily` are fed by MVs on `spans`, so
    inserting `spans` rebuilds both; loading `trace_index.native` on top (as the export's
    manifest suggests) would **double** every trace's sums in the AggregatingMergeTree. The
    MV-derived `trace_index` was instead diffed against the exported one: identical on every
    column for all 534 traces. The rollup was then explicitly rebuilt from `spans` (the MV's
    own SELECT) — 50 keys covering all spans. (The manifest's "10" rollup rows are not
    comparable: 006 had reset the IdeaPad rollup, so it only held post-006 spans.)
  - Migrated runs visible through the API/dashboard: HotpotQA dev20 `810133ff`, HotpotQA
    base100 measurement baseline `785e9e33` (300/300 OK, pass rate 0.637, $1.99), τ² retail
    dev10 `2b252b8d` and `72f45e90`.
- **`data/hotpotqa/` rebuilt** with `bench/build_hotpotqa_corpus.py --n 500`: same HF revision
  `14f0ace3…`, 4,913-paragraph corpus, 500 cases, dev20/base100 ids **identical** to the
  migrated suites; all 120 migrated HotpotQA cases match question/answer/gold titles, all gold
  titles are in the corpus. The CMU original is unreachable from here too (verification still
  `skipped`). τ² venv rebuilt with `make -C agents/tau2_retail_agent tau2-setup` (pinned
  `b7ea907`).
- **`make test-all`:** all unit/offline suites + Go integration green; the four API-backed live
  tests skip because the Anthropic account is still capped (verified: key authenticates, API
  returns "regain access on 2026-10-01 at 00:00 UTC").

### IdeaPad-era workarounds that may no longer be needed (not removed yet)

Kept in place deliberately; each needs a decision before removal.

1. **`build.network: host`** on the ingest image (`deploy/docker-compose.yml`). Existed because
   bridge containers had no egress/DNS. Here Docker uses default iptables (no
   `/etc/docker/daemon.json`) and a bridge container reached `proxy.golang.org` (200) and
   `api.anthropic.com` — the default build network should work.
2. **The `"iptables": false` / no-container-egress note** (`docs/design/data-model.md` §7
   "Container egress"; the k8s-egress open decision below). Not true on this machine.
   Tailscale is also installed and active here and coexists with Docker-managed iptables, so
   the original reason doesn't apply. Consequences to revisit: agents/engine *could* run in
   containers, and Week-6 k8s pods get egress by default.
3. **The disk guard's tight margin** — `python -m engine run` refuses under 1 GB free
   (`VIGIL_MIN_FREE_DISK_GB`, default 1.0), sized for the IdeaPad's nearly full shared disk.
   With ~40 GB free here, the floor could be raised to a more meaningful safety margin (or
   the guard kept as-is as a cheap backstop).
4. Also IdeaPad-specific (not in the original list): **ROS `PYTHONPATH` isolation** — every
   Makefile's `env -u PYTHONPATH` + `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` (this machine has no ROS
   and an empty `PYTHONPATH`; harmless to keep); the **Postgres password gotcha** (fresh volume
   here, not applicable); Redpanda's "shared machine" low-footprint comment and the tight
   per-container memory limits (23 GB RAM here, no one else on the box).

## What's done

### Week 1 — ingestion pipeline, SDK, hello agent
- **`ingest/` (Go):** OTLP receiver (gRPC + HTTP) → validate/normalize → Redpanda → consumer
  → ClickHouse writer. Idempotent inserts (insert-dedup token + dedup in dependent MVs).
  Schema in `ingest/internal/schema/*.sql`: `spans` (wide, denormalized, 365-day eval TTL /
  30-day live), `trace_index`, `agent_version_stats_daily` rollup. Pricing from
  `ingest/internal/pricing/prices.json` (one price table for the whole system).
- **`sdk-python/` (`vigil`):** thin OTel layer. `agent_run` / `llm_call` / `tool_call` /
  `retrieval` spans; `IdentitySpanProcessor` stamps run/eval identity on **every** span;
  `compute_agent_version` content hash; `wrap()` instruments Anthropic (sync + async).
- **`agents/hello_agent`:** tool-use agent (calculator + mock weather), instrumented; used as
  the eval smoke agent.
- Key decisions: `agent_version` = content hash over `{code, prompts, model, params, tools}`
  (data-model §2.3); denormalized identity for single-table scans (§2.2); eval spans 100%
  sampled, retained 365 days (§3.1).

### Eval engine — stages (a)–(d) (`engine/`, design: `docs/design/eval-engine.md`)
- **(a)** SDK `vigil.eval.trial` + `vigil.role`; psycopg3 migrator with `schema_migrations`
  (filename + checksum, fails loudly on drift); Postgres schema
  (`version_manifests`, `eval_suites`, `eval_cases`, `eval_runs`, `eval_run_versions`,
  `eval_case_results`); sync + async data-access (`engine/db/repo.py`).
- **(b)** Agent contract `RunResult`/`ToolCall` (in the SDK, so agents never import the
  engine); `DatasetAdapter` + `LocalJSONAdapter`; deterministic scorers (ExactMatch,
  ExpectedToolCalls, RequiredArguments); cost meter reading the shared `prices.json`.
  Agents are **async** (`AsyncAnthropic`) and report **token counts only** — the engine
  computes cost.
- **(c)** Orchestrator (single Postgres writer) + one worker subprocess per agent version;
  windowed dispatch with per-run **cost budget** (overshoot ≤ concurrency × max per-unit
  cost); dedicated results pipe (fd, not stdout); infra-only retries, timeout = scored
  failure; dev-only Redis LLM cache **refused for measurement** and stamping
  `vigil.cache.hit`; CLI (`python -m engine migrate|suite create|run|show`).
- **(d)** Live integration test (throwaway per-session Postgres DB): 2 cases × 2 trials on
  `hello_agent`, asserts run/version/result rows + trial numbers, ClickHouse trace linkage,
  and measurement+cache refusal. Also added `final_answer` on `RunResult` and the `TokenF1`
  scorer.
- Key decisions (eval-engine.md): subprocess-per-version (SDK version identity is
  process-wide, §3); scorers read the returned transcript, not the trace (§2); psycopg3 +
  raw SQL (§1); materialized cases for reproducibility (§2.2, §9).

### Column promotion (writer + ClickHouse)
- Promoted `vigil.eval.trial`, `vigil.role`, `vigil.cache.hit` to typed `spans` columns
  (`eval_trial Int32` sentinel -1, `role LowCardinality(String)`, `cache_hit UInt8`) via
  migrations `005_span_eval_columns.sql` + writer normalization.
- `agent_version_stats_daily` (`006_rollup_role_and_cache.sql`) adds `role` to the sorting
  key (simulator spans aggregate separately) and excludes cache hits from cost
  (`sumIf(cost_usd, cache_hit = 0)`). Rollup is recreated (it's derived from `spans`).
- **The writer image was rebuilt/redeployed** so new spans populate these columns (old spans
  keep the attributes in `span_attributes`).

### HotpotQA (Session 3) — stages A–C
- **A** — SDK `Retrieval` + `RunResult.retrievals`; engine retrieval scorers in
  `engine/scoring/retrieval.py`: one **merged ranking** per case (all search calls, first-
  retrieval order, deduped); `RetrievalRecall@5` + `@10`, `NDCG` (@10), `AllGoldRetrieved`
  (both-gold boolean). Opt-in by name; EM + TokenF1 score `final_answer`.
- **B** — `bench/build_hotpotqa_corpus.py` builds reproducible artifacts: pinned 500-question
  subset (deterministic by `_id`), **pooled corpus** of all their paragraphs deduped by title
  (~4,913 docs), `data/hotpotqa/{corpus.v1.jsonl, cases.v1.json, subsets/{dev20,base100}.txt}`
  (gitignored — rebuild from source). Source defaults to the **HF parquet mirror** (reliable;
  CMU host is flaky) via `pyarrow`; manifest pins `{name,url,revision (HF commit),sha256}` and
  a `verification` field. `HotpotQAAdapter` (`engine/datasets/hotpotqa.py`) materializes cases
  with the HotpotQA scorer set. Retriever lives in the **agent** package
  (`agents/hotpotqa_agent/.../retrieval.py`): `Retriever` protocol + `BM25Retriever` on
  **`bm25s` (Lucene)** — dense retriever can slot in behind the protocol later.
- **C — complete.** `agents/hotpotqa_agent/.../agent.py`: async multi-hop loop with a `search`
  tool over the pooled corpus; records a retrieval span per search + collects `retrievals`;
  short `final_answer` (FINAL: convention); yes/no handling for comparison questions; **forces
  an answer on the last turn** (drops tools) to bound search and avoid empty answers. Offline
  tests (async stub + tiny retriever) pass. Live smoke validated: bridge → "Yoruba" (4
  searches), comparison → "Northwestern University" (3 searches).

### Week 6 — ingestion load test & backpressure (no LLM calls; in progress)
Plan approved 2026-09-27 (owner decisions: separate DB + topic; Go generator in
`ingest/cmd/loadgen`, Python orchestration in `bench/`; **sustained throughput = highest rate
holding e2e p99 < 5 s with zero loss over a 10-min steady-state window after warm-up**; baseline
with current container limits, then a separately labeled run with limits sized for this machine;
generator pinned to core 0, pipeline to cores 1-3, generator CPU recorded and generator-bound
runs flagged; RetryInfo on gRPC RESOURCE_EXHAUSTED / 429 + Retry-After on HTTP, verified with a
real OTel SDK exporter; refuse fault injection while a real eval run is in progress).
- **Stage 1 — done (pushed, `644b1ac`):** `internal/loadgen` + `cmd/loadgen` (`export` / `run` /
  `reconcile`): templates rebuilt from real `spans` rows via the read-only user and verified
  row-for-row against the production normalizer (515 traces / 3,464 spans, 0 mismatches; 88
  pre-005 rows compared after lifting `vigil.eval.trial` to its column); per-replay deterministic
  ids, shifted timestamps, load-test tags (`vigil-loadtest` identity, `run_kind=live`, no eval
  linkage, `vigil.loadtest.*`); open-loop scheduler, OTLP-spec retries (RetryInfo / Retry-After);
  reconciliation (missing / duplicate / unexpected, `trace_index` consistency, DLQ, e2e latency
  from `ingested_at − sent_at`). Compose profile `loadtest` (`vigil_load`, `otlp.spans.load`,
  `spans.dlq.load`, group `vigil-writer-load`, ports 14317/14318). `bench/ingest_load.py`
  (`up` / `export` / `pilot` / `cleanup` / `hardware`). Pilot (`bench/results/ingest/pilot/`):
  gRPC + HTTP at 1k spans/s, both clean; calibration 704 B/span ClickHouse, 684 B/span Redpanda;
  stage-2 projection peaks at ~21 GB for the 20k spans/s step (tables truncated between steps).
- **Stage 2 — done (pending review):** `bench/ingest_load.py baseline` ramps 500 → 20k spans/s
  per protocol (120 s warm-up + 600 s steady window, every step starts on a drained pipeline with
  truncated load tables; each protocol ramp from a fresh ClickHouse restart). Results in
  `bench/results/ingest/baseline-{asis,sized}/`. **Sustained (zero loss, window e2e p99 < 5 s):**

  | limits | gRPC | HTTP | first failure |
  |---|---|---|---|
  | as-is (ClickHouse 1.5 GiB) | 1,000 | 1,000 | 2,000: load writer crash-loops, never drains |
  | sized (ClickHouse 6.4 GiB) | 2,000 | 2,000 | 5,000: e2e p99 6.7 s (gRPC) / 5.3 s (HTTP), still zero loss |

  - **As-is root cause:** span INSERTs hit ClickHouse `max_server_memory_usage` (1.5 GiB) →
    `MEMORY_LIMIT_EXCEEDED` (code 241, dozens/min in `system.query_log`); the writer exits on a
    failed insert and Docker restarts it, which at 2k spans/s never catches up. Passing steps
    stayed exact because redelivery re-forms the same offset range (same dedup token).
  - **Sized:** zero loss at every step incl. 10k spans/s (7.2M spans each protocol stored exactly
    once), but at 10k the pipeline saturates its 3 cores (receiver ~1.0, ClickHouse ~0.9, writer
    ~0.6) and e2e p99 reaches ~180 s. Request p99 climbs from ~150 ms (2k) to 1.6–1.7 s (5k) and
    6 s (gRPC) / 23 s (HTTP) at 10k — consistent with the receiver's per-trace synchronous produce.
  - **Label caveat:** the 10k steps are labelled "inconclusive: generator behind schedule", but the
    generator used only ~0.3 of its core: the lag is its 32 in-flight slots all waiting on a slow
    receiver (pipeline backpressure), not a generator limit. Those steps fail the 5 s SLO either way.
  - `baseline-asis-uptime11h/`: an earlier as-is attempt against a ClickHouse up ~11 h failed even
    at 500 spans/s (e2e p99 20.8 s) — why every ramp now starts from a fresh ClickHouse.
- **Long-uptime investigation — done (pending review), before stage 3.** `bench/ch_uptime.py`
  (`diagnose` / `merge-probe` / `reset-system-logs` / `soak` / `recover` / `annotate-recovery`);
  results in `bench/results/ingest/{uptime-diagnosis,baseline-asis-fresh,soak-asis-6h}/`. All with
  the original limits (ClickHouse 1.5 GiB cap, load writer 512 MiB), gRPC.

  | ClickHouse state | sustained | what happens |
  |---|---|---|
  | fresh (restart + empty system logs) | 1,000 spans/s | 2,000 fails: span inserts alone exceed 1.5 GiB |
  | long-uptime (6 h soak at 200 spans/s → 12.2 h up) | **0** | stores 0 spans in 5 min with **no** load; writer restarts every ~45 s |

  - **Root cause 1: `system.metric_log` merges.** ClickHouse logs 991 metric columns once per second;
    merges use the Horizontal algorithm (all columns at once) until 131,072 rows (~36 h). Measured on
    the same 129k rows: **4.10 GiB horizontal vs 0.02 GiB vertical**. On the idle 11 h instance the
    merge failed 20,271 times from ~3.4 h of uptime, each retry holding memory at the cap; span
    inserts landing meanwhile failed and the writer crash-looped. Parts, mutations and caches were
    not factors (≤ 8 parts/partition, no mutations, mark cache ~1 MiB). A restart does not clear it
    (the unmergeable parts stay on disk).
  - **Root cause 2 (under load): the spans table's own merges** join in: during the soak the errors
    came first from `vigil_load.spans` merges (79 by 1.1 h), then snowballed (57k errors by 3.4 h).
  - **Root cause 3: load writer OOM while draining a backlog.** Once ClickHouse errors let the writer
    fall behind, every batch fills to 10,000 rows / 32 MiB raw on all 6 partitions at once; with
    ~4 KB attribute maps per span that exceeds 512 MiB → OOM kill (exit 137) every ~45 s. So after
    the soak **restarting ClickHouse alone recovered nothing in 37 min**; raising the writer to 2 GiB
    drained the 2.86M-span backlog in 12.7 min at a **peak of 1,012 MiB** (then restored to 512 MiB).
    Memory scales with batch size × partitions, so draining a backlog after an outage is exactly
    when the writer dies.
  - **Soak reconciliation (after 177 writer crash-restarts):** all **4,320,097** acked spans stored,
    **0 missing, 0 duplicate rows** — but `trace_index` sums to **4,322,222 (+2,125)**: redeliveries
    that re-formed a different offset range got a new dedup token, were inserted twice (the
    ReplacingMergeTree later merged the spans duplicates away), and the MVs double-counted. Redpanda
    retention deleted **0** unconsumed records, but the cushion was only ~9.7k records per
    partition: a little more traffic during the stall would have deleted acknowledged spans.
  - Two measurement-tool fixes on the way: reconcile's GROUP BY now spills to disk (it failed under
    the 1.5 GiB cap on 4.3M rows), and soak/recover rebuild ClickHouse's history from its own logs.
- **Next:** stage 3 fault injection, then stage 4 improvements, each measured on its own:
  1. **Applied 2026-10-01, ahead of stage 3** (`deploy/clickhouse/config.d/system-logs.xml`, same
     file in `config.sized.d` with a 2 GiB merge limit): before = 5,500 memory-limit errors/hour
     on an instance up 45 h (and 0 spans/s sustained after a 6 h soak); after a restart with the
     new settings, **0 new memory-limit errors in the first hour** (tracked memory 0.48 → 0.66
     GiB, `metric_log` merging at level 48, real writer 0 restarts, `make test-all` green). Kept
     `query_views_log` (it proved the partial-insert mechanism). The verification soak + long-
     uptime ramp is still to run. Original plan text:
     **ClickHouse system-log hygiene** (config.d): force Vertical merges for `metric_log`
     (`vertical_merge_algorithm_min_rows_to_activate = 1` via its `<engine>`), TTLs on all system
     logs, `text_log` at warning (it held millions of Trace/Debug rows), disable unused logs
     (`trace_log`, `processors_profile_log`, `query_views_log`), and a
     `merges_mutations_memory_usage_soft_limit` so merges cannot starve inserts. Verify with a
     repeat 6 h soak + long-uptime ramp (target: long-uptime result = fresh result).
  2. **Writer: retry the identical batch in-process** (same offset range, same dedup token) instead
     of exiting — ends the crash loop and the `trace_index` double-count (+2,125 observed). The
     acceptance test exists: `integration/partial_insert_test.go` injects a view failure
     (spans + one view committed, the other failed) and shows an identical same-token retry
     leaves spans, trace_index and the rollup exact (ClickHouse 24.8 pushes a deduplicated block
     to every view; each dedups on its own token), while a re-formed retry double-counts.
     Residue is repaired by `cmd/rebuild` (data-model §3.7, derived from the live view SQL).
  3. **Writer: global memory budget across partitions** (bound total in-flight batch bytes, not
     per-partition rows) — with a test that drains a large backlog within the original 512 MiB.
  4. Larger inserts / async inserts to cut parts and merge load (measure against 1).
  5. Receiver: single produce per request; producer batching; **lag-aware admission control**
     (RESOURCE_EXHAUSTED + RetryInfo / 429 + Retry-After) — the soak shows the retention cushion
     can shrink to ~10k records per partition during a stall.
  Then stage 5: results + design doc.
- **Added 2026-09-28 (from a review of job descriptions; not started — each gets a proposal
  and owner review first):**
  - **AWS via Terraform** (`deploy/terraform/`): the cheapest reasonable design to run the
    stack on AWS briefly, capture results and screenshots, then a **full teardown** (verified:
    nothing left billing). Propose the design and a **cost estimate before creating anything**.
  - **Pipeline monitoring:** metrics and dashboards for throughput, consumer lag, and error
    rates (receiver, writer, Redpanda, ClickHouse), plus a short **runbook** for failures such
    as a ClickHouse outage (builds on the stage-3 fault-injection findings).
  - **CI:** a GitHub Actions workflow running the unit/offline suites on every push, with a
    README status badge.

## What's next (2026-10-25)

1. **Owner review of PR #1 and PR #2**, then merge (PR #2 is stacked on PR #1). The manager runs
   the full local checks and hosted CI on the newest commits first.
2. **Present the demo** (`demo/README.md`: offline evidence, seeded synthetic scenarios, a 3–5
   minute walkthrough script). Optional: record screenshots of the launcher-started UI, where the
   demo notice and SYNTHETIC labels are visible.
3. **Optional, each a separate decision:**
   - the paid HotpotQA `full` comparison (base100 × 3, `docs/HOTPOTQA_VALIDATION.md`) — needs
     owner approval for the spend; not needed for a truthful demo;
   - hardening for any public deployment (authentication, TLS, rate limits, operations) — none
     exists, and the API is local and unauthenticated today;
   - roadmap features (investigation agent, LLM-as-judge scorers, MCP server, TypeScript SDK,
     Kubernetes/Terraform).

Routine, reversible code changes do not need prior approval; paid model runs, deletions of
Docker images/volumes/caches, and anything public do.

## Archived plans (superseded 2026-10-25)

_Kept for history only. These were the next steps at the dates shown; most are done (HotpotQA
Stage D, τ² dev, the A/A test, regression detection, the dashboard and API) and the usage-cap
block below expired. They are not the current plan._

> **2026-09-27:** Vigil moved to the Oracle Cloud ARM machine (see the history section above);
> data, suites and runs migrated and verified. The plan below is unchanged.
>
> **Current state (2026-09-26 pause):** Sessions 1–3 done. HotpotQA Stage D **done** (100×3
> measurement baseline committed). Session 4 **τ²-bench retail done** (agent/adapter/scorer/
> pass^k, τ² isolated in its own venv behind a subprocess domain server; commits `b96e9e5`,
> `8d3ce29`). Week 3 **paired bootstrap regression detection done** (`engine regress` + power/MDE
> harness; commit `2b84ea4`). All unit/offline suites green under `make test-all`.
>
> **Update 2026-09-30:** the account briefly had API access (`make test-all` ran every live suite
> green on the cloud machine) and step 1 ran: τ² dev `6b9a7dd4` — 20/20 units completed, pass^1
> 70.0%, pass^2 60.0%, $0.86 (agent $0.743, simulator $0.121), Postgres↔ClickHouse cost MATCH.
> Step 2 is **partial**: A/A leg A `ddbb1332` completed (300/300, $1.94); leg B `8fd25cc4` hit the
> usage limit again at 00:36:18 UTC (199/300 OK, $1.29) — no gated regression on the 67 paired
> questions (`bench/results/regression/aa-20260930/`); `passed` is "watch" (interval-only signal,
> the paired t CI's ~5% nominal / 6.7% measured false-alarm rate; gated rule 0.0%), and ungated
> `cost_usd` false-alarmed — calibrate informational thresholds (cost first) from A/A variance once
> the full pair exists. **2026-10-01:** leg B re-run in full `29fea4d6` (300/300, $1.98): the
> complete A/A (`bench/results/regression/aa-20261001/`) has **no gated regression and every
> metric ok** (passed Δ −0.010 p 0.22, TokenF1 Δ −0.002 p 0.41, cost Δ +$0.0001 p 0.33).
>
> **BLOCKED on the Anthropic account usage cap — regain access 2026-10-01 00:00 UTC.** No eval
> run or live test (e2e/integration) can succeed until then; the live suites report
> "SKIPPED: API usage cap reached". When the cap lifts, in order:
>
> 1. **Confirm τ² fixes end-to-end.** Re-run the 10×2 τ² retail dev
>    (`engine run --suite tau2-retail-dev10 --agent tau2_retail_agent.agent --mode development
>    --trials 2 --concurrency 4 --budget 2`) and check the report shows near-full completion
>    (tool-error recovery + gold-replay fix), meaningful pass^k, and a Postgres↔ClickHouse cost
>    MATCH. Then `make test-all` (live suites now pass).
> 2. **Real A/A test for the regression detector's FPR on real data.** Run the SAME agent
>    version **twice** on `hotpotqa-base100` (100q×3, measurement, ~$2 each), then
>    `python -m engine regress --baseline <runA> --candidate <runB>`. Expect **no gated
>    regression** and Δ≈0 on every metric; this confirms the empirical false-positive rate on
>    real paired runs (the synthetic study gave gated-rule FPR 0.0%, CI-part 7.3%, MDE ~4.5%
>    pass-rate at 100q — see `bench/results/regression/`). If the A/A flags a gated regression,
>    investigate before trusting the detector.
> 3. **τ² measurement baseline** — size by cost from the dev run, **$6 budget**, and **stop for
>    owner approval** before launching (per the standing rule).
>
> Every eval run is disk-guarded (refuses under 1 GB free) and needs the owner's OK for the
> measurement baseline. Do not prune Docker caches without asking the owner.

1. **HotpotQA Stage D** (as written before it ran; done since — see the 2026-09-26 note above):
   - Build artifacts if absent: `python bench/build_hotpotqa_corpus.py --n 500`.
   - Create suites from `data/hotpotqa/subsets/{dev20,base100}.txt` (adapter `hotpotqa`,
     config `path` + `subset_path`); set `VIGIL_HOTPOTQA_CORPUS=data/hotpotqa/corpus.v1.jsonl`
     so the worker's retriever finds the corpus.
   - **Dev run:** 20 questions, `--mode development`, sanity-check EM/F1/recall/nDCG.
   - **Baseline:** 100 questions × **3 trials**, `--mode measurement`, **`--budget 6`** (Haiku;
     estimate ~$2–5 — re-confirm before running). Retrieval (BM25) is free.
   - **`bench/hotpotqa_baseline.py`:** pull per-case results from Postgres (+ ClickHouse for
     token/latency distributions), report **question-level bootstrap CIs** (resample questions,
     averaging each question's trials) with **trial-to-trial variance separately**, and
     **per-type (bridge/comparison) metrics**; save raw data + generate a Markdown table
     (reproducible, per the `CLAUDE.md` bench convention).
2. **τ-bench (Session 4):** tool-calling agent with an **LLM user simulator** — simulator
   calls tagged `vigil.role=user_simulator` (already supported end-to-end: SDK role stamping,
   `sim_*` token/cost split in `RunResult`/`eval_case_results`, and the rollup's per-role
   aggregation). New adapter + agent under `agents/`.
3. **BFCL + baselines (Session 5):** BFCL adapter (function-calling; ExpectedToolCalls +
   RequiredArguments), plus baseline result tables across agents.
4. **Weeks 3–7 (original roadmap; confirm specifics with the owner — not fully captured in
   the repo):**
   - **Week 3:** LLM-as-judge scorers (the `Scorer` protocol is the seam, eval-engine §6) +
     **bootstrap regression detection** over raw per-trial ClickHouse values + the
     **investigation agent** ("what changed" via `version_manifests` diffs).
   - **Week 4:** Next.js **dashboard** (trace viewer, run comparison, regressions) + the
     **FastAPI** service (deferred from eval-engine Week 2, §Design decisions #4).
   - **Weeks 5–7:** more agents/benchmarks, MCP server, and **Kubernetes** (Week 6 — see
     egress decision below), Helm/Terraform in `deploy/`.

## Open decisions & known issues

- **Kubernetes egress (owner decision needed) — IdeaPad-era; likely moot on the cloud
  machine**, where containers have normal egress (see "Environment" workaround #2). Original
  note, kept until that is decided: Docker runs with `"iptables": false` so
  bridge containers have no outbound NAT/DNS (deliberate — the host runs Tailscale, which
  Docker's iptables management would disrupt). The stack stays in Docker; Python
  agents/engine run natively on the host for Anthropic egress. **Week-6 k8s pods will need an
  egress decision** (scoped MASQUERADE for the pod subnet, Docker/k8s-managed iptables
  reconciled with Tailscale, or an egress proxy). Full context: `docs/design/data-model.md`
  §7 (Container egress). Do not change host firewalling unilaterally.
- **HF mirror pinning/verification.** `cases.v1.json` records the HF dataset commit
  (`revision`) + a `sha256` of the exact bytes. The cross-check against the original
  `hotpot_dev_distractor_v1.json` (matching id/question/answer/supporting_facts for the
  pinned subset, failing loudly on drift) is implemented but currently records
  `"skipped: original unreachable"` because the CMU host times out from here. Re-run the build
  where the original is reachable (or `--original-url` a mirror) to record `"passed"`.
- **GitHub contributors cache** — raised by the owner as an open item; **no corresponding code
  exists in the repo yet**. Clarify scope/intent on resume before acting (do not assume).
- **ROS PYTHONPATH isolation (IdeaPad-era; kept, harmless).** The IdeaPad had ROS on `PYTHONPATH`, which drags system packages
  and a `launch_testing` pytest plugin (and a missing `yaml`) into the venvs. Every Makefile
  test/lint target uses `env -u PYTHONPATH` and `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`; the engine
  also passes `-p asyncio` (autoload is off). The `ament-black` pip warning during installs is
  harmless ROS noise.
- **Postgres password gotcha (IdeaPad-era; not applicable to the fresh cloud volume).** The Postgres data volume was initialized before the current
  `.env`, so host TCP auth can fail even though the container env matches. If so, align it once:
  `docker exec vigil-postgres psql -U vigil -d vigil -c "ALTER USER vigil PASSWORD '<.env value>'"`.

## How to resume

For a no-key look at the product, start with `demo/README.md`. Prereqs: Docker up, the toolchain
on PATH (Go, Node, uv/Python 3.12; the `~/.local/...` paths are the 2026-09 cloud setup), a
repo-root `.env` (gitignored) with these variable
**names** (values not recorded here): `CLICKHOUSE_DB`, `CLICKHOUSE_USER`, `CLICKHOUSE_PASSWORD`,
`POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, plus `POSTGRES_RO_USER`/`POSTGRES_RO_PASSWORD`/`CLICKHOUSE_RO_USER`/
`CLICKHOUSE_RO_PASSWORD` for the dashboard API (`deploy/create_readonly_users.sh`).
Extra first-time steps on a new machine: `make -C agents/tau2_retail_agent install tau2-setup`,
`engine/.venv/bin/pip install -e "engine/.[bench]"` (pyarrow), `npm ci` in `dashboard/`.
Optional runtime env: `VIGIL_HOTPOTQA_CORPUS` (agent corpus path), `VIGIL_OTLP_PROTOCOL`
(`grpc`|`http`), and the `VIGIL_EVAL_*` knobs (see `engine/engine/config.py`).

```sh
# 1. Bring the stack up (Redpanda, ClickHouse, Postgres, Redis, ingest receiver+writer).
cd ingest && make up           # docker compose up -d --build; migrate+topics run once
make migrate                   # apply ClickHouse schema (idempotent)
make smoke                     # end-to-end smoke: emit spans, verify they land

# 2. First-time venvs (each drops PYTHONPATH internally).
cd ../sdk-python && make install
cd ../engine && make install                 # engine + SDK + agents (brings bm25s) into one venv
cd ../agents/hello_agent && make install
cd ../hotpotqa_agent && make install
cd ../../engine && . ../.env >/dev/null 2>&1 || true   # engine tests need POSTGRES_* in env

# 3. Test suites (source ../.env first so Postgres-backed tests run; they skip if PG is down).
cd sdk-python && make test
cd ../engine && set -a && . ../.env && set +a && make test            # unit; add: make test-integration (needs full stack + ANTHROPIC_API_KEY)
cd ../agents/hello_agent && make test                                  # offline; e2e: pytest -m e2e (needs stack + key)
cd ../hotpotqa_agent && make test
cd ../../ingest && go test $(go list ./... | grep -v integration)      # Go units; make test-integration for the tagged suite

# 4. HotpotQA build (rebuilds gitignored artifacts under data/hotpotqa/).
python bench/build_hotpotqa_corpus.py --n 500     # needs pyarrow: pip install -e "engine/.[bench]"
```

Engine Postgres DSN comes from `VIGIL_PG_DSN` or the `POSTGRES_*` vars (`engine/engine/config.py`).
Migrations: ClickHouse via `ingest && make migrate`; Postgres via `engine && python -m engine migrate`.
