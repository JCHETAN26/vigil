import { defineConfig, devices } from "@playwright/test";

// The dashboard (127.0.0.1:3000) and API (127.0.0.1:8080) run on the Linux box; run these tests
// FROM a machine with browsers installed (e.g. a Mac) against SSH-forwarded ports:
//   ssh -L 3000:127.0.0.1:3000 -L 8080:127.0.0.1:8080 <box>
//   cd dashboard && npm ci && npx playwright install chromium
//   PLAYWRIGHT_BASE_URL=http://127.0.0.1:3000 npm run e2e
// There is intentionally no `webServer` block, and no browsers are downloaded on the Linux box.
export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  expect: { timeout: 10_000 },
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL ?? "http://127.0.0.1:3000",
    trace: "on-first-retry",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
