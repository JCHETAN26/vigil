# Regression triage: from two runs to a verdict and its evidence

How to compare two agent versions on the same suite and get the same verdict from the CLI, the
dashboard API, and an offline re-check. Every command and default here comes from the source
cited next to it. The API contract is in [`engine/TRIAGE_API.md`](../engine/TRIAGE_API.md).

## 1. Pick the two sides

A side is **one run narrowed to one agent version** (`engine/engine/gate.py`, `select_version`).
A run can hold several versions, for example a baseline and a candidate run together in one
`engine run --agent A --agent B`.

- Pass the versions explicitly: `--baseline-version` / `--candidate-version` on the CLI,
  `baseline_version` / `candidate_version` in the API. Use the full hash or a unique prefix of at
  least 8 lowercase hex characters.
- If you leave a version out, it is used only when the run has exactly one version
  (`version_selection: "single_version_fallback"`). A run with several versions is refused. The
  results of different versions are never averaged.
- The same run can appear on both sides if the two versions differ. The same run with the same
  version is refused. `/compare` and the CLI also refuse runs from different suites.

Find run ids and versions with `python -m engine show --run <uuid>`, or with `GET /runs/{id}`.

## 2. Shared policy (CLI = API = dashboard)

`engine regress` and the API's `/compare` and `/compare/cases` both call `engine.gate.compare`,
so they use the same policy object (`gate.Policy`, `DEFAULT_THRESHOLDS`):

| | default |
|---|---|
| gated metrics | `passed`, `TokenF1` |
| practical threshold, `passed` | 0.05 (`--threshold-pass`, `threshold_pass`) |
| practical threshold, `TokenF1` | 0.03 (`--threshold-f1`, `threshold_f1`) |
| other metrics | informational, threshold 0 |
| CI | paired bootstrap, method `t`, one-sided alpha 0.05, `n_boot` 10000, seed 1234 |

**Failed trials** (`FAILED_TRIAL_POLICY`): an `error` or `timeout` trial counts as a failure, not
as missing data. It scores 0 on `passed` and on every higher-is-better quality metric in the
comparison. `cost_usd` and `latency_ms` use OK trials only.

**Verdict:** the verdict is `regression` if any gated metric regressed. Adverse evidence counts
even when the data is incomplete. Otherwise the verdict is `incomplete` when the comparison is
not a full paired measurement. Any of these makes it incomplete:

- a run or version that did not succeed
- missing cases or short trials
- unequal case sets
- no results, or a side with no OK trials (every trial failed)
- a gated metric that could not be compared, or that is missing on some OK trials. A missing
  gated metric is reported separately from one where failed trials scored zero

Otherwise the verdict is `no_regression`. **Incomplete never counts as a pass.** The CLI prints
`VERDICT: INCOMPLETE comparison — not a pass`, and the dashboard shows a dashed "INCOMPLETE:
this is not a pass" banner (`dashboard/components/compare/Summary.tsx`).

## 3. CLI

Run these from `engine/` with the engine venv. The Postgres DSN comes from `VIGIL_PG_DSN` or
`POSTGRES_*` (`engine/engine/config.py`).

```sh
cd engine && set -a && . ../.env && set +a
.venv/bin/python -m engine regress \
  --baseline <run-uuid> --baseline-version <hash|>=8-hex prefix> \
  --candidate <run-uuid> --candidate-version <hash|prefix> \
  --save ../evidence/<new-dir> --exit-code
```

- The output gives one line per metric: `[GATE]` or `[info]`, Δ, the 1−α bound, `p`, and the
  status. It also lists skipped metrics, any incomplete reasons, and a `VERDICT:` line.
- `--exit-code`: exit 0 = `no_regression`, 2 = `regression`, 3 = `incomplete`. Without the flag
  the CLI always exits 0.
- `--save DIR` writes a new evidence bundle (`engine/engine/bundle.py`). It refuses to write to a
  path that already exists. Files are written into a hidden `.DIR.partial-*` sibling and renamed
  into place when complete, so a failed export leaves no partial bundle at `DIR` (a killed
  process can leave the hidden temp directory behind).

