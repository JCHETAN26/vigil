import { expect, test as base, type Page, type Route } from "@playwright/test";

// CORRECTNESS FIXTURES, not measured data. Every response below is hand-written to exercise a UI
// state; none of it comes from a real eval run.

export const MOCK_API = "http://vigil-api.invalid";

export type Handler = (url: URL, route: Route) => Promise<void> | void;
export type Routes = [RegExp, Handler][];

export function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

// Serve the mock API from `handlers` (keyed by pathname regex, first match wins). A request with
// no handler is aborted AND recorded; the fixture fails the test if any occurred, so a page that
// starts calling a new endpoint can't pass by accident.
export const test = base.extend<{ mockApi: (handlers: Routes) => Promise<void> }>({
  mockApi: async ({ page }, use) => {
    const unmocked: string[] = [];
    await use(async (handlers) => {
      await page.route(`${MOCK_API}/**`, async (route) => {
        const url = new URL(route.request().url());
        const h = handlers.find(([re]) => re.test(url.pathname));
        if (!h) {
          unmocked.push(`${route.request().method()} ${url.pathname}${url.search}`);
          return route.abort();
        }
        return h[1](url, route);
      });
    });
    expect(unmocked, "requests with no mock handler").toEqual([]);
  },
});
export { expect };

export const RUN_A = "00000000-0000-4000-8000-00000000000a";
export const RUN_B = "00000000-0000-4000-8000-00000000000b";

export function runSummary(id: string, over: Record<string, unknown> = {}) {
  return {
    id,
    suite: "fixture-suite",
    agent_id: "fixture-agent",
    mode: "replay",
    trials_per_case: 1,
    status: "completed",
    cost_usd: 0.01,
    created_at: "2026-10-01T00:00:00Z",
    ...over,
  };
}

export function runDetail(id: string, over: Record<string, unknown> = {}) {
  return {
    ...runSummary(id),
    n_cases: 0,
    n_ok: 0,
    pass_rate: null,
    mean_cost_usd: null,
    mean_latency_ms: null,
    n_versions: 1,
    ...over,
  };
}

export function caseRow(i: number, over: Record<string, unknown> = {}) {
  return {
    agent_version: V_BASE,
    case_id: `case-${String(i).padStart(3, "0")}`,
    trial: 0,
    status: "ok",
    passed: i % 2 === 0,
    score: i % 2 === 0 ? 1 : 0,
    scores: null,
    cost_usd: 0.001,
    input_tokens: 10,
    output_tokens: 5,
    latency_ms: 100,
    trace_id: `${String(i).padStart(32, "0")}`,
    tags: null,
    ...over,
  };
}

// --- Regression triage fixtures (contract: engine/TRIAGE_API.md) -----------------------------
// Shapes follow the contract field-for-field; the numbers are arbitrary, chosen to exercise UI
// states, and say nothing about any agent's quality.

export const V_BASE = "a1".repeat(32); // 64-hex agent versions
export const V_CAND = "b2".repeat(32);

// GET /runs/{id}/versions for `versions` (registered + succeeded unless overridden per hash),
// paged by the request's limit/offset like the API.
export function runVersions(
  url: URL,
  versions: string[],
  over: Record<string, Record<string, unknown>> = {},
) {
  const limit = Number(url.searchParams.get("limit") ?? 50);
  const offset = Number(url.searchParams.get("offset") ?? 0);
  const all = [...versions].sort().map((v) => ({
    agent_version: v,
    registered: true,
    status: "succeeded",
    cases_total: 40,
    cases_done: 40,
    ...over[v],
  }));
  return {
    run_id: url.pathname.split("/")[2],
    versions: all.slice(offset, offset + limit),
    total: all.length,
    limit,
    offset,
  };
}

// Resolve a `*_version` query value the way the API does: a unique prefix of a known version.
export function resolveVersion(param: string | null, fallback: string) {
  if (!param) return fallback;
  return [V_BASE, V_CAND].find((v) => v.startsWith(param)) ?? param;
}

