# Vigil eval engine

Status: **accepted** · Scope: how Vigil runs an agent under test over a dataset, scores
each case, records results, and links them back to traces. Design only — no code yet.

The eval engine (`engine/`) is the piece that turns Vigil from a tracing system into an
*evaluation* system. It runs a versioned agent over a dataset, scores each case with
deterministic checks (LLM-as-judge comes in Week 3), records per-case results in Postgres,
and links every result to its trace in ClickHouse. It is the data source the Week-3
regression detector and the investigation agent build on.

This builds directly on two things already in place:

- The **Vigil SDK** (`sdk-python/`) already stamps `vigil.run.kind=eval`,
  `vigil.eval.run_id`, and `vigil.eval.case_id` onto **every** span of a run (via
  `agent_run(...)` and the `IdentitySpanProcessor`), forces content capture for eval runs
  (`capture_content_eval=True`), and computes an `agent_version` content hash
  (`compute_agent_version`) over `{prompts, tools, model, params, code}` — exactly the
  fields the `version_manifests` table (data-model §3.6) needs.
- The **`spans` table** (data-model §3.1) retains eval spans for **365 days**, never
  samples eval runs, and denormalizes `agent_id` / `agent_version` / `eval_run_id` /
  `eval_case_id` onto every span, so eval queries are flat `WHERE` scans.

The engine runs **natively on the host** (data-model §7 infra decision): it calls the
Anthropic API over the host network and reaches the stack via the published `127.0.0.1`
ports (OTLP `4317`/`4318`, Postgres `5432`, ClickHouse `8123`/`9000`, Redis `6379`).

## Design decisions (resolved)

1. **Postgres access:** `psycopg3` (sync) with **raw-SQL migrations**, mirroring the Go
   ingest migrator (`ingest/cmd/migrate`). SQL is the source of truth; no ORM.
2. **Scoring input:** scorers read a **structured `RunResult` transcript** returned by the
   agent (final output + the tool calls it made), not the ClickHouse trace — so
   deterministic scoring is synchronous and does not wait on the async trace pipeline. The
   `trace_id` still links each result to its full trace for depth.
3. **Dev LLM cache:** backed by the in-stack **Redis**, **off by default**, and **refused
   for measurement runs** (caching would hide run-to-run variance).
4. **Week-2 surface:** an importable **library + CLI**; the FastAPI wrapper CLAUDE.md calls
   for is deferred to Week 4 (the runner logic is the value; the API is a shell).

---

## 1. What the engine does (and does not) do

**Does (Week 2):** run one agent under test over a dataset suite, with **repeated trials
per case**; bounded-concurrency execution with retries, per-case timeouts, and a per-run
cost budget; deterministic scoring; persist suites, cases, runs, and per-case results in
Postgres; upsert version manifests; link every result to its ClickHouse trace.

**Does not:** LLM-as-judge scorers and bootstrap regression detection (**Week 3**), the
dashboard and a FastAPI service (**Week 4**), real dataset wiring beyond the adapter
interface, and Kubernetes.

---

## 2. Postgres schema

Postgres is the **metadata + results** store; ClickHouse remains the trace store. DDL
lives in `engine/db/migrations/*.sql` (source of truth), applied idempotently by
`engine/db/migrate.py` — a small `psycopg3` runner that executes the numbered files in
order, exactly as the Go `cmd/migrate` does for ClickHouse.

### 2.1 `version_manifests` (from data-model §3.6)

The full configuration behind each opaque `agent_version` hash, so two versions can be
diffed field-by-field (the investigation agent's "what changed?" step).

```sql
CREATE TABLE IF NOT EXISTS version_manifests (
    agent_id      text        NOT NULL,
    agent_version text        NOT NULL,   -- the content hash
    git_sha       text        NOT NULL,
    model         text        NOT NULL,
    params        jsonb       NOT NULL,   -- decoding params
    prompts       jsonb       NOT NULL,   -- prompt templates {name -> text}
    tools         jsonb       NOT NULL,   -- tool/function definitions
    code_ref      jsonb,                  -- optional: file hashes, entrypoint, deps
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (agent_id, agent_version)
);
```

**Decision — the eval engine owns this upsert.** The ingest consumer only ever sees the
opaque hash on spans; the engine has the *full* config (via the agent contract §4 +
`compute_agent_version` + `git_sha`), so it upserts the manifest the first time it runs a
version. This refines data-model §3.6's "consumer or SDK upserts" note. (The SDK could also
upsert for live agents later; not needed for evals.)

