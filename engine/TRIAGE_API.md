# Regression triage API — contract for the dashboard

Read-only GET endpoints of `engine/engine/api/app.py`. The three comparison endpoints resolve
runs/versions and the policy through `engine/engine/gate.py` (`/runs/{id}/versions` lists the
same version set the gate selects from), the same code `python -m engine regress` uses, so the
CLI and the UI reach identical decisions for identical inputs. All Postgres reads use bound
parameters over the read-only role. Errors are FastAPI `{"detail": "<message>"}`.

## Browser access (CORS)

The API sends CORS headers only to the exact origins in `VIGIL_DASHBOARD_ORIGINS`. The value
is a comma-separated list of `scheme://host[:port]` origins, read once at startup.

| `VIGIL_DASHBOARD_ORIGINS` | allowed origins |
|---|---|
| unset or blank | `http://127.0.0.1:3000`, `http://localhost:3000` (the default) |
| `http://127.0.0.1:3307,http://localhost:3307` | exactly those two. The defaults are replaced, not extended. |

- Each entry must match the browser's `Origin` header exactly. That means http or https,
  lowercase, no path or trailing slash, no query or fragment, no `user:pass@`, and no default
  port (`:80`/`:443`). `localhost` and `127.0.0.1` are different origins, so list both if the
  UI is opened under both names.
- A malformed entry, an empty entry (`a,,b`), or a wildcard (`*`, `http://*.x`) makes the API
  fail at startup with a `ValueError` that names the bad entry. It never falls back to a
  wider policy.
- Only `GET` is allowed. Credentials are never allowed (`allow_credentials=False`, and no
  `Access-Control-Allow-Credentials` header is sent).
- An unlisted origin gets no `Access-Control-Allow-Origin` header, so the browser blocks the
  response. Its preflight returns 400. Non-browser clients (curl, the CLI) are unaffected.

Example for a UI served on port 3307 against an API on port 8087:
`VIGIL_API_PORT=8087 VIGIL_DASHBOARD_ORIGINS=http://127.0.0.1:3307 python -m engine.api`.

## Shared query parameters

| param | type / validation | default | meaning |
|---|---|---|---|
| `baseline`, `candidate` | UUID (422 if malformed) | required | run ids |
| `baseline_version`, `candidate_version` | `^[0-9a-f]{8,64}$` (422) | none | agent version hash or unique ≥8-char prefix |

**Version selection.** A run can contain several agent versions (baseline and candidate in a
single run). Results are **never averaged across versions**.
- Version omitted, run has exactly one version: that version is used (`version_selection: "single_version_fallback"`).
- Version omitted, run has several versions: **400**. The message lists the 12-char prefixes. The UI should show a version picker fed by `GET /runs/{id}/versions` (below), which lists the choices before any comparison is requested; `context.*.versions_in_run` lists them too once a request succeeds.
- Version given but not in the run: **404**. Ambiguous prefix: **400**.
- Comparing a run against itself is allowed when both versions are given and differ (`baseline=R&candidate=R&baseline_version=…&candidate_version=…`). The same run with the same version: **400**.

Status codes: 404 = run or version not found; 400 = unresolvable selection, runs from
different suites (`/compare*` only), or an unknown `metric`; 422 = parameter validation;
503 = the API's install lacks a numerical dependency the policy needs (method `t` needs SciPy).
The engine never substitutes a normal approximation for the Student-t quantile, so a missing
SciPy is an error with an install hint, never a verdict.

### Policy parameters (`/compare`, `/compare/cases`)

| param | validation | default |
|---|---|---|
| `alpha` | 0 < x < 0.5 | 0.05 |
| `method` | `t` \| `percentile` \| `bca` | `t` |
| `threshold_pass` | 0 ≤ x ≤ 1 | 0.05 |
| `threshold_f1` | 0 ≤ x ≤ 1 | **0.03** (was 0.05 in the API before; now matches the CLI) |
| `n_boot` | 1000 ≤ n ≤ 20000 | 10000 |
| `seed` | 0 ≤ n < 2^32 | 1234 |
| `two_level` | bool | false |

Gated metrics: `passed`, `TokenF1`. Every other metric is informational, with threshold 0.

**Denominators.** An `error`/`timeout` trial counts as a failure. It scores 0 on `passed` and
on every higher-is-better quality metric in the comparison, so it is never dropped. `cost_usd`
and `latency_ms` use OK trials only.

## `GET /runs/{run_id}/versions`

The agent versions a run holds, for choosing one per side. Same set the gate selects from:
every version registered for the run (`eval_run_versions`, even with no results yet) plus any
version that appears only in `eval_case_results`. Not derived from a page of `/runs/{id}/cases`.

