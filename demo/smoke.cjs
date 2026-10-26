#!/usr/bin/env node
// Browser smoke test of a RUNNING `make demo` dashboard (demo/README.md), through its same-origin
// API proxy (/api/vigil). Real dashboard, real API, real stores: nothing is mocked or routed, and
// nothing is written to Postgres or ClickHouse (the API is read-only; this script only reads).
//
//   node demo/smoke.cjs [--base-url http://127.0.0.1:3318] [--chrome /path/to/chrome]
//                       [--artifacts DIR] [--python engine/.venv/bin/python]
//
// What it expects is derived offline from the fixture and the production gate
// (`demo/seed.py links --json`): scenario run ids, version hashes, verdicts, changed manifest
// fields, the first plotted metric's sign, the affected case's trace and its retrieval. It checks
// the SYNTHETIC examples only; the real HotpotQA pilot is not required (its frozen evidence is
// verified offline by `make demo-evidence`).
//
// Browser: the installed Google Chrome channel, or --chrome / PLAYWRIGHT_CHROME_PATH. Nothing is
// downloaded. Uses no database credentials and no model keys.
//
// Output: one JSON report on stdout; exit 0 only if every check passed. With --artifacts DIR, a
// NEW directory DIR/demo-smoke-<UTC timestamp> receives report.json and full-page screenshots
// (an existing one is refused, never overwritten). Screenshots show the synthetic examples only:
// on the runs list every row that is not a synthetic run is masked.
"use strict";

const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { parseArgs } = require("node:util");

const ROOT = path.resolve(__dirname, "..");
const DEFAULT_BASE_URL = "http://127.0.0.1:3318";
const PROXY = "/api/vigil";
const LOOPBACK = new Set(["127.0.0.1", "localhost", "[::1]"]);
// Verdict banner text per gate verdict (dashboard/components/compare/Summary.tsx).
const BANNER = {
  regression: "REGRESSION on gated",
  no_regression: "No regression on gated metrics",
  incomplete: "INCOMPLETE: this is not a pass",
};

class SmokeError extends Error {}

