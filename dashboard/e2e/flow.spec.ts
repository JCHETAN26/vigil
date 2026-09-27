import { expect, test } from "@playwright/test";

// End-to-end happy path: runs list → a run → a trace timeline, plus a keyboard interaction on
// the waterfall. Runs against the live dashboard + API (real HotpotQA/τ² data) via forwarded
// ports; see playwright.config.ts.
test("runs → run → trace timeline", async ({ page }) => {
  await page.goto("/runs");
  await expect(page.getByRole("heading", { name: "Runs" })).toBeVisible();

  // Open the first run (its suite name links to the detail page).
  const firstRunLink = page.locator("tbody tr").first().getByRole("link").first();
  await expect(firstRunLink).toBeVisible();
  await firstRunLink.click();

  // Run detail shows the cases table; follow the first available trace timeline.
  await expect(page.getByRole("heading", { name: "Cases" })).toBeVisible();
  const timelineLink = page.getByRole("link", { name: "timeline" }).first();
  await expect(timelineLink).toBeVisible();
  await timelineLink.click();

  // Trace timeline renders: heading, the accessible SVG group, and the legend.
  await expect(page.getByRole("heading", { name: "Trace timeline" })).toBeVisible();
  const timeline = page.getByRole("group", { name: /trace span timeline/ });
  await expect(timeline).toBeVisible();
  await expect(page.getByLabel("legend")).toBeVisible();

  // Keyboard: focus the first span row, move down, and inspect — the detail panel appears.
  const firstSpan = timeline.getByRole("button").first();
  await firstSpan.focus();
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("region", { name: "selected span detail" })).toBeVisible();
});
