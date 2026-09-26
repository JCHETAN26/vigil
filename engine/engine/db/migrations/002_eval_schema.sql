-- Suites, cases, runs, per-version sub-runs, and per-case results (design doc §2.2, §2.3).
-- An eval_run is one logical measurement over one suite for one agent_id, possibly spanning
-- several agent versions (baseline vs candidate); each version is one worker subprocess,
-- tracked in eval_run_versions. Cases are materialized from the adapter at suite creation so
-- a re-run scores the identical set (reproducibility, per the CLAUDE.md benchmark convention).

CREATE TABLE IF NOT EXISTS eval_suites (
    id         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    name       text        NOT NULL UNIQUE,
    adapter    text        NOT NULL,       -- 'local' | 'tau-bench' | 'hotpotqa' | 'bfcl'
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

CREATE TABLE IF NOT EXISTS eval_runs (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    suite_id        uuid        NOT NULL REFERENCES eval_suites(id),
    agent_id        text        NOT NULL,
    mode            text        NOT NULL CHECK (mode IN ('measurement', 'development')),
    status          text        NOT NULL DEFAULT 'pending',  -- pending|running|succeeded|failed|aborted
    cost_budget_usd numeric,                                 -- NULL = unbounded
    cost_spent_usd  numeric     NOT NULL DEFAULT 0,
    concurrency     int         NOT NULL DEFAULT 4,          -- max in-flight (case, trial) units
    trials_per_case int         NOT NULL DEFAULT 1,          -- repeats per case, for variance (Week-3 bootstrap)
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
    status            text        NOT NULL,               -- ok | error | timeout
    error             text,
    attempts          int         NOT NULL DEFAULT 1,
    input_tokens      int,                                -- AGENT only (excludes user simulator)
    output_tokens     int,
    cost_usd          numeric,                            -- AGENT only
    sim_input_tokens  int,                                -- LLM user simulator (e.g. tau-bench), if any
    sim_output_tokens int,
    sim_cost_usd      numeric,
    latency_ms        int,
    output            jsonb,                              -- bounded transcript (final output + tool_calls)
    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, agent_version, case_id, trial)
);

CREATE INDEX IF NOT EXISTS idx_case_results_run     ON eval_case_results (run_id);
CREATE INDEX IF NOT EXISTS idx_case_results_version ON eval_case_results (agent_version, case_id);
CREATE INDEX IF NOT EXISTS idx_case_results_trace   ON eval_case_results (trace_id);