## 4. Evidence bundle and offline reproduction

A bundle contains `inputs.json`, `policy.json`, `verdict.json`, `metadata.json`, and
`SHA256SUMS`:

- `inputs.json`: both side identities, the suite case list, and every raw (case, trial) row,
  including failed trials with their status and error.
- `policy.json`: the effective policy.
- `verdict.json`: the verdict as computed at export time.
- `metadata.json`: provenance. This covers the suite adapter and config, both version manifests
  with credential-like keys redacted, the engine git sha, the Python and numpy versions, and the
  argv. `dirty_tree` is recorded as "not recorded".

To verify a bundle without a database, run this from the repo root:

```sh
engine/.venv/bin/python bench/regression_verdict.py reproduce evidence/<dir>
# REPRODUCED: verdict ... | MISMATCH: ... (exit 1)
```

Verification fails on any of these:

- a missing or extra file
- a SHA-256 mismatch
- an unsupported format
- a disagreement between `verdict.json` and `policy.json`, or between `verdict.json` and the
  side identities in `inputs.json`
- any verdict field that does not recompute, including every metric value and the coverage audit

**What the checksums do *not* prove:** `SHA256SUMS` is stored inside the bundle and is not
signed. It catches accidental or partial edits. It cannot show who produced the bundle or that
its rows match the database. Someone who edits the rows, recomputes the verdict, and rewrites
`SHA256SUMS` produces a bundle that reproduces cleanly. Reproduction shows that the verdict
follows from the saved data under the saved policy. It does not authenticate where the data
came from.

## 5. Dashboard

Open `/compare?baseline=<uuid>&baseline_version=<hash>&candidate=<uuid>&candidate_version=<hash>`.
The URL holds all of the state, including policy overrides such as `threshold_f1` and the case
`metric`/`filter`/`offset` (`dashboard/lib/compareQuery.ts`). The page shows:

- **Summary:** the verdict banner, the incomplete reasons, both side identities, and per-metric
  verdicts.
- **Affected cases** (`/compare/cases`): sorted most-adverse first on the chosen metric. Filters
  are `all`, `adverse`, `unpaired`, and `failed`, 50 cases per page. Each trial row shows a
  **baseline** and a **candidate** trace link (`/traces/<trace_id>`). Failed trials show
  "no trace" and their error.
- **Version diff** (`/versions/diff`): changed or same for model, git_sha, tools, params,
  prompts, and code_ref, with added, removed, and modified keys. A missing manifest shows as
  "unknown", never as "same". The redaction note is always shown because prompt and tool text
  is not scanned for secrets.

## 6. Running the API and dashboard locally

The defaults agree with each other: the API listens on `VIGIL_API_PORT` 8080, the dashboard
client defaults to `NEXT_PUBLIC_API_URL` `http://127.0.0.1:8080`, `npm run dev` serves on
127.0.0.1:3000, and the API's default CORS origins are `http://127.0.0.1:3000` and
`http://localhost:3000` (`engine/engine/config.py`). `make dev` (repo root) starts both on these
defaults.

To run an isolated copy next to a default one, override every port together. The values below
are **example overrides**, not project defaults:

```sh
# API, bound to 127.0.0.1 only, using the read-only DB roles from .env
cd engine && set -a && . ../.env && set +a
VIGIL_API_PORT=8087 \
VIGIL_DASHBOARD_ORIGINS=http://localhost:3308,http://127.0.0.1:3308 \
  .venv/bin/python -m engine.api

# Dashboard (second shell)
cd dashboard && NEXT_PUBLIC_API_URL=http://127.0.0.1:8087 npx next dev -H 127.0.0.1 -p 3308
# open http://localhost:3308/compare?...
```

`VIGIL_DASHBOARD_ORIGINS` replaces the CORS defaults rather than adding to them. List every
origin you open the UI under. A malformed entry stops the API at startup.