### 2.2 Suites and cases

```sql
CREATE TABLE IF NOT EXISTS eval_suites (
    id         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    name       text        NOT NULL UNIQUE,
    adapter    text        NOT NULL,       -- 'tau-bench' | 'hotpotqa' | 'bfcl'
    config     jsonb       NOT NULL,       -- adapter params: split, subset, path, version
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS eval_cases (
    suite_id  uuid   NOT NULL REFERENCES eval_suites(id) ON DELETE CASCADE,
    case_id   text   NOT NULL,             -- dataset item id (HotpotQA question id, ...)
    input     jsonb  NOT NULL,             -- passed to the agent's run()
    expected  jsonb  NOT NULL,             -- scoring spec (§6)
    tags      text[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (suite_id, case_id)
);
```

Cases are **materialized** from the adapter into `eval_cases` when a suite is created, with
the dataset version pinned in `eval_suites.config`. A re-run therefore scores the *identical*
set of cases — reproducibility, per the CLAUDE.md benchmark convention.

### 2.3 Runs, per-version sub-runs, and results

```sql
CREATE TABLE IF NOT EXISTS eval_runs (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    suite_id        uuid        NOT NULL REFERENCES eval_suites(id),
    agent_id        text        NOT NULL,
    mode            text        NOT NULL CHECK (mode IN ('measurement', 'development')),
    status          text        NOT NULL DEFAULT 'pending',  -- pending|running|succeeded|failed|aborted
    cost_budget_usd numeric,                                 -- NULL = unbounded
    cost_spent_usd  numeric     NOT NULL DEFAULT 0,
    concurrency     int         NOT NULL DEFAULT 4,           -- max in-flight (case, trial) units
    trials_per_case int         NOT NULL DEFAULT 1,           -- repeats per case, for variance (Week-3 bootstrap)
    cache_mode      text        NOT NULL DEFAULT 'off',      -- off|read|read_write
    git_sha         text,
    notes           text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,
    finished_at     timestamptz
);

-- One row per agent version in the run == one worker subprocess (§3).
CREATE TABLE IF NOT EXISTS eval_run_versions (
    run_id         uuid    NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
    agent_version  text    NOT NULL,
    status         text    NOT NULL DEFAULT 'pending',
    cost_spent_usd numeric NOT NULL DEFAULT 0,
    cases_total    int     NOT NULL DEFAULT 0,   -- (case × trial) units to run
    cases_done     int     NOT NULL DEFAULT 0,
    started_at     timestamptz,
    finished_at    timestamptz,
    PRIMARY KEY (run_id, agent_version)
);

CREATE TABLE IF NOT EXISTS eval_case_results (
    id                bigserial   PRIMARY KEY,
    run_id            uuid        NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
    agent_version     text        NOT NULL,
    case_id           text        NOT NULL,
    trial             int         NOT NULL DEFAULT 0,     -- 0-based repeat index within the run
    trace_id          text,                               -- links to ClickHouse spans (§8)
    passed            boolean,
    score             double precision,                   -- composite score
    scores            jsonb       NOT NULL DEFAULT '{}',  -- per-scorer {name -> {passed, score, detail}}
    status            text        NOT NULL,                -- ok | error | timeout
    error             text,
    attempts          int         NOT NULL DEFAULT 1,
    input_tokens      int,                                 -- AGENT only (excludes user simulator)
    output_tokens     int,
    cost_usd          numeric,                             -- AGENT only
    sim_input_tokens  int,                                 -- LLM user simulator (e.g. tau-bench), if any
    sim_output_tokens int,
    sim_cost_usd      numeric,
    latency_ms        int,
    output            jsonb,                               -- bounded transcript (final output + tool_calls)
    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, agent_version, case_id, trial)
);

CREATE INDEX IF NOT EXISTS idx_case_results_run     ON eval_case_results (run_id);
CREATE INDEX IF NOT EXISTS idx_case_results_version ON eval_case_results (agent_version, case_id);
CREATE INDEX IF NOT EXISTS idx_case_results_trace   ON eval_case_results (trace_id);
```

