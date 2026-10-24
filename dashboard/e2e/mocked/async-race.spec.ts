import {
  RUN_A,
  RUN_B,
  V_CAND,
  casesResult,
  compareResult,
  errorBox,
  expect,
  gotoAndSettle,
  json,
  runSummary,
  test,
  triageMetric,
  versionDiff,
} from "./fixtures";

// CORRECTNESS FIXTURE, not AI quality: the responses are hand-written to order two in-flight
// requests. It pins useAsync (components/States.tsx): a reload still pending when the selection
// changes must never overwrite the newer selection's result.

const runs = { runs: [runSummary(RUN_A), runSummary(RUN_B)], limit: 200, offset: 0 };
const PAIR = `/compare?baseline=${RUN_A}&candidate=${RUN_B}`;

test.describe("correctness fixture (not AI quality): async request ordering", () => {
  test("a Retry still in flight when the selection changes cannot replace the newer result", async ({
    page,
    mockApi,
  }) => {
    // The first selection (no versions) fails until Retry; the Retry's response is held until the
    // test releases it. The second selection (explicit candidate version) answers at once.
    let retried = false;
    let release!: () => void;
    const released = new Promise<void>((r) => (release = r));
    let markHeld!: () => void;
    const held = new Promise<void>((r) => (markHeld = r));

    await mockApi([
      [/^\/runs$/, (_u, route) => json(route, runs)],
      [
        /^\/compare$/,
        async (url, route) => {
          if (url.searchParams.get("candidate_version")) return json(route, compareResult(url));
          if (!retried) return json(route, { detail: "transient failure" }, 503);
          markHeld();
          await released;
          // A stale regression verdict: had it landed, the page would have to show it or hang.
          return json(
            route,
            compareResult(url, {
              verdict: "regression",
              gated_regression: true,
              metrics: [triageMetric("passed", { regressed: true, status: "regression" })],
            }),
          );
        },
      ],
      [/^\/compare\/cases$/, (url, route) => json(route, casesResult(url, []))],
      [/^\/versions\/diff$/, (url, route) => json(route, versionDiff(url))],
    ]);

    await gotoAndSettle(page, PAIR);
    await expect(errorBox(page)).toContainText("transient failure");

    retried = true;
    await errorBox(page).getByRole("button", { name: "Retry" }).click();
    await held;
    await expect(page.getByText("Loading comparison…")).toBeVisible();

    // Change the selection while the Retry is pending; the new pair resolves first.
    await page.getByLabel("Candidate agent version").fill(V_CAND.slice(0, 8));
    await page.getByRole("button", { name: "Compare" }).click();
    await expect(page).toHaveURL(/candidate_version=/);
    const banner = page.getByRole("status", { name: "comparison verdict" });
    await expect(banner).toContainText("No regression on gated metrics");

    // Now let the stale Retry response arrive, and give the page time to process it.
    const staleDone = page.waitForEvent(
      "requestfinished",
      (r) => r.url().includes("/compare?") && !r.url().includes("candidate_version"),
    );
    release();
    await (await staleDone).response();
    await page.waitForTimeout(500);

    // The newer selection's result stays; the page is neither stuck loading nor showing the stale verdict.
    await expect(page.getByText("Loading comparison…")).toHaveCount(0);
    await expect(banner).toContainText("No regression on gated metrics");
    await expect(banner).not.toContainText("REGRESSION");
    await expect(errorBox(page)).toHaveCount(0);
  });
});
