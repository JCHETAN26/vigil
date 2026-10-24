import { RUN_A, V_BASE, V_CAND, caseRow, errorBox, expect, gotoAndSettle, json, runDetail, test } from "./fixtures";

// CORRECTNESS FIXTURES, not AI quality: hand-written responses that drive UI states only.
test.describe("correctness fixture (not AI quality): run detail and trace", () => {
  // Run detail used to fetch a single page of 200 rows and silently drop the rest.
  test("run detail paginates case rows beyond one page", async ({ page, mockApi }) => {
    const total = 230;
    const all = Array.from({ length: total }, (_, i) => caseRow(i));
    const seen: string[] = [];
    await mockApi([
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
      [/^\/runs\/[^/]+\/cases$/, (_u, route) => json(route, { run_id: RUN_A, limit: 101, offset: 0, cases: [caseRow(0)] })],
      [/^\/runs\/[^/]+$/, (_u, route) => json(route, runDetail(RUN_A, { n_cases: 1, n_ok: 1 }))],
    ]);
    await gotoAndSettle(page, `/runs/${RUN_A}`);
    await expect(page.locator("tbody tr")).toHaveCount(1);
    await expect(page.getByText(/These figures pool/)).toHaveCount(0);
  });

  test("run detail with no case results shows an empty state", async ({ page, mockApi }) => {
    await mockApi([
      [/^\/runs\/[^/]+\/cases$/, (_u, route) => json(route, { run_id: RUN_A, limit: 101, offset: 0, cases: [] })],
      [/^\/runs\/[^/]+$/, (_u, route) => json(route, runDetail(RUN_A))],
    ]);
    await gotoAndSettle(page, `/runs/${RUN_A}`);
    await expect(page.getByText("No case results for this run.")).toBeVisible();
    await expect(page.getByRole("navigation", { name: "case pages" })).toHaveCount(0);
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