An `eval_run` is one logical measurement over one suite for one `agent_id`, possibly
spanning **several agent versions** (e.g. baseline vs candidate). Each version is executed
by its own worker subprocess and tracked in `eval_run_versions`. The engine sets the SDK's
`vigil.eval.run_id` to `str(eval_runs.id)`, so ClickHouse spans join back on
`(eval_run_id, agent_version, eval_case_id, trial)`.

---

## 3. Runner architecture

The core constraint: **the SDK's `agent_version` is process-wide** (`vigil.init` sets a
global `TracerProvider` and a resource carrying `vigil.agent.version`). To evaluate more
than one version, the engine must use **one subprocess per version**.

```
 orchestrator (engine process, single Postgres writer)
   │  load suite + materialized cases; upsert version_manifests;
   │  create eval_runs + eval_run_versions
   │
   ├─ spawn worker subprocess  ── agent version A ──┐
   │    control pipe: (case, trial) units, windowed │  vigil.init(version=A) once
   │    results pipe: JSONL result records          │  bounded-concurrency loop over units
   │    stdout/stderr: logs only                    │  scores each unit synchronously
   │                                                │  vigil.shutdown() flushes spans
   └─ spawn worker subprocess  ── agent version B ──┘  (same, independent process)
```

### 3.1 Orchestrator (parent)

Loads the suite and its cases, resolves the target version(s), upserts each manifest,
creates the `eval_runs` + `eval_run_versions` rows, then spawns one worker per version. It
is the **single writer** to Postgres: workers stream results, the parent persists them. It
owns the **per-run cost budget** because a run can span multiple version subprocesses — only
the parent sees the total.

### 3.2 Worker (`python -m engine.runner.worker`)

Given the agent module path, the run/version ids, and OTLP/Redis config, the worker:

1. Calls `vigil.init(service_name, agent_id, agent_version, git_sha)` **once** — fixing this
   process's version identity.
2. Builds the client the agent will use: `anthropic.Anthropic()` → `vigil.wrap(...)` →
   optional Redis cache wrapper (§7). Client construction is **engine-controlled** so
   caching and instrumentation are guaranteed regardless of the agent.
3. Runs the `(case, trial)` units dispatched by the parent, up to `concurrency` in flight
   (an `asyncio` semaphore), calling the agent contract's `run(client, input, ..., trial=n)`
   (§4), scoring each result synchronously (§6), and writing one JSONL result record per unit
   to a **dedicated results pipe** — a separate file descriptor, *not* stdout — so a stray
   `print` or library log can never corrupt the protocol. **stdout and stderr carry logs
   only.**
4. On completion (or a drain signal), calls `vigil.shutdown()` to flush buffered spans to
   the receiver.

### 3.3 Windowed dispatch + per-run cost budget

The parent feeds `(case, trial)` units to the worker over the control pipe, keeping at most
`concurrency` outstanding, reads results as they arrive, refills the window, and
**aggregates run-level cost across all version subprocesses**. When
`cost_spent_usd ≥ cost_budget_usd`, it stops dispatching new units and signals the workers to
drain in-flight work and exit; the run ends with `status='aborted'` (reason: budget). Cost
per unit comes from the worker's local token accounting (§7 cost meter) reported in each
result record — **including any LLM user-simulator cost** (§4) — so the budget reacts
immediately rather than waiting on ClickHouse.

**Maximum overshoot.** The budget is checked between dispatches while up to `concurrency`
units are already running, so a run can exceed its budget by at most
**`concurrency × (maximum per-unit cost)`** — the in-flight units that finish after the
threshold is crossed. Size `concurrency` and `cost_budget_usd` with that bound in mind.

