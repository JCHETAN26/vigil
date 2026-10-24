import type { Page } from "@playwright/test";

import {
  RUN_A,
  RUN_B,
  V_BASE,
  V_CAND,
  caseDelta,
  caseSide,
  casesResult,
  compareResult,
  errorBox,
  expect,
  gotoAndSettle,
  json,
  runSide,
  runSummary,
  test,
  traceDetail,
  trial,
  triageMetric,
  versionDiff,
  type Handler,
  type Routes,
} from "./fixtures";

// CORRECTNESS FIXTURES, not AI quality: every response is hand-written to drive one UI state of
// the regression-triage journey (engine/TRIAGE_API.md). Nothing here measures an agent.

const runs = { runs: [runSummary(RUN_A), runSummary(RUN_B)], limit: 200, offset: 0 };
const BASE_TRACE = "1".repeat(32);
const CAND_TRACE = "2".repeat(32);
const PAIR = `/compare?baseline=${RUN_A}&candidate=${RUN_B}`;

// Mocks /runs and the three triage endpoints (defaults: a complete no-regression pair), recording
// every triage request as "<path>?<query>" so tests can check what the page asked for.
function triage(log: string[], h: { compare?: Handler; cases?: Handler; diff?: Handler }): Routes {
  const rec =
    (fn: Handler): Handler =>
    (url, route) => {
      log.push(`${url.pathname}?${url.searchParams}`);
      return fn(url, route);
    };
  return [
    [/^\/runs$/, (_u, route) => json(route, runs)],
    [/^\/compare$/, rec(h.compare ?? ((url, route) => json(route, compareResult(url))))],
    [/^\/compare\/cases$/, rec(h.cases ?? ((url, route) => json(route, casesResult(url, []))))],
    [/^\/versions\/diff$/, rec(h.diff ?? ((url, route) => json(route, versionDiff(url))))],
  ];
}

const paths = (log: string[]) => log.map((l) => l.split("?")[0]);
const params = (entry: string) => new URLSearchParams(entry.split("?")[1]);
const verdictBanner = (page: Page) => page.getByRole("status", { name: "comparison verdict" });

// A regression fixture: q017 got worse on the candidate, one candidate trial errored.
const REGRESSION_METRICS = [
  triageMetric("passed", {
    point_estimate: -0.2,
    ci_bound: -0.11,
    ci_excludes_zero: true,
    beyond_threshold: true,
    regressed: true,
    tail_prob: 0.001,
    status: "regression",
  }),
  triageMetric("TokenF1", { point_estimate: -0.02, ci_bound: -0.005, ci_excludes_zero: true, status: "watch" }),
  triageMetric("cost_usd"),
];
const REGRESSION_CASES = [
  caseDelta("q017", {
    delta: { passed: -1 },
    adverse_delta: 1,
    baseline: caseSide(
      [trial(0, { trace_id: BASE_TRACE }), trial(1, { trace_id: "3".repeat(32) })],
      { passed: 1 },
    ),
    candidate: caseSide(
      [
        trial(0, { status: "error", passed: null, error: "tool timeout after 30s" }),
        trial(1, { passed: false, trace_id: CAND_TRACE }),
      ],
      { passed: 0 },
    ),
  }),
  caseDelta("q003", { delta: { passed: -0.5 }, adverse_delta: 0.5 }),
  caseDelta("q010"),
  caseDelta("q099", {
    paired: false,
    delta: { passed: null },
    adverse_delta: null,
    candidate: caseSide([]),
  }),
];

function regressionRoutes(log: string[]) {
  return triage(log, {
    compare: (url, route) =>
      json(route, compareResult(url, { verdict: "regression", gated_regression: true, metrics: REGRESSION_METRICS })),
    cases: (url, route) => json(route, casesResult(url, REGRESSION_CASES, { verdict: "regression" })),
  });
}

