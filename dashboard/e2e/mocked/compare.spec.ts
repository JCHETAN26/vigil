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
  runVersions,
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

// Default version lists: RUN_A holds V_BASE, RUN_B holds V_CAND (one each).
const singleVersions: Handler = (url, route) =>
  json(route, runVersions(url, [url.pathname.includes(RUN_A) ? V_BASE : V_CAND]));

// Mocks /runs, each run's version list, and the three triage endpoints (defaults: a complete
// no-regression pair), recording every triage request as "<path>?<query>" so tests can check
// what the page asked for. Version-list requests are not triage requests and are not recorded.
function triage(
  log: string[],
  h: { compare?: Handler; cases?: Handler; diff?: Handler; versions?: Handler },
): Routes {
  const rec =
    (fn: Handler): Handler =>
    (url, route) => {
      log.push(`${url.pathname}?${url.searchParams}`);
      return fn(url, route);
    };
  return [
    [/^\/runs$/, (_u, route) => json(route, runs)],
    [/^\/runs\/[^/]+\/versions$/, h.versions ?? singleVersions],
    [/^\/compare$/, rec(h.compare ?? ((url, route) => json(route, compareResult(url))))],
    [/^\/compare\/cases$/, rec(h.cases ?? ((url, route) => json(route, casesResult(url, []))))],
    [/^\/versions\/diff$/, rec(h.diff ?? ((url, route) => json(route, versionDiff(url))))],
  ];
}