| param | validation | default |
|---|---|---|
| `limit` | 1 ≤ n ≤ 200 (422) | 50 |
| `offset` | ≥ 0 (422) | 0 |

```jsonc
{
  "run_id": "<run uuid>",
  "versions": [{
    "agent_version": "<full 64 hex>",
    "registered": true,           // false: seen only in results; then status/cases_* are null
    "status": "succeeded",        // eval_run_versions.status
    "cases_total": 40, "cases_done": 40   // (case × trial) units
  }],
  "total": 3, "limit": 50, "offset": 0
}
```

Ordered by hash, so pages are stable. **The order is not a role**: nothing here says which
version is the baseline, and a client must not assign sides from it. 404 if the run does not
exist; an existing run with no versions returns `versions: []`, `total: 0`. When `total`
exceeds the page, the remaining versions are still selectable by hash or prefix.

`GET /runs` rows and `GET /runs/{run_id}` also carry `notes` (`eval_runs.notes`, free text or
null). The demo's seeded runs say `SYNTHETIC ...` there (`demo/synthetic_fixture.py`), and the
dashboard shows the notes with the run.

## `GET /compare`

```jsonc
{
  // unchanged from before (dashboard/lib/types.ts CompareResult)
  "baseline": "<run uuid>", "candidate": "<run uuid>",
  "method": "t", "alpha": 0.05,
  "paired_cases": 40,                // cases with ≥1 trial on both sides
  "gated_regression": true,
  "metrics": [MetricVerdict],        // only metrics that could be computed
  // new
  "verdict": "regression" | "no_regression" | "incomplete",
  "complete": false,
  "incomplete_reasons": ["candidate run status is 'aborted', not 'succeeded'", "..."],
  "baseline_version": "<64 hex>", "candidate_version": "<64 hex>",
  "context": { "baseline": RunSide, "candidate": RunSide },
  "policy": Policy,
  "coverage": Coverage,
  "skipped_metrics": [{ "metric": "TokenF1", "gated": true, "reason": "only reported by baseline" }]
}
```

- `MetricVerdict`: `metric, gated, threshold, point_estimate, ci_bound, ci_excludes_zero,
  beyond_threshold, regressed, direction ("higher_is_better"|"lower_is_better"), tail_prob,
  n_questions, status ("regression"|"watch"|"ok"), ok_trials_missing{baseline: n, candidate: n}
  (OK trials on that side without this metric), failure_zeros_only[] (sides with no OK trials,
  whose series is failure zeros)`. Ordered: passed, TokenF1, ExactMatch,
  RetrievalRecall@5, RetrievalRecall@10, NDCG, AllGoldRetrieved, cost_usd, latency_ms, then
  the rest alphabetically.
- `RunSide`: `run_id, agent_version, agent_id, suite_id, suite_name, run_status,
  version_status, mode, trials_per_case, cache_mode, git_sha, versions_in_run[],
  version_selection ("explicit"|"single_version_fallback")`.
- `Policy`: `alpha, method, n_boot, seed, two_level, thresholds{metric: number}, gated[],
  failed_trials ("failed_trials_score_zero_on_quality_metrics"), default_threshold (0)`.
- `Coverage`: `suite_cases, paired_cases, only_in_baseline[], only_in_candidate[]`, plus
  `baseline`/`candidate` objects with `cases, trials, expected_trials, ok_trials,
  failed_trials, statuses{status: n}, missing_cases[], cases_short_of_trials[],
  cases_not_in_suite[]`. The id lists are bounded by the suite's size.

**Verdict rule.** `regression` if any gated metric regressed. Adverse evidence counts even
on incomplete data. Otherwise `incomplete` if any `incomplete_reasons` exist: a run or version
that didn't succeed, missing cases or trials, unequal case sets, no results, a side with no OK
trials, a gated metric that couldn't be compared, a gated metric missing on some OK trials, or
**no gated metric compared at all** (reason `no gated metric was compared …`, e.g. OK trials
with `passed = null` and no gated score, so only cost/latency compare). Otherwise
`no_regression`. **Never render `incomplete` as a pass.**
Non-finite numbers are returned as `null`.

**Metric availability per side.** A metric is measured on a side when at least one of its OK
trials reports it; failed trials score 0 on it under the failed-trial rule but never make it
measured. A metric measured on only one side is skipped (`only reported by …`), even if the
other side's failed trials were zero-filled for it. A side with no OK trials is the exception:
its failure zeros are compared (so an all-failed candidate can still be a regression) and the
comparison is incomplete. A gated metric no side's OK trials report (e.g. TokenF1 on τ²) is
not applicable and does not by itself make the comparison incomplete, as long as another gated
metric (τ²: `passed`) was compared.