### 3.4 Retries, timeouts, crash behavior

- **Retries — infrastructure failures only.** Retry only transient *infra* errors (HTTP
  **429**, **5xx**, connection errors) up to `max_attempts` with exponential backoff +
  jitter. `attempts` is recorded.
- **Timeouts are a scored failure, not a retry.** A `(case, trial)` that exceeds its
  per-case wall-clock limit (`asyncio.wait_for`) is recorded as `status='timeout'`,
  `passed=false` — and is **not** retried. Retrying a slow agent would give it extra
  attempts the real deployment wouldn't get and inflate the pass rate; a timeout is a
  genuine failure of that trial. An assertion that didn't hold is likewise a real
  `status='ok'`, `passed=false` result, never retried. An overall run guard bounds the whole
  run.
- **Crash behavior** — results stream and persist incrementally, so a crash leaves partial
  `eval_case_results` with the run marked `failed`/`aborted`; workers are killed on parent
  exit. Re-running is a fresh `eval_run` (results are immutable per run).

---

## 4. Agent contract

An "agent under test" is a Python module the engine imports in the worker. It exposes:

- `AGENT_ID: str`, and the manifest inputs `prompts: Mapping[str,str]`, `tools`, `model`,
  `params` — from which the engine computes the version (`compute_agent_version`) and
  upserts `version_manifests`.
- `run(client, case_input, *, eval_run_id, eval_case_id, trial) -> RunResult`.

```python
@dataclass
class RunResult:
    output: Any                       # final answer, for ExactMatch and judges
    tool_calls: list[ToolCall]        # [{name, arguments}] the agent made, for tool scorers
    trace_id: str                     # 032x hex; links to ClickHouse
    input_tokens: int                 # AGENT tokens (excludes user simulator)
    output_tokens: int
    cost_usd: float                   # AGENT cost
    sim_input_tokens: int = 0         # LLM user-simulator tokens (tau-bench), if any
    sim_output_tokens: int = 0
    sim_cost_usd: float = 0.0
```

The engine passes the (wrapped, maybe-cached) `client` in and the `trial` index, so the
agent does not construct its own client in eval mode and the SDK can stamp `vigil.eval.trial`
(see below). `hello_agent.run()` currently returns
`{answer, trace_id, agent_version}`; it will be **extended** to this richer `RunResult` — a
small change, since its loop already collects `tool_use` blocks. Returning the transcript is
what lets deterministic scorers run synchronously without reading the trace.

**Required SDK additions.** `agent_run(...)` gains a `trial: int | None` parameter that
stamps `vigil.eval.trial` onto every span of the run (through the existing
`IdentitySpanProcessor` / `RunContext`). For agents with an LLM **user simulator** (e.g.
tau-bench), the simulator's model calls are tagged with the span attribute
`vigil.role=user_simulator`, so their tokens and cost can be summed into the `sim_*` fields
and excluded from the agent's cost metrics — while still counting toward the run budget.

---

## 5. Dataset adapter interface

One interface so tau-bench, HotpotQA, and BFCL plug in identically:

```python
@dataclass
class Case:
    case_id: str
    input: Any            # passed to agent.run
    expected: dict        # scoring spec (§6)
    tags: list[str]

class DatasetAdapter(Protocol):
    name: str
    version: str
    def load(self) -> Iterable[Case]: ...
```

- **`TauBenchAdapter`** — maps tau-bench tasks; `input` = the user goal + environment
  seed; `expected` = required tool calls / final DB state assertions. tau-bench drives an
  **LLM user simulator**; those model calls are tagged `vigil.role=user_simulator` and their
  usage is tracked in the `sim_*` fields (§4) — counted toward the run budget but excluded
  from agent cost metrics.
- **`HotpotQAAdapter`** — `input` = the question; `expected` = the gold answer (ExactMatch)
  and, later, supporting facts for a faithfulness judge.
- **`BFCLAdapter`** — `input` = the function-calling prompt; `expected` = the expected tool
  name(s) and required arguments.

