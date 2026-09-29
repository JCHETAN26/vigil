# Vigil — project status (resume guide)

_Last updated: 2026-09-27. Purpose: let a fresh session resume without the prior chat._

Vigil is an agent reliability + evaluation platform: it traces agents (OpenTelemetry →
ClickHouse), runs them over datasets, scores them, and (later) detects regressions and
diagnoses them. Portfolio project — code quality, tests, and measured results matter more than
feature count (see `CLAUDE.md`).

## Snapshot

- Branch: `main`. Everything below is committed.
- Components: `ingest/` (Go), `sdk-python/`, `engine/` (Python eval engine), `agents/`
  (`hello_agent`, `hotpotqa_agent`), `bench/`, `deploy/`, `docs/`.
- Design docs: `docs/design/data-model.md` (ingest/ClickHouse), `docs/design/eval-engine.md`
  (the eval engine). This file is the living status/plan.

## Environment (migrated 2026-09-27)

**Primary environment: the owner's Oracle Cloud ARM machine** (Ubuntu 22.04, aarch64, 4 cores,
23 GB RAM, ~40 GB free disk, no GPU; not shared). **The IdeaPad (x86_64, shared) is retired
from Vigil** — nothing runs there any more.

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
- **Next:** stage 3 fault injection, stage 4 improvements (single produce per request, producer
  batching, in-writer retry with identical dedup token, lag-aware admission control), stage 5
  results + design doc.
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

## What's next (in order)

> **2026-09-27:** Vigil moved to the Oracle Cloud ARM machine (see "Environment" above); data,
> suites and runs migrated and verified. The plan below is unchanged.
>
> **Current state (2026-09-26 pause):** Sessions 1–3 done. HotpotQA Stage D **done** (100×3
> measurement baseline committed). Session 4 **τ²-bench retail done** (agent/adapter/scorer/
> pass^k, τ² isolated in its own venv behind a subprocess domain server; commits `b96e9e5`,
> `8d3ce29`). Week 3 **paired bootstrap regression detection done** (`engine regress` + power/MDE
> harness; commit `2b84ea4`). All unit/offline suites green under `make test-all`.
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

1. **HotpotQA Stage D** (not started):
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

Prereqs: Docker up, the toolchain on PATH (`~/.local/go/bin`, `~/.local/node/bin`,
`~/.local/bin` — see "Environment"), a repo-root `.env` (gitignored) with these variable
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
