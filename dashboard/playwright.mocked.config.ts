import { defineConfig, devices } from "@playwright/test";

// Route-mocked UI correctness tests (e2e/mocked). Every API response is a hand-written fixture
// served by page.route; nothing here touches the live API, ClickHouse, or Postgres.
//
// The dashboard is started fresh on a dedicated localhost port (default 3317, distinct from the
// dev server's 3000; override with VIGIL_MOCK_PORT) with its own build dir, and an already-running
// server on that port is NOT reused — Playwright fails instead, so these tests never silently hit
// some other app.
// NEXT_PUBLIC_API_URL points at an `.invalid` host (RFC 2606, never resolves): a request a test
// forgot to mock fails loudly rather than reaching a real API.
//
// Browser: no bundle is downloaded. Uses PLAYWRIGHT_CHROME_PATH if set (e.g. /usr/bin/google-chrome),
// else the installed Google Chrome channel.
//   npm run e2e:mocked
const port = Number(process.env.VIGIL_MOCK_PORT ?? 3317);
const MOCK_API = "http://vigil-api.invalid"; // keep in sync with e2e/mocked/fixtures.ts
const chromePath = process.env.PLAYWRIGHT_CHROME_PATH;

export default defineConfig({
  testDir: "./e2e/mocked",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  retries: 0,
  workers: Number(process.env.PLAYWRIGHT_WORKERS ?? 1),
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chrome-mocked",
      use: {
        ...devices["Desktop Chrome"],
        ...(chromePath ? { launchOptions: { executablePath: chromePath } } : { channel: "chrome" }),
      },
    },
  ],
  webServer: {
    command: `npx next dev -H 127.0.0.1 -p ${port}`,
    url: `http://127.0.0.1:${port}/compare`,
    reuseExistingServer: false,
    timeout: 180_000,
    env: {
      NEXT_PUBLIC_API_URL: MOCK_API,
      NEXT_DIST_DIR: ".next-mocked",
      NEXT_TELEMETRY_DISABLED: "1",
      NODE_OPTIONS: "--max-old-space-size=1536",
    },
  },
});
