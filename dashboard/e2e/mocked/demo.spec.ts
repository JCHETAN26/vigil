import http from "node:http";
import type { AddressInfo } from "node:net";

import { RUN_A, RUN_B, V_BASE, expect, gotoAndSettle, json, runDetail, runSummary, runVersions, test } from "./fixtures";

// CORRECTNESS FIXTURES, not AI quality. The demo launcher's UI (NEXT_PUBLIC_VIGIL_DEMO=1, set for
// this mocked server in playwright.mocked.config.ts) must make the prepared synthetic data
// visible as such, and must not label anything else synthetic.

const SYN_SUITE = "vigil-demo-synthetic-qa";
const SYN_NOTES = "SYNTHETIC demo fixture (prepared, invented data): no agent ran and no model was called.";

test.describe("correctness fixture (not AI quality): demo labels", () => {
  test("runs list marks only vigil-demo-synthetic-* runs and shows their notes", async ({ page, mockApi }) => {
    await mockApi([
      [
        /^\/runs$/,
        (_u, route) =>
          json(route, {
            runs: [
              runSummary(RUN_A, { suite: SYN_SUITE, notes: SYN_NOTES }),
              runSummary(RUN_B, { suite: "hotpotqa-triage-pilot-27300e54a989", notes: null }),
            ],
            limit: 100,
            offset: 0,
          }),
      ],
    ]);
    await gotoAndSettle(page, "/runs");
    await expect(page.getByRole("note", { name: "demo notice" })).toContainText("no model calls");
    const synthetic = page.locator("tbody tr").filter({ hasText: SYN_SUITE });
    await expect(synthetic).toContainText("SYNTHETIC");
    await expect(synthetic).toContainText("no model was called");
    const real = page.locator("tbody tr").filter({ hasText: "hotpotqa-triage-pilot" });
    await expect(real).toHaveCount(1);
    await expect(real).not.toContainText("SYNTHETIC");
  });

  test("run detail of a synthetic run shows the label and the run notes", async ({ page, mockApi }) => {
    await mockApi([
      [/^\/runs\/[^/]+\/versions$/, (url, route) => json(route, runVersions(url, [V_BASE]))],
      [/^\/runs\/[^/]+\/cases$/, (_u, route) => json(route, { run_id: RUN_A, limit: 101, offset: 0, cases: [] })],
      [/^\/runs\/[^/]+$/, (_u, route) => json(route, runDetail(RUN_A, { suite: SYN_SUITE, notes: SYN_NOTES }))],
    ]);
    await gotoAndSettle(page, `/runs/${RUN_A}`);
    await expect(page.getByRole("heading", { level: 1 })).toContainText("SYNTHETIC");
    await expect(page.getByLabel("run notes")).toHaveText(SYN_NOTES);
  });

  test("an empty runs list points at the no-key demo", async ({ page, mockApi }) => {
    await mockApi([[/^\/runs$/, (_u, route) => json(route, { runs: [], limit: 100, offset: 0 })]]);
    await gotoAndSettle(page, "/runs");
    await expect(page.getByText(/make demo/)).toBeVisible();
  });
});

// The optional same-origin proxy (app/api/vigil/[...path]/route.ts) against a stand-in upstream
// on the loopback port the mocked server was configured with (VIGIL_API_PROXY_TARGET).
test.describe("dashboard API proxy", () => {
  const port = Number(process.env.VIGIL_MOCK_PROXY_PORT ?? 3319);

  test("forwards GETs with their query to the fixed loopback target, and nothing else", async ({ request }) => {
    const seen: string[] = [];
    const upstream = http.createServer((req, res) => {
      seen.push(`${req.method} ${req.url} cookie=${req.headers.cookie ?? ""}`);
      res.writeHead(req.url?.startsWith("/runs/missing") ? 404 : 200, { "content-type": "application/json" });
      res.end(JSON.stringify(req.url?.startsWith("/runs/missing") ? { detail: "run not found" } : { ok: true, url: req.url }));
    });
    await new Promise<void>((resolve) => upstream.listen(port, "127.0.0.1", resolve));
    expect((upstream.address() as AddressInfo).port).toBe(port);
    try {
      const ok = await request.get("/api/vigil/runs?limit=2&offset=0", { headers: { cookie: "session=x" } });
      expect(ok.status()).toBe(200);
      expect(await ok.json()).toEqual({ ok: true, url: "/runs?limit=2&offset=0" });

      const missing = await request.get("/api/vigil/runs/missing");
      expect(missing.status()).toBe(404); // the API's status and body pass through
      expect(await missing.json()).toEqual({ detail: "run not found" });

      expect((await request.get("/api/vigil/runs/%3Cscript%3E")).status()).toBe(400);
      expect((await request.post("/api/vigil/runs")).status()).toBe(405);
      expect(seen).toEqual(["GET /runs?limit=2&offset=0 cookie=", "GET /runs/missing cookie="]);
    } finally {
      await new Promise<void>((resolve) => upstream.close(() => resolve()));
    }
    // Upstream gone: a clear 502, not a hang or a fallback to anything else.
    const down = await request.get("/api/vigil/healthz");
    expect(down.status()).toBe(502);
    expect((await down.json()).detail).toContain("cannot reach the Vigil API");
  });
});
