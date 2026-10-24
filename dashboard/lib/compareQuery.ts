// The compare page's selection lives entirely in the URL. One parser and one set of query
// builders feed /compare, /compare/cases and /versions/diff, so the three calls can never
// disagree about which runs, versions or policy they are asking for.

import type { CaseFilter } from "./types";

export const CASE_PAGE = 50; // /compare/cases accepts 1..200

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export const VERSION_RE = /^[0-9a-f]{8,64}$/;
const FILTERS: CaseFilter[] = ["all", "adverse", "unpaired", "failed"];

// Policy overrides the API accepts. Passed through verbatim; the API validates them (422).
export const POLICY_KEYS = [
  "alpha",
  "method",
  "threshold_pass",
  "threshold_f1",
  "n_boot",
  "seed",
  "two_level",
] as const;
export type PolicyKey = (typeof POLICY_KEYS)[number];

export interface Selection {
  baseline: string;
  candidate: string;
  baselineVersion: string;
  candidateVersion: string;
  policy: Partial<Record<PolicyKey, string>>;
  metric: string;
  filter: CaseFilter;
  offset: number;
}

export function parseSelection(sp: URLSearchParams): Selection {
  const policy: Partial<Record<PolicyKey, string>> = {};
  for (const k of POLICY_KEYS) {
    const v = sp.get(k);
    if (v) policy[k] = v;
  }
  const filter = sp.get("filter") as CaseFilter | null;
  const offset = Number(sp.get("offset"));
  return {
    baseline: sp.get("baseline") ?? "",
    candidate: sp.get("candidate") ?? "",
    baselineVersion: sp.get("baseline_version") ?? "",
    candidateVersion: sp.get("candidate_version") ?? "",
    policy,
    metric: sp.get("metric") ?? "",
    filter: filter && FILTERS.includes(filter) ? filter : "all",
    offset: Number.isInteger(offset) && offset > 0 ? offset : 0,
  };
}

// Problems the API would reject with a 422, caught before any request is sent.
export function selectionProblems(s: Selection): string[] {
  const out: string[] = [];
  if (s.baseline && !UUID_RE.test(s.baseline)) out.push("baseline is not a run id (UUID)");
  if (s.candidate && !UUID_RE.test(s.candidate)) out.push("candidate is not a run id (UUID)");
  if (s.baselineVersion && !VERSION_RE.test(s.baselineVersion))
    out.push("baseline version must be 8–64 lowercase hex characters");
  if (s.candidateVersion && !VERSION_RE.test(s.candidateVersion))
    out.push("candidate version must be 8–64 lowercase hex characters");
  return out;
}

// baseline/candidate + versions: the parameters every triage endpoint shares.
export function pairQuery(s: Selection): URLSearchParams {
  const q = new URLSearchParams({ baseline: s.baseline, candidate: s.candidate });
  if (s.baselineVersion) q.set("baseline_version", s.baselineVersion);
  if (s.candidateVersion) q.set("candidate_version", s.candidateVersion);
  return q;
}

// /compare: the pair plus any policy overrides.
export function summaryQuery(s: Selection): URLSearchParams {
  const q = pairQuery(s);
  for (const k of POLICY_KEYS) if (s.policy[k]) q.set(k, s.policy[k]!);
  return q;
}

// /compare/cases: exactly the /compare query plus the case view. Always bounded by CASE_PAGE.
export function casesQuery(s: Selection): URLSearchParams {
  const q = summaryQuery(s);
  if (s.metric) q.set("metric", s.metric);
  q.set("filter", s.filter);
  q.set("limit", String(CASE_PAGE));
  q.set("offset", String(s.offset));
  return q;
}

// The page URL for a selection; defaults are omitted to keep links short.
export function compareHref(s: Selection): string {
  const q = summaryQuery(s);
  if (s.metric) q.set("metric", s.metric);
  if (s.filter !== "all") q.set("filter", s.filter);
  if (s.offset) q.set("offset", String(s.offset));
  return `/compare?${q}`;
}

// The API's 400 for a multi-version run without an explicit version names the run and lists
// 12-char version prefixes ("run <id> contains N agent versions (a, b); select one …").
// Those prefixes are valid `*_version` values, so they seed the version picker.
export function versionsFromError(message: string): { runId: string; versions: string[] } | null {
  const m = /run ([0-9a-f-]{36}) contains \d+ agent versions \(([0-9a-f, ]+)\)/i.exec(message);
  if (!m) return null;
  const versions = m[2].split(",").map((v) => v.trim()).filter((v) => VERSION_RE.test(v));
  return versions.length ? { runId: m[1], versions } : null;
}
