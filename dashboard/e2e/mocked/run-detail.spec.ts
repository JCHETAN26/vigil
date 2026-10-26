import {
  RUN_A,
  RUN_B,
  V_BASE,
  V_CAND,
  caseRow,
  compareResult,
  casesResult,
  errorBox,
  expect,
  gotoAndSettle,
  json,
  runDetail,
  runSide,
  runSummary,
  runVersions,
  test,
  versionDiff,
  type Handler,
  type Routes,
} from "./fixtures";

// CORRECTNESS FIXTURES, not AI quality: hand-written responses that drive UI states only.

const versionsOf = (versions: string[], over: Record<string, Record<string, unknown>> = {}): Routes[number] => [
  /^\/runs\/[^/]+\/versions$/,
  (url, route) => json(route, runVersions(url, versions, over)),
];

test.describe("correctness fixture (not AI quality): run detail and trace", () => {
  // Run detail used to fetch a single page of 200 rows and silently drop the rest.
  test("run detail paginates case rows beyond one page", async ({ page, mockApi }) => {
    const total = 230;
    const all = Array.from({ length: total }, (_, i) => caseRow(i));
    const seen: string[] = [];
    await mockApi([
      versionsOf([V_BASE]),
      [/^\/runs\/[^/]+\/cases$/, (url, route) => {
        const limit = Number(url.searchParams.get("limit"));
        const offset = Number(url.searchParams.get("offset"));
        seen.push(`${limit}@${offset}`);
        return json(route, { run_id: RUN_A, limit, offset, cases: all.slice(offset, offset + limit) });
      }],
      [/^\/runs\/[^/]+$/, (_u, route) => json(route, runDetail(RUN_A, { n_cases: total, n_ok: total }))],
    ]);

    await gotoAndSettle(page, `/runs/${RUN_A}`);
    const rows = page.locator("tbody tr");
    await expect(rows).toHaveCount(100);
    await expect(rows.first()).toContainText("case-000");
    const pager = page.getByRole("navigation", { name: "case pages" });
    await expect(pager).toContainText("1–100 of 230");
    await expect(pager.getByRole("button", { name: /Previous/ })).toBeDisabled();

    await pager.getByRole("button", { name: /Next/ }).click();
    await expect(page).toHaveURL(/offset=100/);
    await expect(rows.first()).toContainText("case-100");

    await pager.getByRole("button", { name: /Next/ }).click();
    await expect(rows).toHaveCount(30);
    await expect(rows.last()).toContainText("case-229");
    await expect(pager).toContainText("201–230 of 230");
    await expect(pager.getByRole("button", { name: /Next/ })).toBeDisabled();

    // Bounded requests: one extra row to detect the next page, never an unbounded fetch.
    expect(new Set(seen)).toEqual(new Set(["101@0", "101@100", "101@200"]));
  });

  // A multi-version run returns each (case_id, trial) once per agent_version. Rows used to be
  // keyed and labelled by case+trial alone, so the two versions' rows were indistinguishable.
  test("run detail tells the same case and trial apart across two versions", async ({ page, mockApi }) => {
    const cases = [
      caseRow(0, { agent_version: V_BASE, passed: true, score: 1, trace_id: "a".repeat(32) }),
      caseRow(0, { agent_version: V_CAND, passed: false, score: 0, trace_id: "b".repeat(32) }),
    ];
    await mockApi([
      versionsOf([V_BASE, V_CAND]),
      [/^\/runs\/[^/]+\/cases$/, (_u, route) => json(route, { run_id: RUN_A, limit: 101, offset: 0, cases })],
      [/^\/runs\/[^/]+$/, (_u, route) => json(route, runDetail(RUN_A, { n_cases: 2, n_ok: 2, n_versions: 2 }))],
    ]);
    const warnings: string[] = [];
    page.on("console", (m) => {
      if (m.text().includes("same key")) warnings.push(m.text());
    });

    await gotoAndSettle(page, `/runs/${RUN_A}`);
    await expect(page.getByRole("columnheader", { name: "Version" })).toBeVisible();
    const rows = page.locator("tbody tr");
    await expect(rows).toHaveCount(2);
    for (const [i, version, passed, trace] of [
      [0, V_BASE, "yes", "a"],
      [1, V_CAND, "no", "b"],
    ] as const) {
      const row = rows.nth(i);
      await expect(row).toContainText("case-000");
      await expect(row.getByTitle(version)).toHaveText(version.slice(0, 12));
      await expect(row.getByRole("cell", { name: passed, exact: true })).toBeVisible();
      await expect(row.getByRole("link", { name: "timeline" })).toHaveAttribute("href", `/traces/${trace.repeat(32)}`);
    }
    // The pooled run-level figures say so instead of reading as one version's numbers.
    await expect(page.getByText("These figures pool all 2 agent versions in this run.")).toBeVisible();
    expect(warnings).toEqual([]); // no React duplicate-key warning
  });

  test("single-version run shows no pooled-versions note", async ({ page, mockApi }) => {
    await mockApi([
      versionsOf([V_BASE]),
      [/^\/runs\/[^/]+\/cases$/, (_u, route) => json(route, { run_id: RUN_A, limit: 101, offset: 0, cases: [caseRow(0)] })],
      [/^\/runs\/[^/]+$/, (_u, route) => json(route, runDetail(RUN_A, { n_cases: 1, n_ok: 1 }))],
    ]);
    await gotoAndSettle(page, `/runs/${RUN_A}`);
    await expect(page.locator("tbody tr")).toHaveCount(1);
    await expect(page.getByText(/These figures pool/)).toHaveCount(0);
  });

  test("run detail with no case results shows an empty state", async ({ page, mockApi }) => {
    await mockApi([
      versionsOf([V_BASE]),
      [/^\/runs\/[^/]+\/cases$/, (_u, route) => json(route, { run_id: RUN_A, limit: 101, offset: 0, cases: [] })],
      [/^\/runs\/[^/]+$/, (_u, route) => json(route, runDetail(RUN_A))],
    ]);
    await gotoAndSettle(page, `/runs/${RUN_A}`);
    await expect(page.getByText("No case results for this run.")).toBeVisible();
    await expect(page.getByRole("navigation", { name: "case pages" })).toHaveCount(0);
  });

  test("run detail lists every version and opens an explicit comparison of two of them", async ({ page, mockApi }) => {
    // Three versions: two registered (one with no results yet), one seen only in results.
    const VR = "d".repeat(64);
    const triage: string[] = [];
    const rec =
      (fn: Handler): Handler =>
      (url, route) => {
        triage.push(`${url.pathname}?${url.searchParams}`);
        return fn(url, route);
      };
    const both = { versions_in_run: [V_BASE, V_CAND, VR], version_selection: "explicit" };
    const sides = (url: URL) => ({
      context: {
        baseline: runSide(RUN_A, url.searchParams.get("baseline_version")!, both),
        candidate: runSide(RUN_A, url.searchParams.get("candidate_version")!, both),
      },
    });
    await mockApi([
      versionsOf([V_BASE, V_CAND, VR], {
        [V_CAND]: { status: "pending", cases_done: 0 },
        [VR]: { registered: false, status: null, cases_total: null, cases_done: null },
      }),
      [/^\/runs\/[^/]+\/cases$/, (_u, route) => json(route, { run_id: RUN_A, limit: 101, offset: 0, cases: [caseRow(0)] })],
      [/^\/runs\/[^/]+$/, (_u, route) => json(route, runDetail(RUN_A, { n_cases: 1, n_ok: 1, n_versions: 2 }))],
      [/^\/runs$/, (_u, route) => json(route, { runs: [runSummary(RUN_A), runSummary(RUN_B)], limit: 200, offset: 0 })],
      [/^\/compare$/, rec((url, route) => json(route, compareResult(url, sides(url))))],
      [/^\/compare\/cases$/, rec((url, route) => json(route, casesResult(url, [], sides(url))))],
      [/^\/versions\/diff$/, rec((url, route) => json(route, versionDiff(url)))],
    ]);
    await gotoAndSettle(page, `/runs/${RUN_A}`);

    // Every version is listed in full, including the one with no results (n_versions counts 2).
    const panel = page.getByRole("region", { name: "Agent versions" });
    await expect(panel.getByRole("listitem")).toHaveCount(3);
    for (const v of [V_BASE, V_CAND, VR]) await expect(panel).toContainText(v);
    await expect(panel.getByRole("listitem", { name: `version ${V_CAND.slice(0, 12)}` })).toContainText("pending · 0/40 units");
    await expect(panel.getByRole("listitem", { name: `version ${VR.slice(0, 12)}` })).toContainText("results only (not registered)");
    // The cases table is unaffected by the versions list.
    await expect(page.locator("tbody tr")).toHaveCount(1);

    // No side is pre-selected, and one version cannot be both sides.
    const link = panel.getByRole("link", { name: "Compare the selected versions" });
    await expect(link).toHaveCount(0);
    await expect(panel).toContainText("Pick a baseline and a candidate version to compare them.");
    for (const r of await panel.getByRole("radio").all()) await expect(r).not.toBeChecked();
    await panel.getByLabel(`baseline: ${VR.slice(0, 12)}`).check();
    await panel.getByLabel(`candidate: ${VR.slice(0, 12)}`).check();
    await expect(panel).toContainText("Pick two different versions to compare them.");
    await expect(link).toHaveCount(0);

    // The user's choice, full hashes, same run on both sides.
    await panel.getByLabel(`candidate: ${V_BASE.slice(0, 12)}`).check();
    await expect(link).toHaveAttribute(
      "href",
      `/compare?baseline=${RUN_A}&candidate=${RUN_A}&baseline_version=${VR}&candidate_version=${V_BASE}`,
    );
    expect(triage).toEqual([]);
    await link.click();
    await expect(page.getByRole("status", { name: "comparison verdict" })).toContainText("No regression on gated metrics");
    expect(triage.length).toBeGreaterThan(0);
    for (const entry of triage) {
      const q = new URLSearchParams(entry.split("?")[1]);
      expect([q.get("baseline"), q.get("candidate"), q.get("baseline_version"), q.get("candidate_version")]).toEqual([
        RUN_A,
        RUN_A,
        VR,
        V_BASE,
      ]);
    }
  });

  test("run detail pages through many versions and keeps both picks across pages", async ({ page, mockApi }) => {
    const many = Array.from({ length: 120 }, (_, i) => i.toString(16).padStart(4, "0").repeat(16));
    const lbl = (i: number) => many[i].slice(0, 12);
    const seen: string[] = [];
    await mockApi([
      [
        /^\/runs\/[^/]+\/versions$/,
        (url, route) => {
          seen.push(`${url.searchParams.get("limit")}@${url.searchParams.get("offset")}`);
          return json(route, runVersions(url, many));
        },
      ],
      [/^\/runs\/[^/]+\/cases$/, (_u, route) => json(route, { run_id: RUN_A, limit: 101, offset: 0, cases: [caseRow(0)] })],
      [/^\/runs\/[^/]+$/, (_u, route) => json(route, runDetail(RUN_A, { n_cases: 1, n_ok: 1, n_versions: 120 }))],
    ]);
    await gotoAndSettle(page, `/runs/${RUN_A}`);
    const panel = page.getByRole("region", { name: "Agent versions" });
    const pager = page.getByRole("navigation", { name: "version pages" });
    await expect(panel.getByRole("listitem")).toHaveCount(50);
    await expect(pager).toContainText("1–50 of 120");

    await panel.getByLabel(`baseline: ${lbl(2)}`).check();
    await pager.getByRole("button", { name: /Next/ }).click();
    await expect(pager).toContainText("51–100 of 120");
    await panel.getByLabel(`candidate: ${lbl(75)}`).check();
    await pager.getByRole("button", { name: /Next/ }).click();
    await expect(pager).toContainText("101–120 of 120");
    await expect(panel.getByRole("listitem")).toHaveCount(20);

    // Both picks are full hashes held across pages, shown in full wherever the list is.
    const picked = panel.getByRole("note", { name: "selected versions" });
    await expect(picked).toContainText(many[2]);
    await expect(picked).toContainText(many[75]);
    const link = panel.getByRole("link", { name: "Compare the selected versions" });
    await expect(link).toHaveAttribute(
      "href",
      `/compare?baseline=${RUN_A}&candidate=${RUN_A}&baseline_version=${many[2]}&candidate_version=${many[75]}`,
    );
    await pager.getByRole("button", { name: /Previous/ }).click();
    await pager.getByRole("button", { name: /Previous/ }).click();
    await expect(panel.getByLabel(`baseline: ${lbl(2)}`)).toBeChecked();
    // The case list's own pager is separate and untouched.
    await expect(page).not.toHaveURL(/offset=/);
    expect(new Set(seen)).toEqual(new Set(["50@0", "50@50", "50@100"]));
  });

  test("a single-version run offers comparison with another run; a failed version list can be retried", async ({
    page,
    mockApi,
  }) => {
    let fail = true;
    await mockApi([
      [
        /^\/runs\/[^/]+\/versions$/,
        (url, route) => {
          if (fail) return json(route, { detail: "postgres unavailable" }, 503);
          return json(route, runVersions(url, [V_BASE]));
        },
      ],
      [/^\/runs\/[^/]+\/cases$/, (_u, route) => json(route, { run_id: RUN_A, limit: 101, offset: 0, cases: [caseRow(0)] })],
      [/^\/runs\/[^/]+$/, (_u, route) => json(route, runDetail(RUN_A, { n_cases: 1, n_ok: 1 }))],
    ]);
    await gotoAndSettle(page, `/runs/${RUN_A}`);
    const panel = page.getByRole("region", { name: "Agent versions" });
    await expect(errorBox(page)).toContainText("postgres unavailable");
    fail = false;
    await panel.getByRole("button", { name: "Retry" }).click();
    await expect(panel.getByRole("listitem")).toHaveCount(1);
    await expect(panel.getByRole("radio")).toHaveCount(0);
    await expect(panel.getByRole("link", { name: "Compare the selected versions" })).toHaveCount(0);
    await expect(panel.getByRole("link", { name: "Compare with another run" })).toHaveAttribute(
      "href",
      `/compare?baseline=${RUN_A}`,
    );
  });

  test("unknown run surfaces the API error with a retry", async ({ page, mockApi }) => {
    await mockApi([[/^\/runs\//, (_u, route) => json(route, { detail: "run not found" }, 404)]]);
    await gotoAndSettle(page, `/runs/${RUN_A}`);
    const alert = errorBox(page);
    await expect(alert).toContainText("run not found");
    await expect(alert.getByRole("button", { name: "Retry" })).toBeVisible();
  });

  test("missing trace evidence shows the API error, not a blank timeline", async ({ page, mockApi }) => {
    await mockApi([[/^\/traces\//, (_u, route) => json(route, { detail: "trace not found" }, 404)]]);
    await gotoAndSettle(page, `/traces/${"f".repeat(32)}`);
    await expect(page.getByRole("heading", { name: "Trace timeline" })).toBeVisible();
    await expect(errorBox(page)).toContainText("trace not found");
  });
});