test.describe("correctness fixture (not AI quality): compare", () => {
  test("without a selection the page asks for two runs and does not compare", async ({ page, mockApi }) => {
    const log: string[] = [];
    await mockApi(triage(log, {}));
    await gotoAndSettle(page, "/compare");
    await expect(page.getByText(/Pick a baseline and a candidate/)).toBeVisible();
    await expect(page.getByRole("button", { name: "Compare" })).toBeDisabled();
    expect(log).toEqual([]);
  });

  test("no-regression result shows the ok verdict for every metric", async ({ page, mockApi }) => {
    const log: string[] = [];
    const rows = [caseDelta("q001"), caseDelta("q002")];
    await mockApi(
      triage(log, {
        cases: (url, route) =>
          json(
            route,
            casesResult(url, url.searchParams.get("filter") === "adverse" ? [] : rows),
          ),
      }),
    );
    await gotoAndSettle(page, PAIR);
    await expect(verdictBanner(page)).toContainText("No regression on gated metrics");
    await expect(page.getByLabel("verdict: ok")).toHaveCount(3);
    await expect(page.getByLabel("verdict: REGRESSION")).toHaveCount(0);
    await expect(page.getByRole("row", { name: "case q001" })).toBeVisible();

    // The "candidate worse" view is a real empty state, requested from the API.
    await page.getByLabel("Show").selectOption("adverse");
    await expect(page).toHaveURL(/filter=adverse/);
    await expect(page.getByText("No case is worse on the candidate for this metric.")).toBeVisible();
    expect(params(log.filter((l) => l.startsWith("/compare/cases")).at(-1)!).get("filter")).toBe("adverse");
  });

  test("a failed comparison shows the API error and can be retried", async ({ page, mockApi }) => {
    let calls = 0;
    const log: string[] = [];
    await mockApi(
      triage(log, {
        compare: (_u, route) => {
          calls += 1;
          return json(route, { detail: "runs are from different suites; a paired comparison requires the same suite" }, 400);
        },
        diff: (url, route) => json(route, versionDiff(url, { same_suite: false })),
      }),
    );
    await gotoAndSettle(page, PAIR);
    const alert = errorBox(page);
    await expect(alert).toContainText("runs are from different suites");
    await expect(page.getByText("No verdict is shown because the comparison request failed.")).toBeVisible();
    await expect(verdictBanner(page)).toHaveCount(0);
    // /versions/diff accepts different suites, so the manifest diff is still available.
    await expect(page.getByText("The runs come from different suites.")).toBeVisible();
    expect(paths(log)).not.toContain("/compare/cases");
    // Dev strict mode may fire the initial effect twice, so assert Retry adds a call, not a total.
    const before = calls;
    await alert.getByRole("button", { name: "Retry" }).click();
    await expect.poll(() => calls).toBeGreaterThan(before);
  });

  test("regression: verdict, policy, adverse-sorted cases with both sides' evidence, and the version diff", async ({
    page,
    mockApi,
  }) => {
    const log: string[] = [];
    await mockApi(regressionRoutes(log));
    await gotoAndSettle(page, PAIR);

    await expect(verdictBanner(page)).toContainText("REGRESSION on gated metric: passed");
    await expect(verdictBanner(page)).not.toContainText("No regression");
    await expect(page.getByLabel("verdict: REGRESSION")).toHaveCount(1);
    await expect(page.getByLabel("verdict: watch")).toHaveCount(1);

    // Effective policy comes from the response (TokenF1 default is 0.03).
    const policyTable = page.getByRole("table", { name: "Regression thresholds" });
    await expect(policyTable.getByRole("row", { name: /TokenF1 0\.03 gated/ })).toBeVisible();
    await expect(page.getByText(/Error\/timeout trials count as failures/)).toBeVisible();

    // Worst first, unpaired last.
    const cases = page.getByRole("region", { name: "Affected cases" });
    const rows = cases.locator("tbody tr");
    await expect(rows).toHaveCount(4);
    await expect(rows.nth(0)).toHaveAccessibleName("case q017");
    await expect(rows.nth(1)).toHaveAccessibleName("case q003");
    await expect(rows.nth(3)).toHaveAccessibleName("case q099");

    const worst = page.getByRole("row", { name: "case q017" });
    await expect(worst).toContainText("▲ 1.000");
    await expect(worst).toContainText("2/2 ok"); // baseline
    await expect(worst).toContainText("1/2 ok · 1 failed"); // candidate
    await expect(worst).toContainText("error 1 · ok 1");
    await expect(worst).toContainText("tool timeout after 30s");
    await expect(worst).toContainText("no trace"); // the errored trial has no trace
    await expect(worst.getByRole("link", { name: "baseline trace, case q017 trial 0" })).toHaveAttribute(
      "href",
      `/traces/${BASE_TRACE}`,
    );
    await expect(worst.getByRole("link", { name: "candidate trace, case q017 trial 1" })).toHaveAttribute(
      "href",
      `/traces/${CAND_TRACE}`,
    );
    const unpaired = page.getByRole("row", { name: "case q099" });
    await expect(unpaired).toContainText("unpaired");
    await expect(unpaired).toContainText("no trials");

    // What changed between the versions.
    await expect(page.getByRole("note", { name: "diff summary" })).toHaveText("Manifests differ.");
    await expect(page.getByRole("row", { name: "field prompts" })).toContainText("changed");
    await expect(page.getByRole("row", { name: "field prompts" })).toContainText("modified: system");
    await expect(page.getByRole("row", { name: "field model" })).toContainText("same");
    await expect(page.getByRole("note", { name: "redaction" })).toContainText("params.api_key");
    await expect(page.getByRole("note", { name: "redaction" })).toContainText("not scanned");

    // All three calls carry the same pair; the case page is bounded.
    expect(new Set(paths(log))).toEqual(new Set(["/compare", "/compare/cases", "/versions/diff"]));
    for (const entry of log) {
      const q = params(entry);
      expect([q.get("baseline"), q.get("candidate"), q.get("baseline_version"), q.get("candidate_version")]).toEqual([
        RUN_A,
        RUN_B,
        null,
        null,
      ]);
    }
    for (const entry of log.filter((l) => l.startsWith("/compare/cases"))) {
      expect(params(entry).get("limit")).toBe("50");
    }
  });

  test("incomplete data is never shown as a pass", async ({ page, mockApi }) => {
    let variant = 0;
    const variants = [
      // The API's own incomplete verdict, with reasons and missing cases.
      {
        verdict: "incomplete",
        complete: false,
        incomplete_reasons: ["candidate run status is 'aborted', not 'succeeded'", "candidate is missing 3 suite case(s)"],
        coverage: {
          suite_cases: 40,
          paired_cases: 37,
          only_in_baseline: ["q038", "q039", "q040"],
          only_in_candidate: [],
          baseline: {
            cases: 40, trials: 80, expected_trials: 80, ok_trials: 80, failed_trials: 0, statuses: { ok: 80 },
            missing_cases: [], cases_short_of_trials: [], cases_not_in_suite: [],
          },
          candidate: {
            cases: 37, trials: 74, expected_trials: 80, ok_trials: 70, failed_trials: 4, statuses: { ok: 70, timeout: 4 },
            missing_cases: ["q038", "q039", "q040"], cases_short_of_trials: [], cases_not_in_suite: [],
          },
        },
      },
      // Inconsistent: "no_regression" but not complete.
      { verdict: "no_regression", complete: false },
      // No verdict field at all.
      { verdict: undefined },
      // Disjoint case sets: nothing paired, no metrics.
      { verdict: "incomplete", complete: false, paired_cases: 0, metrics: [], incomplete_reasons: ["no paired cases"] },
    ];
    await mockApi(
      triage([], {
        compare: (url, route) => json(route, compareResult(url, variants[variant])),
        cases: (url, route) => json(route, casesResult(url, [], { verdict: variants[variant].verdict })),
      }),
    );

    for (variant = 0; variant < variants.length; variant++) {
      await gotoAndSettle(page, `${PAIR}&seed=${variant + 1}`);
      await expect(verdictBanner(page)).toContainText("INCOMPLETE: this is not a pass");
      await expect(verdictBanner(page)).not.toContainText("No regression");
      if (variant === 0) {
        const reasons = page.getByRole("list", { name: "incomplete reasons" });
        await expect(reasons).toContainText("candidate run status is 'aborted'");
        await expect(reasons).toContainText("missing 3 suite case(s)");
        const cov = page.getByRole("group", { name: "candidate coverage" });
        await expect(cov).toContainText("74/80 trials");
        await expect(cov).toContainText("missing cases (3): q038, q039, q040");
      }
      if (variant === 2) await expect(verdictBanner(page)).toContainText("no recognised verdict");
      if (variant === 3) await expect(page.getByText("No metric could be computed for this pair.")).toBeVisible();
    }
  });

  test("mixed-version run: the API's version list drives an explicit selection used by every call", async ({
    page,
    mockApi,
  }) => {
    const log: string[] = [];
    const multi = `run ${RUN_A} contains 2 agent versions (${V_BASE.slice(0, 12)}, ${V_CAND.slice(0, 12)}); select one explicitly — results are never averaged across versions`;
    const needsVersions = (url: URL) =>
      !url.searchParams.get("baseline_version") || !url.searchParams.get("candidate_version");
    const both = { versions_in_run: [V_BASE, V_CAND], version_selection: "explicit" };
    const sides = { context: { baseline: runSide(RUN_A, V_BASE, both), candidate: runSide(RUN_A, V_CAND, both) } };
    await mockApi(
      triage(log, {
        compare: (url, route) =>
          needsVersions(url) ? json(route, { detail: multi }, 400) : json(route, compareResult(url, sides)),
        cases: (url, route) => json(route, casesResult(url, [caseDelta("q001")], sides)),
        diff: (url, route) => (needsVersions(url) ? json(route, { detail: multi }, 400) : json(route, versionDiff(url))),
      }),
    );

    await gotoAndSettle(page, `/compare?baseline=${RUN_A}&candidate=${RUN_A}`);
    await expect(errorBox(page)).toContainText("contains 2 agent versions");
    await expect(verdictBanner(page)).toHaveCount(0);
    // Nothing else is requested until the versions are chosen.
    expect(new Set(paths(log))).toEqual(new Set(["/compare"]));

    const compareBtn = page.getByRole("button", { name: "Compare" });
    await expect(compareBtn).toBeDisabled(); // same run needs two different versions
    await page.getByRole("group", { name: "Baseline versions in run" }).getByRole("button", { name: V_BASE.slice(0, 12) }).click();
    await page.getByRole("group", { name: "Candidate versions in run" }).getByRole("button", { name: V_CAND.slice(0, 12) }).click();
    await compareBtn.click();

    await expect(page).toHaveURL(new RegExp(`baseline_version=${V_BASE.slice(0, 12)}&candidate_version=${V_CAND.slice(0, 12)}`));
    await expect(verdictBanner(page)).toContainText("No regression on gated metrics");
    const base = page.getByRole("group", { name: "baseline context" });
    await expect(base).toContainText("explicitly selected");
    await expect(base).toContainText("Mixed-version run (2 versions)");

    // Unversioned requests only ever reached /compare; once chosen, all three carry both versions.
    const unversioned = log.filter((l) => !params(l).get("baseline_version"));
    const versioned = log.filter((l) => params(l).get("baseline_version"));
    expect(new Set(paths(unversioned))).toEqual(new Set(["/compare"]));
    expect(new Set(paths(versioned))).toEqual(new Set(["/compare", "/compare/cases", "/versions/diff"]));
    for (const entry of versioned) {
      expect(params(entry).get("baseline_version")).toBe(V_BASE.slice(0, 12));
      expect(params(entry).get("candidate_version")).toBe(V_CAND.slice(0, 12));
    }
  });

  test("a version missing from the run shows the 404 and no verdict, cases or diff", async ({ page, mockApi }) => {
    const log: string[] = [];
    await mockApi(
      triage(log, {
        compare: (_u, route) => json(route, { detail: `agent version deadbeef is not in run ${RUN_A}` }, 404),
      }),
    );
    await gotoAndSettle(page, `${PAIR}&baseline_version=deadbeef`);
    await expect(errorBox(page)).toContainText("agent version deadbeef is not in run");
    await expect(verdictBanner(page)).toHaveCount(0);
    await expect(page.getByRole("region", { name: "Affected cases" })).toHaveCount(0);
    expect(new Set(paths(log))).toEqual(new Set(["/compare"]));
    expect(params(log[0]).get("baseline_version")).toBe("deadbeef");
  });

  test("a malformed version in the URL is rejected before any request", async ({ page, mockApi }) => {
    const log: string[] = [];
    await mockApi(triage(log, {}));
    await gotoAndSettle(page, `${PAIR}&candidate_version=NOT-HEX`);
    await expect(errorBox(page)).toContainText("candidate version must be 8–64 lowercase hex characters");
    await expect(verdictBanner(page)).toHaveCount(0);
    expect(log).toEqual([]);
  });

  test("missing manifest and failing drill-down calls are reported, not hidden", async ({ page, mockApi }) => {
    let diffMode: "missing" | "error" = "missing";
    await mockApi(
      triage([], {
        cases: (_u, route) => json(route, { detail: "postgres unavailable" }, 503),
        diff: (url, route) =>
          diffMode === "error"
            ? json(route, { detail: "clickhouse error: timeout" }, 502)
            : json(
                route,
                versionDiff(url, {
                  identical: null,
                  missing_manifests: ["baseline"],
                  baseline_manifest: null,
                  fields: Object.fromEntries(
                    ["model", "git_sha", "tools", "params", "prompts", "code_ref"].map((f) => [f, { changed: null }]),
                  ),
                }),
              ),
      }),
    );
    await gotoAndSettle(page, PAIR);
    await expect(errorBox(page)).toContainText("postgres unavailable");
    await expect(page.getByRole("note", { name: "diff summary" })).toHaveText(
      "Cannot tell what changed: no manifest recorded for baseline.",
    );
    await expect(page.getByRole("row", { name: "field prompts" })).toContainText("unknown");
    await expect(page.getByRole("row", { name: "field prompts" })).not.toContainText("same");

    diffMode = "error";
    await gotoAndSettle(page, `${PAIR}&seed=7`);
    const diffErr = page.getByRole("region", { name: "version diff error" });
    await expect(diffErr).toContainText("clickhouse error: timeout");
    await expect(diffErr.getByRole("button", { name: "Retry" })).toBeVisible();
    await expect(page.getByRole("note", { name: "diff summary" })).toHaveCount(0);
  });

  test("affected cases page in bounded steps; changing the metric restarts at the top", async ({ page, mockApi }) => {
    const log: string[] = [];
    const rows = Array.from({ length: 120 }, (_, i) =>
      caseDelta(`case-${String(i).padStart(3, "0")}`, { adverse_delta: (120 - i) / 120 }),
    );
    await mockApi(triage(log, { cases: (url, route) => json(route, casesResult(url, rows)) }));
    await gotoAndSettle(page, PAIR);

    const region = page.getByRole("region", { name: "Affected cases" });
    const pager = page.getByRole("navigation", { name: "affected case pages" });
    await expect(region.locator("tbody tr")).toHaveCount(50);
    await expect(pager).toContainText("1–50 of 120");
    await pager.getByRole("button", { name: /Next/ }).click();
    await expect(page).toHaveURL(/offset=50/);
    await expect(region.locator("tbody tr").first()).toHaveAccessibleName("case case-050");
    await pager.getByRole("button", { name: /Next/ }).click();
    await expect(pager).toContainText("101–120 of 120");
    await expect(pager.getByRole("button", { name: /Next/ })).toBeDisabled();

    await page.getByLabel("Sort metric").selectOption("TokenF1");
    await expect(page).toHaveURL(/metric=TokenF1/);
    await expect(page).not.toHaveURL(/offset=/);
    await expect(pager).toContainText("1–50 of 120");

    const caseCalls = log.filter((l) => l.startsWith("/compare/cases")).map(params);
    expect(caseCalls.every((q) => q.get("limit") === "50")).toBe(true);
    expect(new Set(caseCalls.map((q) => `${q.get("metric") ?? ""}@${q.get("offset")}`))).toEqual(
      new Set(["@0", "@50", "@100", "TokenF1@0"]),
    );
  });

  test("drill from an adverse case into the candidate trace and back", async ({ page, mockApi }) => {
    const log: string[] = [];
    await mockApi([
      [/^\/traces\/[0-9a-f]{32}$/, (url, route) => json(route, traceDetail(url.pathname.split("/").at(-1)!))],
      ...regressionRoutes(log),
    ]);
    await gotoAndSettle(page, PAIR);
    await page.getByRole("link", { name: "candidate trace, case q017 trial 1" }).click();
    await expect(page).toHaveURL(new RegExp(`/traces/${CAND_TRACE}$`));
    await expect(page.getByRole("heading", { name: "Trace timeline" })).toBeVisible();
    await expect(page.getByText("1 spans")).toBeVisible();

    await page.goBack();
    await expect(page).toHaveURL(new RegExp(`baseline=${RUN_A}&candidate=${RUN_B}`));
    await expect(verdictBanner(page)).toContainText("REGRESSION on gated metric: passed");
    await expect(page.getByRole("row", { name: "case q017" })).toBeVisible();
  });
});