Historical bundles: a bundle saved before this rule whose data compared no gated metric
recorded `no_regression`; `bench/regression_verdict.py reproduce` now recomputes `incomplete`
and reports a MISMATCH. No engine scorer produces that shape (every scored trial sets
`passed`), so normal bundles, which always compare `passed`, are unchanged and reproduce as
before; the bundle format version is not bumped.

## `GET /compare/cases`

Same parameters as `/compare`, plus:

| param | validation | default |
|---|---|---|
| `metric` | `^[A-Za-z0-9_@.\-]{1,64}$`; must appear in `metrics_available`, else 400 (when neither side has results: 200 with `cases: []`, `metrics_available: []`) | the first gated metric present (normally `passed`) |
| `filter` | `all` \| `adverse` (adverse_delta > 0) \| `unpaired` \| `failed` (any failed trial on either side) | `all` |
| `limit` | 1..200 | 50 |
| `offset` | ≥ 0 | 0 |

```jsonc
{
  "baseline", "candidate", "baseline_version", "candidate_version",
  "verdict", "complete", "incomplete_reasons", "context", "policy", "coverage",  // identical to /compare
  "sort_metric": "passed", "sort_direction": "higher_is_better",
  "metrics_available": ["passed", "TokenF1", "cost_usd", "latency_ms"],
  "filter": "all", "total": 40, "limit": 50, "offset": 0,
  "cases": [{
    "case_id": "q017",
    "paired": true,                      // false: one side has no trials for this case
    "delta": { "<metric>": number | null },   // candidate mean − baseline mean
    "adverse_delta": 0.33,               // > 0 = candidate worse on sort_metric; null if unpaired
    "baseline": CaseSide, "candidate": CaseSide
  }]
}
```

`CaseSide`: `n_trials, n_ok, n_failed, statuses{status: n}, mean{metric: number|null}` (null
where that side never measured the metric),
`
trials: [{trial, status, passed, trace_id (32-hex | null), error (≤500 chars | null)}]`.
Link a trial with `GET /traces/{trace_id}`. Failed trials have no trace (`trace_id: null`).

Sort order: paired cases by `adverse_delta` descending, then case_id. Unpaired cases come last
(also reachable through `filter=unpaired`). `total` counts after filtering.

## `GET /versions/diff`

Shared parameters only. Uses the same version-selection rules. Unlike `/compare`, the runs
may come from different suites, and the same run+version is allowed.

```jsonc
{
  "baseline":  { "run_id", "agent_id", "agent_version", "run_git_sha", "version_selection", "suite_id" },
  "candidate": { ... },
  "same_suite": true, "same_version": false,
  "identical": false,                  // null when a manifest is missing
  "missing_manifests": [],             // "baseline" and/or "candidate"
  "fields": {
    "model":    { "changed": false },
    "git_sha":  { "changed": true },
    "tools":    { "changed": false },
    "params":   { "changed": true, "added": ["top_k"], "removed": [], "modified": ["max_tokens"] },
    "prompts":  { "changed": true, "added": [], "removed": [], "modified": ["system"] },
    "code_ref": { "changed": false }   // dict fields carry key lists when both sides are objects
  },                                   // every field is {"changed": null} if a manifest is missing
  "baseline_manifest":  { "model", "git_sha", "params", "prompts", "tools", "code_ref", "created_at" } | null,
  "candidate_manifest": { ... } | null,
  "redaction": {
    "redacted_paths":  { "baseline": ["params.api_key"], "candidate": [] },
    "truncated_paths": { "baseline": ["prompts.system"], "candidate": [] },
    "note": "<limits text>"
  }
}
```

Only the listed config fields are returned. Values under credential-like keys become
`"[redacted]"`. Strings over 20,000 chars are truncated. `changed` flags are computed on raw
values, so a change inside a redacted or truncated value still shows as changed. **Limit:**
redaction matches key names only. Prompt bodies and tool descriptions are not scanned, so
render `redaction.note`.

## Behavior changes the UI must handle

- `/compare` returns 400 for multi-version runs without an explicit version. It used to average
  the versions silently.
- The default F1 threshold is 0.03, not 0.05.
- Failed trials now lower `passed`/quality metrics. Before, they were filtered out.
- `paired_cases: 0` now returns 200 with `verdict: "incomplete"` and `metrics: []`. Before,
  disjoint case sets could produce a 500.
