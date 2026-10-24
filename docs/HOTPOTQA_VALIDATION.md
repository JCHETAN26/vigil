# HotpotQA regression-triage validation: real agent, one controlled change

This workflow checks the regression-triage path end to end on the **real** HotpotQA agent with
real model calls. The [synthetic check in REGRESSION_TRIAGE.md §7](REGRESSION_TRIAGE.md) made no
model calls. Here the two sides are the current agent and a copy that differs in exactly one
setting, run through the real orchestrator, workers, SDK instrumentation, scorers, gate, API and
offline export. The tool is [`bench/hotpotqa_triage.py`](../bench/hotpotqa_triage.py).

**Status: tooling only, no results yet.** No stage has been run: `ANTHROPIC_API_KEY` is not
configured, so no model call has been made. Only the offline `plan` has run, and nothing below
reports a measured outcome. The approved next step is `pilot` with `--budget 1.50`, under the
owner-approved $2. A paid `full` run is not approved yet. When a stage
has been run, its raw outputs live under `bench/results/hotpotqa_triage/<UTC>-<stage>/` and its
`summary.md` is the record.

## 1. The controlled change

| | baseline | candidate |
|---|---|---|
| agent module | `hotpotqa_agent.validation.top_k5` | `hotpotqa_agent.validation.top_k1` |
| documents per `search` (`top_k`) | 5 (the current default) | 1 |
| version (model `claude-haiku-4-5-20251001`, the tool's default) | `c2eda30a0214…` | `4b6ff74526af…` |
| version (model alias `claude-haiku-4-5`, for reference only) | `837445cd2d2c…` | `512191d1b2a4…` |

The model is part of the version hash, so the hash depends on the model id passed to the
variants. The tool pins the dated snapshot `claude-haiku-4-5-20251001` (see §2), so its runs
carry the first pair of hashes. The second pair is what the variants hash to under the alias.

Everything else is the same: system prompt, model, request params (`max_tokens` 1024, no
temperature set), tool name and input schema, turn limit, BM25 retriever, corpus, questions,
trials and policy. One `top_k` value controls the three places it appears
([`agents/hotpotqa_agent/hotpotqa_agent/validation/__init__.py`](../agents/hotpotqa_agent/hotpotqa_agent/validation/__init__.py)):

- **Behavior:** the agent loop calls `retriever.search(query, top_k)`.
- **What the model is told:** the tool description says "Returns the top {top_k} matching
  paragraphs".
- **Manifest:** `params.retrieval_top_k`. Neither `retrieval_top_k` nor the API's own `top_k`
  sampling parameter is sent to the model. The model request is built from the default
  agent's `PARAMS`.

The engine computes the version hash from the module's `prompts`/`tools`/`model`/`params`
(`engine/engine/runner/contract.py`, `manifest_of`), so the hash changes exactly when `top_k`
changes. The version diff then shows `params.modified = ["retrieval_top_k"]` and `tools`
changed (the description), with model and prompts unchanged. `plan` checks all of this before
any run, and `verify` checks it again against the stored manifests and `/versions/diff`.

**Why separate modules.** The engine starts one worker subprocess per `--agent` module path,
so each version runs in its own process. Each variant also keeps deep copies of its own
configuration. The two variants share no mutable object with each other or with the default
agent, even when both are imported in one process (the orchestrator imports both to hash them).
The only shared state is the cached read-only BM25 index over the same corpus.

**The default agent is unchanged.** `hotpotqa_agent.agent` keeps `TOP_K = 5`, its params and
its default model (the `claude-haiku-4-5` alias) and its version hash (`0f1be87f…` for that
alias, pinned in `tests/test_validation_variants.py`).
Its loop moved into `run_loop(...)`, which takes the configuration as arguments. `run` calls it
with the old module defaults, and a test checks that `top_k5` sends the model exactly the
default agent's requests. `top_k5` has a different hash from the default agent only because its
manifest also records `retrieval_top_k`. So it cannot be paired with the historical base100
runs (`785e9e33`, …), which belong to a different suite anyway.

**No gold leakage.** The worker passes the agent only `case.input`, the question. The answer and
gold titles stay in `expected`, which only the scorers read. A test checks that the model sees
only the system prompt, the question and retrieved corpus text. `plan.json` and every output
file contain case ids, never answers. Preflight reads gold titles only to confirm they are in
the corpus.

**Sampling.** The Messages API has no seed, and the agent does not set a temperature. Both
versions use the same defaults, and repeated trials (in `full`) measure sampling noise. The
bootstrap seed (1234) is fixed by the gate policy.

## 2. Stages (predeclared)

| stage | cases | trials | mode | units (both versions) | default soft budget | purpose |
|---|---|---|---|---|---|---|
| `smoke` | first 2 ids of `dev20` | 1 | development | 4 | $0.10 (max $0.25) | checks the plumbing; no statistical meaning |
| `pilot` | `dev20` | 1 | development | 40 | $0.60 (max $1.50) | cost sizing and consistency checks; low power, uncalibrated |
| `full` | `base100` | 3 | measurement | 600 | $6.00 (max $10) | the predeclared measurement, at the size the detector was studied at |

Fixed for every stage: concurrency **1**, LLM cache **off**, model `claude-haiku-4-5-20251001`
(override with `--model`; recorded and checked against the spans), the default gate policy
(`passed`/`TokenF1` gated, thresholds 0.05/0.03, t-CI, α 0.05, 10,000 resamples, seed 1234).
`plan.json` records the case ids and their sha256 before the run, and `verify` fails if
`plan.json` changes afterwards.

**Why the dated snapshot.** An alias such as `claude-haiku-4-5` can later point at a different
snapshot, so a re-run could call a different model under the same name. The tool therefore
defaults to `claude-haiku-4-5-20251001`. Anthropic's
[Haiku 4.5 overview](https://platform.claude.com/docs/en/models/haiku-4-5/overview) lists this
snapshot as active (legacy) at $1 input / $5 output per million tokens, which matches the shared
price table. The committed base100 baseline and A/A runs used the alias, and the default agent
still does. Its default model and hash are unchanged.

**Pricing preflight.** The engine's cost meter prices a model missing from
`ingest/internal/pricing/prices.json` at $0. With such a model, the soft budget would never
stop the run. `plan` and `run` therefore check (blocking check `model_priced`) that the
requested model and both variants' manifest models resolve in that table (dated ids resolve to
their base entry) with input and output rates above 0. If the check fails, `run` refuses before
writing or spending anything, even with `--confirm-spend`. The table used is
`VIGIL_PRICES_PATH` if set, else the shared file, as for the engine's workers. Its path, sha256,
`prices_as_of` and the rates are recorded in `plan.json` under `execution.pricing`. The check
reads only that file. It reads no `.env` and no credentials.

The cost estimate uses the committed base100×3 baseline (`785e9e33`: $1.99 for 300 units at
top_k 5) and is rough: full ≈ $4.0, pilot ≈ $0.27, smoke ≈ $0.03. **`--budget` is the engine's
soft budget.** The engine checks it between units and lets in-flight work finish (at concurrency
1 the overshoot is at most one unit). It is **not a billing cap**. A run that hits it is
`aborted`, and the gate then reports `incomplete`.

## 3. Prerequisites (manager)

`run` checks each of these and refuses, before writing or spending anything, if one is missing:

1. **Free disk ≥ 1 GiB** on the repo and results filesystems. This is stricter than the engine's
   1 GB floor, and the tool never lets `VIGIL_MIN_FREE_DISK_GB` lower the engine's floor below it.
2. **The pinned corpus**, built with `python bench/build_hotpotqa_corpus.py --n 500`. Its pins
   are HF revision `14f0ace3…`, source sha256 `c20b638c…`, 500 cases, 4,913 docs, corpus sha256
   `8672c9c6…`, and the dev20 and base100 id files by sha256. The sha256 of `cases.v1.json` is
   recorded but not pinned, because its `verification` note depends on whether the CMU host was
   reachable at build time.
3. **A priced model:** the `model_priced` check in §2 passes.
4. **`ANTHROPIC_API_KEY` and the stack credentials** in the environment (`set -a; . ./.env; set
   +a`). Only presence is checked. No value is read into the plan, logged, or printed.
5. **The stack is up and migrated:** Postgres, ClickHouse and the OTLP receiver, checked with
   local TCP probes (`--skip-stack-probe` turns these off).
6. **A clean, committed tree.** The engine records only the commit, and the version hash does not
   cover source code. Earlier `bench/results/hotpotqa_triage/` outputs are ignored by this check.
   `--allow-dirty` overrides it, and the dirty paths are recorded in `plan.json`.
7. **`--confirm-spend`.**
8. *(Optional, for the API agreement check)* the read-only API on a loopback address
   (`make dev`, or `cd engine && .venv/bin/python -m engine.api`).

The engine venv already has `hotpotqa_agent` installed in editable mode, so the new `validation`
subpackage needs no reinstall.

## 4. Commands

Run these from the repo root with the engine venv:

```sh
PY="env -u PYTHONPATH engine/.venv/bin/python"

# Offline plan + preflight: no network, DB or model calls; prints every command `run` will execute.
$PY bench/hotpotqa_triage.py plan --stage smoke            # add --json for the full plan

# With .env loaded and the stack up:
set -a; . ./.env; set +a
$PY bench/hotpotqa_triage.py run --stage smoke --confirm-spend --api-url http://127.0.0.1:8080
#   inspect bench/results/hotpotqa_triage/<UTC>-smoke/summary.md, then:
$PY bench/hotpotqa_triage.py run --stage pilot --budget 1.50 --confirm-spend --api-url http://127.0.0.1:8080
$PY bench/hotpotqa_triage.py run --stage full  --confirm-spend --api-url http://127.0.0.1:8080

# Re-run the post-run checks later (e.g. after ClickHouse has flushed, or with the API up):
$PY bench/hotpotqa_triage.py verify bench/results/hotpotqa_triage/<dir> --api-url http://127.0.0.1:8080
# Offline re-check of the verdict (no DB):
$PY bench/regression_verdict.py reproduce bench/results/hotpotqa_triage/<dir>/bundle
```

`run` executes these steps in order. Each step's command and output go to a numbered log in the
result directory.

1. Create the suite `hotpotqa-triage-<stage>-<hash of cases file + ids>` with `engine suite
   create`. An existing suite with that name is reused only if its case ids equal the predeclared
   ids. Otherwise the run stops before any spend.
2. `engine run --agent …top_k5 --agent …top_k1 --mode … --trials … --concurrency 1 --cache off
   --budget … --notes "hotpotqa-triage <stage> <dir>"`. This is one run holding both versions.
   If the process crashes after creating the run, the run is found again by its unique notes and
   still goes through the gate.
3. `engine regress` on that run with explicit `--baseline-version`/`--candidate-version`,
   `--save <dir>/bundle --exit-code`.
4. `bench/regression_verdict.py reproduce <dir>/bundle`.
5. `verify`, described below.

## 5. Outputs and checks

The result directory is `bench/results/hotpotqa_triage/<UTC>-<stage>/`. It is new for every run,
and creation fails if it already exists.

| file | content |
|---|---|
| `plan.json` (+ `.sha256`) | stage, predeclared ids/trials/policy, variants with full manifests and hashes, data provenance (hashes, revision), git HEAD + dirty paths, preflight results, commands |
| `steps.jsonl`, `0N_*.log` | each step's time, exit code, and full output |
| `run.json` | run id and exit codes of run/regress/reproduce |
| `bundle/` | the `engine regress --save` evidence bundle |
| `units_{baseline,candidate}.json`, `table_*.md` | raw per-unit rows (stored scores, tokens, cost, latency) and the `hotpotqa_baseline.py` question-level bootstrap tables per version; written once, and later `verify` runs only confirm they still match the DB |
| `verify-<UTC>.json` | every check with its details and per-version stats (one file per verify) |
| `summary.json`, `summary.md` | gate verdict, metric table, stats, checks, reproduce command, dashboard URL, limits |

`verify` fails (exit 1) if any blocking check fails. **The verdict itself is never a check**, so
`regression`, `no_regression` and `incomplete` are all reported as computed. The checks:

- **plan_unchanged:** `plan.json` still matches its sha256 recorded before the run.
- **run_versions_match_plan, manifest_*_matches_plan:** the run holds exactly the two planned
  hashes, and the stored manifests equal the planned ones.
- **run_settings_match_plan, run_git_sha_matches_plan:** mode, trials, concurrency 1, cache off,
  and the commit. `run_status` (succeeded vs aborted) is reported as a warning.
- **\*_units_complete, \*_has_ok_units:** cases × trials units per version, with their statuses.
- **\*_nonzero_tokens_cost:** every OK unit has input tokens, output tokens and cost above 0. With
  the cache off, a zero means something is wrong.
- **\*_retrieval_k:** from the persisted transcripts, no search returned more than that version's
  top_k, and some returned exactly top_k.
- **\*_traces_in_clickhouse, \*_span_retrieval_k, \*_span_model, \*_span_tokens_nonzero:** from
  ClickHouse, every OK unit's trace exists, `vigil.retrieval.k` ≤ top_k, the spans' request model
  is the planned one, and span tokens and cost are above 0. Span totals also include retried
  attempts, so they are shown next to the Postgres totals rather than required to equal them.
  Right after a run, the writer may still be flushing. Re-run `verify`.
- **bundle_reproduces, cli_exit_matches_bundle:** the offline reproduction passed, and the CLI
  exit code (0/2/3) agrees with `verdict.json`.
- **api_matches_cli, api_version_diff** (when `--api-url` is given): `GET /compare` returns the
  same verdict, paired cases, and per-metric estimate/bound/p/status. `/versions/diff` shows only
  `params.retrieval_top_k` and `tools` changed.

Dashboard: `/compare?baseline=<run>&baseline_version=<b>&candidate=<run>&candidate_version=<c>`.
`summary.md` prints the full URL.

## 6. Reading the result honestly

- The workflow does not predict a verdict (`predeclared.expected_verdict` is `null`). Fewer
  retrieved documents per search could plausibly hurt multi-hop accuracy, but the agent can also
  compensate by searching more often. Only the measurement shows which happens.
- `smoke` and `pilot` results are **not calibrated statistical claims**. With 2 or 20 questions
  and one trial, the gate's CI is wide, and a pilot verdict says little about top_k. They exist
  to confirm the path and size the cost.
- `full` (base100 × 3) matches the size at which the detector was characterized: A/A in
  `bench/results/regression/aa-20261001/`, and the power study on `785e9e33` (about 80% power for
  a 9.5-point pass-rate drop). Even so, it is one run of one change on one dataset and one model.
- `incomplete` is not a pass. Typical causes are a soft-budget stop, failed or timed-out trials,
  or a version that did not finish.

## 7. Known limitations and a proposed engine change

- **Dispatch order.** The orchestrator queues every baseline unit before any candidate unit
  (`engine/engine/runner/orchestrator.py`: `for v in versions for c in cases for t in
  range(trials)`). At concurrency 1 the baseline therefore runs completely first, so:
  - the two versions run at different times;
  - a soft-budget stop cuts only the candidate, and the gate reports that as `incomplete`, never
    as a pass. The preflight warns when the budget is below 1.5× the estimate.

  **Proposed small change (not made here; outside this change's scope):** interleave the queue
  as `for c in cases for t in range(trials) for v in versions`, and add a scheduler/orchestrator
  test that the dispatch order alternates versions. This does not change any result's content,
  only the order units are dispatched in. It affects every multi-version run, so it needs the
  owner's approval.
- **The version hash covers prompts/tools/model/params, not code.** The commit is recorded
  separately, and `run` refuses a dirty tree by default.
- **Cost estimate.** It comes from one prior run at top_k 5 and is only a sizing aid.
- **Report tables** use the scores stored at run time, the same ones the gate compares. Use
  `bench/hotpotqa_baseline.py --run <id>` to re-score from transcripts. Note that it aggregates
  every version in a run together, so it is not suitable for this two-version run.
- **Trace flush lag.** ClickHouse ingestion is asynchronous. `verify` can be re-run, and each run
  writes a new `verify-<UTC>.json`.