// 120 distinct versions (more than two pages of 50); version i starts with i in 4 hex digits.
const MANY = Array.from({ length: 120 }, (_, i) => i.toString(16).padStart(4, "0").repeat(16));
const label = (i: number) => MANY[i].slice(0, 12);

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

  // The metrics chart is real rendering, not a placeholder: under Recharts 2.12 it stayed an
  // empty 220px box, because the App Router renders with React 19 (Next's vendored React) and
  // 2.12 identified its <Bar>/<XAxis> children with react-is 16, which does not know React 19
  // elements (and relied on element-time defaultProps). Recharts 2.15 supports React 19. Bars
  // must exist, one per metric with a point estimate, on the right side of the zero line.
  // The bars are deltas, so the Y domain must always include 0: with only negative (or only
  // positive) deltas an auto domain such as [-0.9, -0.3] hid the zero line and started every
  // bar at -0.3, misstating its length.
  const CHART_CASES: { name: string; deltas: [string, number][] }[] = [
    { name: "mixed signs", deltas: [["passed", -0.2], ["TokenF1", -0.05], ["ExactMatch", 0.1]] },
    { name: "all negative", deltas: [["passed", -0.416], ["TokenF1", -0.332], ["AllGoldRetrieved", -0.6]] },
    { name: "all positive", deltas: [["passed", 0.3], ["TokenF1", 0.6], ["ExactMatch", 0.45]] },
  ];
  for (const { name, deltas } of CHART_CASES) {
    test(`the metrics chart draws each delta from a visible zero line (${name})`, async ({ page, mockApi }) => {
      const metrics = deltas.map(([m, d]) => triageMetric(m, { point_estimate: d }));
      await mockApi(
        triage([], { compare: (url, route) => json(route, compareResult(url, { verdict: "regression", metrics })) }),
      );
      await gotoAndSettle(page, PAIR);
      const chart = page.locator(".recharts-responsive-container");
      await expect(chart.locator("svg.recharts-surface")).toBeVisible();
      const bars = chart.locator(".recharts-bar-rectangle path");
      await expect(bars).toHaveCount(deltas.length);
      for (const [m] of deltas) {
        await expect(chart.locator(".recharts-cartesian-axis-tick-value", { hasText: m })).toHaveCount(1);
      }
      // The zero line is drawn and lies inside the plot area.
      const zeroLine = chart.locator(".recharts-reference-line line");
      await expect(zeroLine).toHaveCount(1);
      const zero = (await zeroLine.boundingBox())!;
      const plot = (await chart.locator(".recharts-cartesian-grid").boundingBox())!;
      const zeroY = zero.y + zero.height / 2;
      expect(zero.width).toBeGreaterThan(0);
      expect(zeroY).toBeGreaterThanOrEqual(plot.y - 1);
      expect(zeroY).toBeLessThanOrEqual(plot.y + plot.height + 1);
      // Each bar starts at zero, on its sign's side, with a length proportional to |delta|.
      const boxes = (await Promise.all(deltas.map((_, i) => bars.nth(i).boundingBox()))).map((b) => b!);
      const pxPerUnit = boxes[0].height / Math.abs(deltas[0][1]);
      deltas.forEach(([m, d], i) => {
        const b = boxes[i];
        expect(b.height, m).toBeGreaterThan(0);
        if (d < 0) expect(Math.abs(b.y - zeroY), `${m} hangs from zero`).toBeLessThanOrEqual(1.5);
        else expect(Math.abs(b.y + b.height - zeroY), `${m} rises from zero`).toBeLessThanOrEqual(1.5);
        expect(b.height / pxPerUnit, `${m} length`).toBeCloseTo(Math.abs(d), 1);
      });
    });
  }

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

  test("mixed-version run vs another run: the API's refusal and the run's versions drive an explicit selection used by every call", async ({
    page,
    mockApi,
  }) => {
    const log: string[] = [];
    const multi = `run ${RUN_A} contains 2 agent versions (${V_BASE.slice(0, 12)}, ${V_CAND.slice(0, 12)}); select one explicitly — results are never averaged across versions`;
    const unversioned = (url: URL) => !url.searchParams.get("candidate_version");
    const both = { versions_in_run: [V_BASE, V_CAND], version_selection: "explicit" };
    const sides = (url: URL) => ({
      context: {
        baseline: runSide(RUN_B, V_BASE),
        candidate: runSide(RUN_A, url.searchParams.get("candidate_version")!, both),
      },
    });
    await mockApi(
      triage(log, {
        versions: (url, route) =>
          json(route, runVersions(url, url.pathname.includes(RUN_A) ? [V_BASE, V_CAND] : [V_BASE])),
        compare: (url, route) =>
          unversioned(url) ? json(route, { detail: multi }, 400) : json(route, compareResult(url, sides(url))),
        cases: (url, route) => json(route, casesResult(url, [caseDelta("q001")], sides(url))),
        diff: (url, route) => (unversioned(url) ? json(route, { detail: multi }, 400) : json(route, versionDiff(url))),
      }),
    );

    // A deep link that predates version selection: different runs, no versions.
    await gotoAndSettle(page, `/compare?baseline=${RUN_B}&candidate=${RUN_A}`);
    await expect(errorBox(page)).toContainText("contains 2 agent versions");
    await expect(verdictBanner(page)).toHaveCount(0);
    // Nothing else is requested until the versions are chosen.
    expect(new Set(paths(log))).toEqual(new Set(["/compare"]));

    // The 400's prefixes and the run's full version list collapse into one choice per version.
    const cand = page.getByRole("group", { name: "Candidate versions in run" });
    await expect(cand.getByRole("button")).toHaveCount(2);
    await cand.getByRole("button", { name: V_CAND.slice(0, 12) }).click();
    await page.getByRole("button", { name: "Compare" }).click();

    await expect(page).toHaveURL(new RegExp(`candidate_version=${V_CAND}`));
    await expect(verdictBanner(page)).toContainText("No regression on gated metrics");
    const candCtx = page.getByRole("group", { name: "candidate context" });
    await expect(candCtx).toContainText("explicitly selected");
    await expect(candCtx).toContainText("Mixed-version run (2 versions)");

    // Unversioned requests only ever reached /compare; once chosen, all three carry the full hash.
    const versioned = log.filter((l) => params(l).get("candidate_version"));
    expect(new Set(paths(log.filter((l) => !params(l).get("candidate_version"))))).toEqual(new Set(["/compare"]));
    expect(new Set(paths(versioned))).toEqual(new Set(["/compare", "/compare/cases", "/versions/diff"]));
    for (const entry of versioned) expect(params(entry).get("candidate_version")).toBe(V_CAND);
  });

  test("same run from a fresh page: picking it lists its versions, and the user picks each side", async ({
    page,
    mockApi,
  }) => {
    const log: string[] = [];
    const versionCalls: string[] = [];
    // Three versions; two share their first 12 characters.
    const VX = "abcdef012345" + "0".repeat(52);
    const VY = "abcdef012345" + "1".repeat(52);
    const three = { versions_in_run: [V_BASE, VX, VY].sort(), version_selection: "explicit" };
    const sides = (url: URL) => ({
      context: {
        baseline: runSide(RUN_A, url.searchParams.get("baseline_version")!, three),
        candidate: runSide(RUN_A, url.searchParams.get("candidate_version")!, three),
      },
    });
    await mockApi(
      triage(log, {
        versions: (url, route) => {
          versionCalls.push(url.pathname + url.search);
          return json(route, runVersions(url, url.pathname.includes(RUN_A) ? [VY, V_BASE, VX] : [V_CAND]));
        },
        compare: (url, route) => json(route, compareResult(url, sides(url))),
        cases: (url, route) => json(route, casesResult(url, [], sides(url))),
      }),
    );

    await gotoAndSettle(page, "/compare");
    await page.getByLabel("Baseline run").selectOption(RUN_A);
    await page.getByLabel("Candidate run").selectOption(RUN_A);
    const compareBtn = page.getByRole("button", { name: "Compare" });
    await expect(page.getByText("Baseline and candidate are the same run: select two different agent versions.")).toBeVisible();
    await expect(compareBtn).toBeDisabled();

    for (const side of ["Baseline", "Candidate"]) {
      const group = page.getByRole("group", { name: `${side} versions in run` });
      await expect(group).toContainText("Versions in this run (3)");
      // Full hashes, listed by hash and labelled long enough to tell VX and VY apart.
      await expect(group.getByRole("button")).toHaveText([V_BASE.slice(0, 12), "abcdef0123450", "abcdef0123451"]);
      // Nothing is pre-selected: the order is not a baseline/candidate suggestion.
      for (const b of await group.getByRole("button").all()) await expect(b).toHaveAttribute("aria-pressed", "false");
    }
    // Discovery happened without any comparison request, and every list request is bounded.
    expect(log).toEqual([]);
    expect(versionCalls.length).toBeGreaterThan(0);
    for (const c of versionCalls) expect(c).toMatch(new RegExp(`^/runs/${RUN_A}/versions\\?limit=50&offset=0$`));

    // The user decides which version is the baseline (here the later hash).
    await page.getByRole("group", { name: "Baseline versions in run" }).getByRole("button", { name: "abcdef0123451" }).click();
    await page.getByRole("group", { name: "Candidate versions in run" }).getByRole("button", { name: V_BASE.slice(0, 12) }).click();
    await expect(page.getByRole("group", { name: "Baseline versions in run" }).getByRole("button", { name: "abcdef0123451" })).toHaveAttribute("aria-pressed", "true");
    await compareBtn.click();

    await expect(page).toHaveURL(new RegExp(`baseline_version=${VY}&candidate_version=${V_BASE}`));
    await expect(verdictBanner(page)).toContainText("No regression on gated metrics");
    expect(new Set(paths(log))).toEqual(new Set(["/compare", "/compare/cases", "/versions/diff"]));
    for (const entry of log) {
      const q = params(entry);
      expect([q.get("baseline"), q.get("candidate"), q.get("baseline_version"), q.get("candidate_version")]).toEqual([
        RUN_A,
        RUN_A,
        VY,
        V_BASE,
      ]);
    }
  });

  test("a same-run link without versions asks for them instead of requesting a comparison", async ({ page, mockApi }) => {
    const log: string[] = [];
    await mockApi(
      triage(log, { versions: (url, route) => json(route, runVersions(url, [V_BASE, V_CAND])) }),
    );
    await gotoAndSettle(page, `/compare?baseline=${RUN_A}&candidate=${RUN_A}`);
    await expect(page.getByText(/same run: choose which of its agent versions is the baseline/)).toBeVisible();
    await expect(errorBox(page)).toHaveCount(0);
    await expect(verdictBanner(page)).toHaveCount(0);
    await expect(page.getByRole("group", { name: "Baseline versions in run" }).getByRole("button")).toHaveCount(2);
    expect(log).toEqual([]);

    // A single-version run cannot be compared with itself; the form says so. (The newer mock
    // registration takes precedence.)
    await mockApi(triage(log, {}));
    await gotoAndSettle(page, `/compare?baseline=${RUN_A}&candidate=${RUN_A}&seed=2`);
    await expect(page.getByText(/holds a single agent version: pick a different run for one side/)).toBeVisible();
    expect(log).toEqual([]);
  });

  test("version lists answer for the run picked now, never a previous pick; a failed list can be retried", async ({
    page,
    mockApi,
  }) => {
    let release!: () => void;
    const released = new Promise<void>((r) => (release = r));
    let failNext = true;
    await mockApi(
      triage([], {
        versions: async (url, route) => {
          if (url.pathname.includes(RUN_A)) {
            await released; // RUN_A's list is slow...
            return json(route, runVersions(url, [V_BASE, "d".repeat(64)]));
          }
          if (failNext) {
            failNext = false;
            return json(route, { detail: "postgres unavailable" }, 503);
          }
          return json(route, runVersions(url, [V_CAND]));
        },
      }),
    );
    await gotoAndSettle(page, "/compare");
    const baseGroup = page.getByRole("group", { name: "Baseline versions in run" });

    // ...and is superseded by RUN_B, whose first list request fails.
    await page.getByLabel("Baseline run").selectOption(RUN_A);
    await expect(page.getByText("Loading versions…")).toBeVisible();
    await page.getByLabel("Baseline run").selectOption(RUN_B);
    const err = errorBox(page);
    await expect(err).toContainText("Could not list the agent versions of this run: postgres unavailable");
    await err.getByRole("button", { name: "Retry" }).click();
    await expect(baseGroup.getByRole("button")).toHaveText([V_CAND.slice(0, 12)]);

    // RUN_A's late answer arrives; it must not replace RUN_B's versions.
    const late = page.waitForEvent("requestfinished", (r) => r.url().includes(`/runs/${RUN_A}/versions`));
    release();
    await (await late).response();
    await page.waitForTimeout(500);
    await expect(baseGroup.getByRole("button")).toHaveText([V_CAND.slice(0, 12)]);
    await expect(page.getByRole("button", { name: V_BASE.slice(0, 12) })).toHaveCount(0);
    await expect(errorBox(page)).toHaveCount(0);
  });

  test("versions past the first page are reachable by paging, and a pick survives paging", async ({ page, mockApi }) => {
    const log: string[] = [];
    const seen: string[] = [];
    await mockApi(
      triage(log, {
        versions: (url, route) => {
          seen.push(`${url.pathname.includes(RUN_A) ? "A" : "B"}:${url.searchParams.get("limit")}@${url.searchParams.get("offset")}`);
          return json(route, runVersions(url, url.pathname.includes(RUN_A) ? MANY : [V_CAND]));
        },
      }),
    );
    await gotoAndSettle(page, "/compare");
    await page.getByLabel("Baseline run").selectOption(RUN_A);
    await page.getByLabel("Candidate run").selectOption(RUN_A);
    const group = (side: string) => page.getByRole("group", { name: `${side} versions in run` });
    const pager = (side: string) => page.getByRole("navigation", { name: `${side} version pages` });

    await expect(group("Baseline").getByRole("button")).toHaveCount(50);
    await expect(pager("Baseline")).toContainText("1–50 of 120");
    await group("Baseline").getByRole("button", { name: label(3) }).click();
    await pager("Baseline").getByRole("button", { name: /Next/ }).click();
    await expect(pager("Baseline")).toContainText("51–100 of 120");
    await expect(group("Baseline").getByRole("button", { name: label(3) })).toHaveCount(0);
    await expect(page.getByText(`Selected ${label(3)} is not on this page of the list.`)).toBeVisible();
    await expect(page.getByLabel("Baseline agent version")).toHaveValue(MANY[3]); // still picked

    // The candidate comes from the last page, never typed.
    await pager("Candidate").getByRole("button", { name: /Next/ }).click();
    await pager("Candidate").getByRole("button", { name: /Next/ }).click();
    await expect(pager("Candidate")).toContainText("101–120 of 120");
    await expect(pager("Candidate").getByRole("button", { name: /Next/ })).toBeDisabled();
    await group("Candidate").getByRole("button", { name: label(110) }).click();
    await page.getByRole("button", { name: "Compare" }).click();

    await expect(page).toHaveURL(new RegExp(`baseline_version=${MANY[3]}&candidate_version=${MANY[110]}`));
    await expect(verdictBanner(page)).toContainText("No regression on gated metrics");
    for (const entry of log) {
      expect([params(entry).get("baseline_version"), params(entry).get("candidate_version")]).toEqual([MANY[3], MANY[110]]);
    }
    // Every list request was one bounded page.
    expect(new Set(seen)).toEqual(new Set(["A:50@0", "A:50@50", "A:50@100"]));
  });

  test("a version page still loading for a previous run never replaces the current run's list", async ({
    page,
    mockApi,
  }) => {
    let release!: () => void;
    const released = new Promise<void>((r) => (release = r));
    const seen: string[] = [];
    await mockApi(
      triage([], {
        versions: async (url, route) => {
          const a = url.pathname.includes(RUN_A);
          const off = url.searchParams.get("offset");
          seen.push(`${a ? "A" : "B"}@${off}`);
          if (a && off === "50") await released; // RUN_A's second page is slow
          return json(route, runVersions(url, a ? MANY : [V_CAND]));
        },
      }),
    );
    await gotoAndSettle(page, "/compare");
    const group = page.getByRole("group", { name: "Baseline versions in run" });
    await page.getByLabel("Baseline run").selectOption(RUN_A);
    await expect(group.getByRole("button")).toHaveCount(50);
    await page.getByRole("navigation", { name: "Baseline version pages" }).getByRole("button", { name: /Next/ }).click();
    await expect(page.getByText("Loading versions…")).toBeVisible();

    // Switch runs while page 2 of RUN_A is in flight: RUN_B's list starts at its first page.
    await page.getByLabel("Baseline run").selectOption(RUN_B);
    await expect(group.getByRole("button")).toHaveText([V_CAND.slice(0, 12)]);
    expect(seen.at(-1)).toBe("B@0");

    const late = page.waitForEvent("requestfinished", (r) => r.url().includes(`/runs/${RUN_A}/versions`) && r.url().includes("offset=50"));
    release();
    await (await late).response();
    await page.waitForTimeout(500);
    await expect(group.getByRole("button")).toHaveText([V_CAND.slice(0, 12)]);
    await expect(page.getByRole("navigation", { name: "Baseline version pages" })).toHaveCount(0); // B fits one page
    await expect(page.getByText("Loading versions…")).toHaveCount(0);
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