## 7. Validation record: 2026-10-23 (synthetic check and final local checks)

This synthetic check exercised the path below end to end, from eval run to verdict, trace links,
and offline reproduction. It is one run on one dataset, not a proof that the pipeline is correct
in general. **It is not an AI-quality or performance result.** It uses synthetic data, it made
no paid model calls, and it does not measure latency or throughput. No artifacts from it are committed under
`bench/results/`.

**Original synthetic evaluation (one run, before the final review fixes):**

- Path: the real orchestrator → OTLP receiver → Kafka → writer → ClickHouse. The queue was
  **Apache Kafka 4.1, not the Redpanda image pinned in `deploy/docker-compose.yml`**.
- Data: 160 results, which is 40 cases × 2 agent versions × 2 trials.

**Re-query and re-export validation (after the final review fixes):** the API, CLI and real
Chrome checks were repeated on the stored results of that same run, on the final code. No new
eval runs and no model or provider calls were made for them.

- The CLI regression and the API regression agreed in both directions (regression and
  improvement).
- Version paging returned 160 stable, unique version rows.
- All 160 trace links resolved to real traces with spans.
- Invalid version selections were refused with an error.
- In real Chrome, the comparison page, the version diff and navigation to the traces worked.
- A format-2 bundle was exported from clean committed code
  (`15a5f5c97de7fd8f6316035acbd0c8ec0bc73dbc`) and reproduced offline with `reproduce`.
- A tampered copy of that bundle was rejected by `reproduce`.

An earlier version-1 bundle exported before the review fixes was interim and is not part of the
final evidence.

Final local checks after the review fixes, run with `make test-all REQUIRE_DB=1`: passed.

| Suite | Result |
|---|---|
| SDK | 34 passed |
| engine | 236 passed |
| hello_agent | 5 passed |
| hotpotqa_agent | 8 passed |
| tau2_retail_agent | 2 passed |
| bench | 83 passed |
| Go units | all passed |
| dashboard typecheck | passed |
| integration | 8 passed, 1 skipped (no HotpotQA run) |
| paid model-call suites | **not run** (no `ANTHROPIC_API_KEY`) |

Other final checks:

| Check | Result |
|---|---|
| `ruff check` (engine, tests) | passed |
| `go vet -tags=integration ./...` | passed |
| `go test -race ./...` | all passed |
| dashboard `npm run build:isolated` | passed |
| route-mocked Playwright suite (`e2e:mocked`, Chrome) | 18 passed |
| `npm audit`, production dependencies only | 0 vulnerabilities |
| `npm audit` (including dev) | 3 moderate, dev-only (see below) |
| GitHub CI (hosted) | see the [PR checks](https://github.com/JCHETAN26/vigil/pull/1/checks) and [workflow run](https://github.com/JCHETAN26/vigil/actions/runs/40064325583) for current results |

Hosted CI runs on draft [PR #1](https://github.com/JCHETAN26/vigil/pull/1). The links above
show its current status; this document does not record a final CI result. The remaining review
step is the review of that PR.

The mocked browser suite includes regression tests for three bugs fixed in review. A stale
response must not overwrite a retried request. Paging across several versions must stay
stable. The incomplete guard must hold, so an incomplete comparison is never shown as a pass.
Each test was checked by putting the bug back and confirming the test fails.

The dependency audit was run after these upgrades: Next 15.5.28, Playwright 1.58.2, a direct
`postcss` 8.5.31 dependency, `source-map-js` 1.2.2, and an `overrides` entry that pins Next's
own `postcss` to 8.5.31. The 3 remaining moderate advisories come from the Tailwind 3 build
chain. They are in dev dependencies only, which is why the production-only audit is clean.
The fix npm suggests is a Tailwind 4 migration, which is deferred and out of scope here.

Earlier real-model measurements (HotpotQA base100 A/A, τ² retail baselines) stay as they were
under `bench/results/`. This check does not replace or re-validate them.
