// Boundary checks of demo/smoke.cjs that need no browser, dashboard or database:
// which base URLs it accepts and that artifact output never reuses a directory.
//   node --test demo/tests/smoke_cli.test.cjs
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { test } = require("node:test");

const { parseBaseUrl, newArtifactDir, options, SmokeError } = require("../smoke.cjs");

test("accepts loopback http origins, including alternate ports and SSH-forwarded ones", () => {
  assert.equal(parseBaseUrl("http://127.0.0.1:3318"), "http://127.0.0.1:3318");
  assert.equal(parseBaseUrl("http://localhost:4400/"), "http://localhost:4400");
  assert.equal(parseBaseUrl("http://[::1]:3318"), "http://[::1]:3318");
});

for (const [raw, why] of [
  ["http://user:pw@127.0.0.1:3318", /credentials/],
  ["http://127.0.0.1:3318/runs", /origin only/],
  ["http://127.0.0.1:3318/?x=1", /origin only/],
  ["http://127.0.0.1:3318/?", /origin only/],
  ["http://127.0.0.1:3318/#top", /origin only/],
  ["https://127.0.0.1:3318", /http:/],
  ["file:///etc/passwd", /http:/],
  ["http://10.0.0.5:3318", /not loopback/],
  ["http://127.0.0.1.example.com:3318", /not loopback/],
  ["http://0.0.0.0:3318", /not loopback/],
  ["127.0.0.1:3318", /http:|not a valid URL/],
  ["not a url", /not a valid URL/],
]) {
  test(`rejects ${raw}`, () => {
    assert.throws(() => parseBaseUrl(raw), (e) => e instanceof SmokeError && why.test(e.message));
  });
}

test("a malformed URL is refused without echoing it (it may carry credentials)", () => {
  const raw = "http://admin:s3cret-token@127.0.0.1:bad-port";
  assert.throws(
    () => parseBaseUrl(raw),
    (e) => e instanceof SmokeError && /not a valid URL/.test(e.message) && !e.message.includes("s3cret-token") && !e.message.includes(raw),
  );
});

test("defaults to the make-demo dashboard port and rejects unknown flags", () => {
  const saved = process.env.DEMO_SMOKE_URL;
  delete process.env.DEMO_SMOKE_URL;
  try {
    assert.equal(options([]).origin, "http://127.0.0.1:3318");
    assert.throws(() => options(["--base-url", "http://127.0.0.1:3318/x"]), SmokeError);
    assert.throws(() => options(["--headed"]));
  } finally {
    if (saved !== undefined) process.env.DEMO_SMOKE_URL = saved;
  }
});

test("artifact directories are new and timestamped; a collision is refused, not overwritten", () => {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), "vigil-smoke-"));
  try {
    const now = new Date("2026-10-26T06:30:00.123Z");
    const dir = newArtifactDir(parent, now);
    assert.equal(path.basename(dir), "demo-smoke-20261026T063000Z");
    fs.writeFileSync(path.join(dir, "report.json"), "keep");
    assert.throws(() => newArtifactDir(parent, now), (e) => e instanceof SmokeError && /already exists/.test(e.message));
    assert.equal(fs.readFileSync(path.join(dir, "report.json"), "utf8"), "keep");
  } finally {
    fs.rmSync(parent, { recursive: true, force: true });
  }
});