// An explicit loopback http origin, nothing else: no credentials, path, query or fragment.
function parseBaseUrl(raw) {
  let u;
  try {
    u = new URL(raw);
  } catch {
    // Not echoed: a malformed value may still carry credentials.
    throw new SmokeError(`--base-url is not a valid URL; expected an origin such as ${DEFAULT_BASE_URL}`);
  }
  if (u.protocol !== "http:") throw new SmokeError(`--base-url must use http:, got ${u.protocol}`);
  if (u.username || u.password) throw new SmokeError("--base-url must not contain credentials");
  if (!LOOPBACK.has(u.hostname))
    throw new SmokeError(
      `--base-url host ${u.hostname} is not loopback (127.0.0.1, localhost, [::1]); ` +
        "for a remote demo forward its port over SSH and use 127.0.0.1",
    );
  if (u.pathname !== "/" || u.search || u.hash || /[?#]/.test(raw))
    throw new SmokeError("--base-url must be an origin only (no path, query or fragment)");
  return u.origin;
}

// A fresh, timestamped directory under `parent`; an existing one is refused, never reused.
function newArtifactDir(parent, now = new Date()) {
  const stamp = now.toISOString().replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z");
  fs.mkdirSync(parent, { recursive: true });
  const dir = path.join(path.resolve(parent), `demo-smoke-${stamp}`);
  try {
    fs.mkdirSync(dir);
  } catch (e) {
    if (e.code === "EEXIST") throw new SmokeError(`artifact directory ${dir} already exists; refusing to overwrite it`);
    throw e;
  }
  return dir;
}

function options(argv) {
  const { values } = parseArgs({
    args: argv,
    options: {
      "base-url": { type: "string", default: process.env.DEMO_SMOKE_URL || DEFAULT_BASE_URL },
      chrome: { type: "string", default: process.env.PLAYWRIGHT_CHROME_PATH || "" },
      artifacts: { type: "string", default: "" },
      python: { type: "string", default: path.join(ROOT, "engine", ".venv", "bin", "python") },
    },
    strict: true,
    allowPositionals: false,
  });
  return {
    origin: parseBaseUrl(values["base-url"]),
    chrome: values.chrome,
    artifacts: values.artifacts,
    python: values.python,
  };
}

// The offline targets from the fixture. The child gets no credentials or keys: PATH/HOME only.
function linkPlan(python) {
  if (!fs.existsSync(python)) throw new SmokeError(`python ${python} not found: run 'make -C engine install'`);
  const env = { PATH: process.env.PATH ?? "/usr/bin:/bin", HOME: process.env.HOME ?? "/tmp" };
  const out = execFileSync(python, [path.join(ROOT, "demo", "seed.py"), "links", "--json"], {
    cwd: ROOT,
    env,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "inherit"],
  });
  const plan = JSON.parse(out);
  if (plan.synthetic !== true || plan.scenarios?.length !== 3) throw new SmokeError("unexpected `seed.py links --json` output");
  return plan;
}

function loadPlaywright() {
  try {
    return require(require.resolve("@playwright/test", { paths: [path.join(ROOT, "dashboard")] }));
  } catch {
    throw new SmokeError("@playwright/test is not installed: run 'npm ci' in dashboard/");
  }
}

async function launch(chromium, chromePath) {
  if (chromePath) {
    if (!fs.existsSync(chromePath))
      throw new SmokeError(`no browser at ${chromePath} (--chrome / PLAYWRIGHT_CHROME_PATH); nothing is downloaded`);
    return { browser: await chromium.launch({ headless: true, executablePath: chromePath }), label: chromePath };
  }
  try {
    return { browser: await chromium.launch({ headless: true, channel: "chrome" }), label: "chrome channel" };
  } catch (e) {
    throw new SmokeError(
      "could not start the installed Google Chrome (channel 'chrome'). Install Chrome or pass " +
        `--chrome /path/to/chrome (or PLAYWRIGHT_CHROME_PATH); this script downloads no browser. Cause: ${e.message.split("\n")[0]}`,
    );
  }
}

const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

async function run(opts, report) {
  const plan = linkPlan(opts.python);
  report.fixture_version = plan.fixture_version;
  const { chromium, expect } = loadPlaywright();
  const outDir = opts.artifacts ? newArtifactDir(opts.artifacts) : null;
  report.artifacts = outDir;
  const shots = [];
  const { browser, label } = await launch(chromium, opts.chrome);
  report.browser = label;
  const context = await browser.newContext({ viewport: { width: 1440, height: 1080 } });
  try {
    const page = await context.newPage();
    const problems = { page_errors: [], failed_requests: [], api_errors: [], off_origin: [] };
    page.on("pageerror", (e) => problems.page_errors.push(e.message));
    page.on("requestfailed", (r) => problems.failed_requests.push(`${r.url()} ${r.failure()?.errorText ?? ""}`));
    page.on("request", (r) => {
      const u = new URL(r.url());
      if ((u.protocol === "http:" || u.protocol === "https:") && u.origin !== opts.origin) problems.off_origin.push(r.url());
    });
    page.on("response", (r) => {
      if (r.url().startsWith(opts.origin + PROXY + "/") && r.status() >= 400) problems.api_errors.push(`${r.status()} ${r.url()}`);
    });
    const shot = async (name, extra = {}) => {
      if (!outDir) return;
      const file = path.join(outDir, `${name}.png`);
      await page.screenshot({ path: file, fullPage: true, ...extra });
      shots.push(path.basename(file));
    };
    const step = (s) => (report.step = s);

    step("proxy health");
    const health = await page.request.get(`${opts.origin}${PROXY}/healthz`, { failOnStatusCode: false, timeout: 15_000 }).catch((e) => {
      throw new SmokeError(`${opts.origin} is not answering (${e.message.split("\n")[0]}); start the demo with 'make demo' first`);
    });
    if (health.status() !== 200)
      throw new SmokeError(`${opts.origin}${PROXY}/healthz returned ${health.status()}: is this a 'make demo' dashboard (proxy mode)?`);

    step("runs list");
    await page.goto(`${opts.origin}${plan.runs_path}`);
    await expect(page.getByRole("note", { name: "demo notice" })).toContainText("no model calls", { timeout: 60_000 });
    const rows = page.locator("tbody tr");
    await expect(rows.filter({ hasText: plan.suite_name })).toHaveCount(plan.run_ids.length, { timeout: 60_000 });
    for (const id of plan.run_ids) {
      const row = rows.filter({ has: page.locator(`a[href="/runs/${id}"]`) });
      await expect(row).toHaveCount(1);
      await expect(row).toContainText("SYNTHETIC");
    }
    const others = rows.filter({ hasNotText: plan.suite_name });
    report.masked_non_synthetic_rows = await others.count();
    await shot("runs", { mask: [others] });

    report.scenarios = {};
    for (const scn of plan.scenarios) {
      step(`compare ${scn.key}`);
      const api = page.waitForResponse((r) => new URL(r.url()).pathname === `${PROXY}/compare` && r.request().method() === "GET", {
        timeout: 90_000,
      });
      await page.goto(`${opts.origin}${scn.compare_path}`);
      const data = await (await api).json();
      if (data.verdict !== scn.expected_verdict)
        throw new SmokeError(`${scn.key}: API verdict ${data.verdict}, fixture expects ${scn.expected_verdict}`);
      const plotted = (data.metrics ?? []).filter((m) => m.point_estimate != null);
      const want = scn.first_chart_metric;
      if (!want || !plotted.length || plotted[0].metric !== want.metric || Math.sign(plotted[0].point_estimate) !== Math.sign(want.point_estimate))
        throw new SmokeError(`${scn.key}: first plotted metric ${JSON.stringify(plotted[0] ?? null)} != offline ${JSON.stringify(want)}`);
      if (want.point_estimate === 0) throw new SmokeError(`${scn.key}: first metric delta is 0, so its bar side cannot be checked`);

      const verdict = page.getByRole("status", { name: "comparison verdict" });
      await expect(verdict).toContainText(BANNER[scn.expected_verdict], { timeout: 90_000 });
      for (const m of scn.regressed_gated_metrics) await expect(verdict).toContainText(m);
      if (scn.incomplete_reasons.length) {
        const list = page.getByRole("list", { name: "incomplete reasons" });
        for (const r of scn.incomplete_reasons) await expect(list).toContainText(r);
      }

      await expect(page.getByRole("note", { name: "diff summary" })).toContainText("Manifests differ.", { timeout: 60_000 });
      for (const f of scn.changed_manifest_fields) await expect(page.getByRole("row", { name: `field ${f}`, exact: true })).toContainText("changed");

      const zero = page.locator(".recharts-reference-line line");
      const bar = page.locator(".recharts-bar-rectangle path").first();
      await expect(bar).toBeVisible({ timeout: 30_000 });
      // A horizontal SVG line has zero height, which Playwright counts as hidden: check it is
      // drawn (stroked, spanning the plot) instead.
      await expect(zero).toHaveCount(1);
      await expect(zero).toHaveAttribute("stroke", /.+/);
      const z = await zero.boundingBox();
      const b = await bar.boundingBox();
      const surface = await page.locator(".recharts-surface").boundingBox();
      if (!z || !b || !surface) throw new SmokeError(`${scn.key}: chart, zero line or first bar has no box`);
      if (z.width < surface.width / 2 || z.y < surface.y || z.y > surface.y + surface.height)
        throw new SmokeError(`${scn.key}: zero line is not drawn across the chart`);
      const zy = z.y + z.height / 2;
      const negative = want.point_estimate < 0;
      // A negative delta hangs below the zero line (its top edge on it); a positive one stands on it.
      const edge = negative ? b.y : b.y + b.height;
      if (Math.abs(edge - zy) > 1.5 || b.height < 1)
        throw new SmokeError(`${scn.key}: first bar (${want.metric} ${want.point_estimate}) is not on the ${negative ? "lower" : "upper"} side of the zero line`);
      report.scenarios[scn.key] = {
        verdict: data.verdict,
        first_metric: want.metric,
        bar: negative ? "below zero" : "above zero",
        changed_fields: scn.changed_manifest_fields,
      };
      await shot(`compare-${scn.key}`);

      if (scn.key === plan.affected.scenario) {
        step("affected trace");
        const a = plan.affected;
        const link = page.getByRole("link", { name: `candidate trace, case ${a.case_id} trial 0`, exact: true });
        await expect(link).toBeVisible({ timeout: 60_000 });
        await link.click();
        await expect(page).toHaveURL(`${opts.origin}${a.trace_path}`);
        await expect(page.getByRole("heading", { name: "Trace timeline" })).toBeVisible({ timeout: 60_000 });
        await expect(page.getByRole("group", { name: /trace span timeline/ })).toBeVisible();
        const detail = page.getByRole("region", { name: "selected span detail" });
        await page.getByRole("button", { name: new RegExp(`\\(gen_ai\\.chat ${escapeRe(plan.model)}\\)`) }).last().click();
        await expect(detail).toContainText(plan.model);
        await page.getByRole("button", { name: new RegExp(`\\(${escapeRe(a.retrieval_span)}\\), start`) }).click();
        await expect(detail).toContainText(`Retrieved (top ${a.retrieved_doc_ids.length})`);
        for (const d of a.retrieved_doc_ids) await expect(detail).toContainText(d);
        report.affected_trace = { case_id: a.case_id, trace_id: a.trace_id, retrieved_top: a.retrieved_doc_ids.length };
        await shot("trace");
      }
    }

    step("run detail version picker");
    const scn = plan.scenarios.find((s) => s.key === plan.affected.scenario);
    await page.goto(`${opts.origin}${scn.run_path}`);
    const panel = page.getByRole("region", { name: "Agent versions" });
    await expect(panel.getByRole("listitem")).toHaveCount(2, { timeout: 60_000 });
    const item = (v) => panel.getByRole("listitem").filter({ hasText: v });
    await item(scn.baseline_version).getByRole("radio", { name: /^baseline:/ }).check();
    await item(scn.candidate_version).getByRole("radio", { name: /^candidate:/ }).check();
    const cmpLink = panel.getByRole("link", { name: "Compare the selected versions" });
    const q = new URL(await cmpLink.getAttribute("href"), opts.origin).searchParams;
    const got = ["baseline", "baseline_version", "candidate", "candidate_version"].map((k) => q.get(k));
    const exp = [scn.run_id, scn.baseline_version, scn.run_id, scn.candidate_version];
    if (JSON.stringify(got) !== JSON.stringify(exp)) throw new SmokeError(`run detail link ${JSON.stringify(got)} != ${JSON.stringify(exp)}`);
    await shot("run-detail");
    await cmpLink.click();
    await expect(page.getByRole("status", { name: "comparison verdict" })).toContainText(BANNER[scn.expected_verdict], { timeout: 90_000 });
    report.run_detail_version_link = true;

    step("page health");
    const bad = Object.entries(problems).filter(([, v]) => v.length);
    if (bad.length) throw new SmokeError(`browser problems: ${JSON.stringify(Object.fromEntries(bad))}`);
    delete report.step;
    report.ok = true;
  } finally {
    report.screenshots = shots;
    await context.close();
    await browser.close();
  }
}

async function main(argv) {
  const report = { ok: false, synthetic_only: true, mocked: false, proxy: PROXY };
  let code = 1;
  let outDir = null;
  try {
    const opts = options(argv);
    report.base_url = opts.origin;
    await run(opts, report);
    code = 0;
  } catch (e) {
    report.error = e instanceof SmokeError ? e.message : String(e?.stack ?? e).split("\n").slice(0, 6).join("\n");
  }
  outDir = report.artifacts;
  const text = JSON.stringify(report, null, 2);
  if (outDir) fs.writeFileSync(path.join(outDir, "report.json"), text + "\n", { flag: "wx" });
  process.stdout.write(text + "\n");
  return code;
}

if (require.main === module) main(process.argv.slice(2)).then((c) => process.exit(c));

module.exports = { parseBaseUrl, newArtifactDir, options, SmokeError, DEFAULT_BASE_URL };