Adapters are registered by `name` and selected via `eval_suites.adapter`; `load()` results
are materialized into `eval_cases` at suite creation (§2.2). The dataset version is pinned
in `eval_suites.config` so the case set is reproducible.

---

## 6. Scorers (deterministic first, judge-ready)

```python
@dataclass
class ScoreResult:
    name: str
    passed: bool
    score: float          # 0.0–1.0
    detail: dict

class Scorer(Protocol):
    def score(self, case: Case, result: RunResult) -> ScoreResult: ...
```

Deterministic scorers for Week 2:

- **ExactMatch** — normalized compare of `result.output` to `expected.answer`.
- **ExpectedToolCalls** — the set (or ordered sequence) of tool names in
  `result.tool_calls` matches `expected.tool_calls`.
- **RequiredArguments** — for named tool calls, required arguments are present and (where
  specified) equal to `expected.arguments`.

A case's `expected` lists which scorers apply and their parameters. `CaseResult` aggregates
their `ScoreResult`s into `passed` (all must pass), a composite `score`, and a per-scorer
`scores` jsonb stored on `eval_case_results`.

**Week-3 extension seam.** The `Scorer` protocol is the seam for LLM-as-judge: an
`LLMJudgeScorer` takes the same `(case, result)`, calls a judge model through the engine's
client infrastructure, returns a graded `ScoreResult`, and — when it needs span-level
context (e.g. retrieved documents for RAG faithfulness) — fetches the trace by
`result.trace_id`. The measurement-run cache refusal (§7) applies to judge calls too.

---

## 7. Cost meter and the dev-only LLM cache

**Cost meter** (`engine/runner/cost.py`) — reads the **same `prices.json` the Go writer
uses** (`ingest/internal/pricing/prices.json`), so there is exactly **one price table** in
the system. It turns per-call token usage into `cost_usd`, accumulated per `(case, trial)`
and per run and split into **agent** vs **user-simulator** cost (§4). This local accounting
drives the real-time budget (§3.3); the simulator's cost counts toward the budget but is
recorded in the `sim_*` fields so agent cost metrics exclude it.

**LLM cache** (`engine/runner/cache.py`, development only) — a caching wrapper around the
`vigil.wrap`-ed client, backed by **Redis**:

- Key = `sha256` of canonical `(model, params, system, tools, messages)`; value =
  serialized response; TTL configurable.
- **Off by default** (`VIGIL_EVAL_CACHE=off`); `read` / `read_write` enabled only for
  `mode='development'`.
- **Hard refusal:** if a run is `mode='measurement'` *and* caching is requested/enabled, the
  runner **refuses to start** with a clear error. Caching would serve identical responses
  across runs and hide the run-to-run variance that regression detection depends on — a
  cached measurement is a corrupt measurement. The check is enforced at run creation and
  re-checked in the worker before any cached read.

---

## 8. How results link back to ClickHouse

The two stores play complementary roles:

- **Postgres** — the verdict store: pass/fail, scores, attempts, budget/cost summary,
  latency, and the bounded transcript. "What passed, and by how much."
- **ClickHouse** — the telemetry store: every span with full token/cost/latency detail,
  100%-sampled and retained 365 days for eval. "What actually happened, in full
  distribution."

**Linkage keys:**