export function runSide(runId: string, version: string, over: Record<string, unknown> = {}) {
  return {
    run_id: runId,
    agent_version: version,
    agent_id: "fixture-agent",
    suite_id: "11111111-1111-4111-8111-111111111111",
    suite_name: "fixture-suite",
    run_status: "succeeded",
    version_status: "succeeded",
    mode: "replay",
    trials_per_case: 2,
    cache_mode: "off",
    git_sha: "c0ffee".padEnd(40, "0"),
    versions_in_run: [version],
    version_selection: "single_version_fallback",
    ...over,
  };
}

export function policy(over: Record<string, unknown> = {}) {
  return {
    alpha: 0.05,
    method: "t",
    n_boot: 10000,
    seed: 1234,
    two_level: false,
    thresholds: { passed: 0.05, TokenF1: 0.03 },
    gated: ["passed", "TokenF1"],
    failed_trials: "failed_trials_score_zero_on_quality_metrics",
    default_threshold: 0,
    ...over,
  };
}

function sideCoverage(over: Record<string, unknown> = {}) {
  return {
    cases: 40,
    trials: 80,
    expected_trials: 80,
    ok_trials: 80,
    failed_trials: 0,
    statuses: { ok: 80 },
    missing_cases: [],
    cases_short_of_trials: [],
    cases_not_in_suite: [],
    ...over,
  };
}

export function coverage(over: Record<string, unknown> = {}) {
  return {
    suite_cases: 40,
    paired_cases: 40,
    only_in_baseline: [],
    only_in_candidate: [],
    baseline: sideCoverage(),
    candidate: sideCoverage(),
    ...over,
  };
}

export function triageMetric(name: string, over: Record<string, unknown> = {}) {
  return {
    metric: name,
    gated: name === "passed" || name === "TokenF1",
    threshold: name === "passed" ? 0.05 : name === "TokenF1" ? 0.03 : 0,
    point_estimate: 0,
    ci_bound: -0.01,
    ci_excludes_zero: false,
    beyond_threshold: false,
    regressed: false,
    direction: name === "cost_usd" || name === "latency_ms" ? "lower_is_better" : "higher_is_better",
    tail_prob: 0.5,
    n_questions: 40,
    status: "ok",
    ...over,
  };
}

// The fields /compare and /compare/cases share, echoing the request's pair.
export function triageSummary(url: URL, over: Record<string, unknown> = {}) {
  const b = url.searchParams.get("baseline")!;
  const c = url.searchParams.get("candidate")!;
  const bv = resolveVersion(url.searchParams.get("baseline_version"), V_BASE);
  const cv = resolveVersion(url.searchParams.get("candidate_version"), V_CAND);
  return {
    baseline: b,
    candidate: c,
    baseline_version: bv,
    candidate_version: cv,
    verdict: "no_regression",
    complete: true,
    incomplete_reasons: [],
    context: { baseline: runSide(b, bv), candidate: runSide(c, cv) },
    policy: policy(),
    coverage: coverage(),
    ...over,
  };
}

export function compareResult(url: URL, over: Record<string, unknown> = {}) {
  return {
    method: "t",
    alpha: 0.05,
    paired_cases: 40,
    gated_regression: false,
    metrics: [triageMetric("passed"), triageMetric("TokenF1"), triageMetric("cost_usd")],
    skipped_metrics: [],
    ...triageSummary(url),
    ...over,
  };
}

type Trial = { trial: number; status: string; passed: boolean | null; trace_id: string | null; error: string | null };

export function trial(n: number, over: Partial<Trial> = {}): Trial {
  return { trial: n, status: "ok", passed: true, trace_id: null, error: null, ...over };
}

export function caseSide(trials: Trial[], mean: Record<string, number | null> = {}) {
  const statuses: Record<string, number> = {};
  for (const t of trials) statuses[t.status] = (statuses[t.status] ?? 0) + 1;
  const nOk = trials.filter((t) => t.status === "ok").length;
  return { n_trials: trials.length, n_ok: nOk, n_failed: trials.length - nOk, statuses, mean, trials };
}

export function caseDelta(caseId: string, over: Record<string, unknown> = {}) {
  return {
    case_id: caseId,
    paired: true,
    delta: { passed: 0 },
    adverse_delta: 0,
    baseline: caseSide([trial(0), trial(1)], { passed: 1 }),
    candidate: caseSide([trial(0), trial(1)], { passed: 1 }),
    ...over,
  };
}

// A /compare/cases response that pages `rows` honouring the request's limit/offset/filter.
export function casesResult(url: URL, rows: unknown[], over: Record<string, unknown> = {}) {
  const limit = Number(url.searchParams.get("limit") ?? 50);
  const offset = Number(url.searchParams.get("offset") ?? 0);
  return {
    ...triageSummary(url),
    sort_metric: url.searchParams.get("metric") ?? "passed",
    sort_direction: "higher_is_better",
    metrics_available: ["passed", "TokenF1", "cost_usd", "latency_ms"],
    filter: url.searchParams.get("filter") ?? "all",
    total: rows.length,
    limit,
    offset,
    cases: rows.slice(offset, offset + limit),
    ...over,
  };
}

export function versionDiff(url: URL, over: Record<string, unknown> = {}) {
  const side = (runId: string, version: string) => ({
    run_id: runId,
    agent_id: "fixture-agent",
    agent_version: version,
    run_git_sha: "c0ffee".padEnd(40, "0"),
    version_selection: "explicit",
    suite_id: "11111111-1111-4111-8111-111111111111",
  });
  const manifest = (system: string, maxTokens: number) => ({
    model: "fixture-model",
    git_sha: "c0ffee".padEnd(40, "0"),
    params: { max_tokens: maxTokens, api_key: "[redacted]" },
    prompts: { system },
    tools: [],
    code_ref: null,
    created_at: "2026-10-01T00:00:00Z",
  });
  return {
    baseline: side(url.searchParams.get("baseline")!, resolveVersion(url.searchParams.get("baseline_version"), V_BASE)),
    candidate: side(url.searchParams.get("candidate")!, resolveVersion(url.searchParams.get("candidate_version"), V_CAND)),
    same_suite: true,
    same_version: false,
    identical: false,
    missing_manifests: [],
    fields: {
      model: { changed: false },
      git_sha: { changed: false },
      tools: { changed: false },
      params: { changed: true, added: [], removed: [], modified: ["max_tokens"] },
      prompts: { changed: true, added: [], removed: [], modified: ["system"] },
      code_ref: { changed: false },
    },
    baseline_manifest: manifest("Answer briefly.", 512),
    candidate_manifest: manifest("Answer in detail.", 256),
    redaction: {
      redacted_paths: { baseline: ["params.api_key"], candidate: ["params.api_key"] },
      truncated_paths: { baseline: [], candidate: [] },
      note: "Redaction matches key names only; prompt bodies and tool descriptions are not scanned.",
    },
    ...over,
  };
}

export function traceDetail(traceId: string) {
  return {
    trace_id: traceId,
    n_spans: 1,
    truncated: false,
    spans: [
      {
        span_id: "0000000000000001",
        parent_span_id: null,
        name: "agent.run",
        kind: "internal",
        role: null,
        model: null,
        start_ms: 0,
        duration_ms: 120,
        input_tokens: null,
        output_tokens: null,
        cache_write_tokens: null,
        cache_read_tokens: null,
        cost_usd: null,
        retrieved_doc_ids: [],
        retrieval_k: null,
        events: [],
      },
    ],
  };
}

// The app's ErrorBox; Next's (empty) route announcer also has role=alert, so scope by content.
export function errorBox(page: Page) {
  return page.getByRole("alert").filter({ hasText: "Something went wrong" });
}

export async function gotoAndSettle(page: Page, path: string) {
  await page.goto(path);
  await expect(page.getByRole("status").filter({ hasText: /^Loading/ })).toHaveCount(0);
}