- Point lookup — `eval_case_results.trace_id` ↔ `spans.trace_id` (one trial's full trace).
- Aggregate — `(eval_runs.id → vigil.eval.run_id, agent_version, case_id → vigil.eval.case_id,
  trial → vigil.eval.trial, run_kind='eval')` ↔ `spans`. Within a trace, simulator spans are
  distinguished by `vigil.role='user_simulator'`.

**One price table, so costs agree.** Both the engine's local accounting
(`eval_case_results.cost_usd`) and the ingest writer's `spans.cost_usd` compute from the
**same `prices.json`** (§7) over the same token counts, so they agree by construction.
ClickHouse remains the source of truth for full distributions; Postgres holds the per-trial
summary and drives the budget.

**Week-3 hand-off.** Bootstrap regression detection (Week 3) resamples the **raw per-trial
values in ClickHouse** filtered by `(agent_id, agent_version, run_kind='eval')` within the
365-day window — which is exactly why runs record **multiple trials per case**
(`trials_per_case`): the bootstrap needs the per-trial distribution, not a single point.
Postgres supplies the pass/fail and score series per version and the `version_manifests` rows
for diffing good-vs-bad.

---

## 9. Trade-offs & alternatives considered

- **Subprocess-per-version** vs threads/async in one process. Forced by the process-wide SDK
  `agent_version`. Costs process-spawn and IPC overhead; buys unambiguous version identity,
  isolation, and the option to run different code/deps per version. Threads can't give each
  a distinct process-wide provider.
- **Parent-side windowed dispatch** vs each worker self-managing its budget. Parent dispatch
  makes the per-run budget authoritative across *all* version subprocesses and keeps Postgres
  writes centralized; the cost is a small pipe-based line protocol.
- **Dedicated results pipe** vs stdout for the worker→parent protocol. A separate file
  descriptor keeps the result stream immune to a stray `print` or library log; stdout/stderr
  stay human-readable for logs. The cost is one extra fd to wire up per worker.
- **Structured transcript** vs reading the trace to score. The transcript makes deterministic
  scoring synchronous and free of the trace pipeline's eventual consistency (writer batch +
  flush). The `trace_id` still links for depth, and judges can read the trace when they need
  span-level context.
- **psycopg3 + raw SQL** vs an ORM. Transparency and consistency with the repo's "SQL is the
  source of truth" pattern (the Go migrator); less abstraction to reason about for a
  metadata store of this size.
- **Redis cache** vs a local file. Reuses the in-stack Redis, is shared across dev runs, and
  is hard-gated against measurement. A local file would isolate per developer but adds a file
  to manage and isn't shared.
- **Materializing cases** vs loading from the adapter each run. Materializing pins the exact
  case set for reproducibility; loading live would let a dataset shift under two runs of the
  "same" suite.

---

## 10. Package layout, configuration, testing

```
engine/                     (native venv; not containerized — see data-model §7)
  pyproject.toml, Makefile (venv/install/test/lint/migrate), README.md
  engine/
    config.py               # env → config (Postgres DSN, Redis URL, OTLP, budgets, cache)
    db/
      migrations/*.sql       # 001_version_manifests.sql, 002_eval_schema.sql
      migrate.py             # psycopg3 idempotent applier (mirrors ingest/cmd/migrate)
      repo.py                # typed CRUD: suites, cases, runs, run_versions, results, manifests
    datasets/{base,taubench,hotpotqa,bfcl}.py
    scoring/{base,deterministic}.py
    runner/{orchestrator,worker,protocol,contract,cache,cost}.py
    cli.py                   # python -m engine: migrate | suite create | run | show
  tests/                     # unit + integration
```

**Config (env):** `VIGIL_PG_DSN` (or `POSTGRES_*` → DSN), `VIGIL_REDIS_URL`, `VIGIL_OTLP_*`,
`VIGIL_EVAL_CACHE` (`off|read|read_write`), `VIGIL_EVAL_COST_BUDGET_USD`,
`VIGIL_EVAL_CONCURRENCY`, `VIGIL_EVAL_TRIALS_PER_CASE`, per-case timeout and `max_attempts`.

**Testing:** unit tests (no external services) for the scorers, adapters, budget accounting
(including the `concurrency × max per-unit cost` overshoot bound), retry-only-on-infra vs
timeout-as-scored-failure, the cache **measurement-refusal**, and the parent/worker pipe
protocol; an integration test that runs a **2-case × 2-trial** suite against `hello_agent` +
the live stack, asserting one `eval_case_results` row per `(case, trial)`, that each
`trace_id` resolves to spans in ClickHouse carrying `vigil.eval.trial`, and that a
`measurement` + cache combination is refused. Reproducible bench scripts (per the CLAUDE.md
convention) land in `bench/` in a later week.
