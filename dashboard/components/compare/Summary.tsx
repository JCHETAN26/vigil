"use client";

import { Bar, BarChart, CartesianGrid, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { SyntheticBadge } from "@/components/Synthetic";
import { VerdictBadge, verdictOf } from "@/components/VerdictBadge";
import type { CompareResult, Coverage, Policy, RunSide, SideCoverage } from "@/lib/types";

export const fmt = (x: number | null | undefined, digits = 4) =>
  x == null || !Number.isFinite(x) ? "—" : x.toFixed(digits);

// Chart axis labels only (the table keeps fmt's four decimals): three significant digits, so a
// tick such as -0.2777777777777778 reads -0.278; exponent form for very small or large values.
export const axisTick = (x: number) => {
  if (!Number.isFinite(x)) return "";
  if (x === 0) return "0";
  const a = Math.abs(x);
  return a < 1e-3 || a >= 1e4 ? x.toExponential(1) : String(Number(x.toPrecision(3)));
};

export const shortVersion = (v: string | null | undefined) => (v ? v.slice(0, 12) : "—");

type Banner = { kind: "regression" | "pass" | "incomplete"; reasons: string[] };

// The only place a verdict is decided for display. A pass needs the API to say `no_regression`
// AND `complete` with no incomplete reasons and at least one computed gated metric; anything
// else (incomplete, unknown verdict, missing fields) is shown as INCOMPLETE, never as a pass.
// Regression wins even on incomplete data, as in the contract.
export function bannerOf(d: CompareResult): Banner {
  const reasons = Array.isArray(d.incomplete_reasons) ? [...d.incomplete_reasons] : [];
  if (d.verdict === "regression") return { kind: "regression", reasons };
  const gatedComputed = Array.isArray(d.metrics) && d.metrics.some((m) => m.gated);
  if (d.verdict === "no_regression" && d.complete === true && reasons.length === 0 && gatedComputed) {
    return { kind: "pass", reasons };
  }
  if (d.verdict !== "incomplete" && d.verdict !== "no_regression") {
    reasons.unshift("the API response carries no recognised verdict");
  } else if (d.verdict === "no_regression" && !gatedComputed) {
    reasons.unshift("no gated metric was computed");
  } else if (d.verdict === "no_regression" && d.complete !== true && reasons.length === 0) {
    reasons.unshift("the API did not mark the comparison complete");
  }
  return { kind: "incomplete", reasons };
}

export function VerdictBanner({ data }: { data: CompareResult }) {
  const b = bannerOf(data);
  const regressed = (data.metrics ?? []).filter((m) => m.gated && verdictOf(m) === "regression");
  const style = {
    regression: "border-regression bg-regression/5",
    pass: "border-ok bg-ok/5",
    incomplete: "border-watch bg-watch/5 border-dashed",
  }[b.kind];
  return (
    <section role="status" aria-label="comparison verdict" className={`p-3 rounded-md border-2 text-sm ${style}`}>
      <p className="font-semibold">
        {b.kind === "regression" &&
          `▲ REGRESSION on gated metric${regressed.length === 1 ? "" : "s"}: ${regressed.map((m) => m.metric).join(", ") || "see metrics"}`}
        {b.kind === "pass" && "✓ No regression on gated metrics"}
        {b.kind === "incomplete" && "◆ INCOMPLETE: this is not a pass. The data cannot support a no-regression verdict."}
      </p>
      <p className="opacity-70">
        {data.paired_cases ?? 0} paired case(s) · method {data.policy?.method ?? data.method} · α{" "}
        {data.policy?.alpha ?? data.alpha}
      </p>
      {b.reasons.length > 0 && (
        <div className="mt-2">
          <p className="font-semibold">
            {b.kind === "regression" ? "The data is also incomplete (adverse evidence still counts):" : "Why incomplete:"}
          </p>
          <ul className="list-disc ml-5" aria-label="incomplete reasons">
            {b.reasons.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

export function ContextPanel({
  data,
  onPickVersion,
}: {
  data: CompareResult;
  onPickVersion: (side: "baseline" | "candidate", version: string) => void;
}) {
  return (
    <section aria-labelledby="ctx-h" className="flex flex-col gap-2">
      <h2 id="ctx-h" className="text-lg font-semibold">
        What is being compared
      </h2>
      <div className="grid sm:grid-cols-2 gap-3">
        <SideCard side="baseline" s={data.context.baseline} onPickVersion={onPickVersion} />
        <SideCard side="candidate" s={data.context.candidate} onPickVersion={onPickVersion} />
      </div>
    </section>
  );
}

function SideCard({
  side,
  s,
  onPickVersion,
}: {
  side: "baseline" | "candidate";
  s: RunSide;
  onPickVersion: (side: "baseline" | "candidate", version: string) => void;
}) {
  const others = (s.versions_in_run ?? []).filter((v) => v !== s.agent_version);
  return (
    <div className="border rounded-md p-3 text-sm" aria-label={`${side} context`} role="group">
      <h3 className="font-semibold capitalize">{side}</h3>
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
        <dt className="opacity-70">Run</dt>
        <dd className="font-mono text-xs break-all">{s.run_id}</dd>
        <dt className="opacity-70">Suite</dt>
        <dd>
          {s.suite_name}
          <SyntheticBadge suite={s.suite_name} />
        </dd>
        <dt className="opacity-70">Agent</dt>
        <dd>{s.agent_id}</dd>
        <dt className="opacity-70">Version</dt>
        <dd className="font-mono text-xs" title={s.agent_version}>
          {shortVersion(s.agent_version)}{" "}
          <span className="font-sans opacity-70">
            ({s.version_selection === "explicit" ? "explicitly selected" : "only version in run"})
          </span>
        </dd>
        <dt className="opacity-70">Status</dt>
        <dd>
          run {s.run_status}
          {s.version_status ? ` · version ${s.version_status}` : ""}
        </dd>
        <dt className="opacity-70">Mode</dt>
        <dd>
          {s.mode} · {s.trials_per_case} trial(s)/case{s.cache_mode ? ` · cache ${s.cache_mode}` : ""}
        </dd>
        <dt className="opacity-70">Git</dt>
        <dd className="font-mono text-xs">{s.git_sha ? s.git_sha.slice(0, 12) : "—"}</dd>
      </dl>
      {others.length > 0 && (
        <div className="mt-2 text-xs">
          <p>
            Mixed-version run ({s.versions_in_run.length} versions). Results are never averaged across
            versions. Switch this side to:
          </p>
          <div className="flex flex-wrap gap-1 mt-1">
            {others.map((v) => (
              <button
                key={v}
                type="button"
                className="border rounded px-1 font-mono"
                onClick={() => onPickVersion(side, v)}
              >
                {shortVersion(v)}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export function PolicyPanel({ policy }: { policy: Policy }) {
  const metrics = Object.keys(policy.thresholds ?? {});
  return (
    <section aria-labelledby="policy-h" className="text-sm flex flex-col gap-2">
      <h2 id="policy-h" className="text-lg font-semibold">
        Effective policy
      </h2>
      <p>
        method <b>{policy.method}</b> · α <b>{policy.alpha}</b> · {policy.n_boot} bootstrap resamples · seed{" "}
        {policy.seed} · {policy.two_level ? "two-level (case + trial) resampling" : "case-level resampling"}
      </p>
      <p className="opacity-70">
        {policy.failed_trials === "failed_trials_score_zero_on_quality_metrics"
          ? "Error/timeout trials count as failures: they score 0 on pass rate and quality metrics; cost and latency use OK trials only."
          : `Failed-trial policy: ${policy.failed_trials}`}
      </p>
      <table className="text-sm border-collapse w-auto">
        <caption className="sr-only">Regression thresholds</caption>
        <thead>
          <tr className="text-left border-b">
            <th className="p-1 pr-4">Metric</th>
            <th className="p-1 pr-4">Threshold</th>
            <th className="p-1">Role</th>
          </tr>
        </thead>
        <tbody>
          {metrics.map((m) => (
            <tr key={m} className="border-b">
              <td className="p-1 pr-4">{m}</td>
              <td className="p-1 pr-4">{policy.thresholds[m]}</td>
              <td className="p-1">{policy.gated.includes(m) ? "gated" : "informational"}</td>
            </tr>
          ))}
          <tr>
            <td className="p-1 pr-4 opacity-70">any other metric</td>
            <td className="p-1 pr-4">{policy.default_threshold}</td>
            <td className="p-1">informational</td>
          </tr>
        </tbody>
      </table>
    </section>
  );
}

const MAX_IDS = 20;

function IdList({ label, ids }: { label: string; ids: string[] }) {
  if (!ids || ids.length === 0) return null;
  const shown = ids.slice(0, MAX_IDS);
  return (
    <p>
      <span className="opacity-70">{label} ({ids.length}): </span>
      <span className="font-mono text-xs">{shown.join(", ")}</span>
      {ids.length > shown.length && <span className="opacity-70"> … and {ids.length - shown.length} more</span>}
    </p>
  );
}

function SideCoverageCard({ side, c }: { side: string; c: SideCoverage }) {
  const statuses = Object.entries(c.statuses ?? {})
    .map(([k, n]) => `${k} ${n}`)
    .join(" · ");
  return (
    <div className="border rounded-md p-3" role="group" aria-label={`${side} coverage`}>
      <h3 className="font-semibold capitalize">{side}</h3>
      <p>
        {c.cases} case(s) · {c.trials}/{c.expected_trials} trials · {c.ok_trials} ok · {c.failed_trials} failed
      </p>
      {statuses && <p className="opacity-70">statuses: {statuses}</p>}
      <IdList label="missing cases" ids={c.missing_cases} />
      <IdList label="cases short of trials" ids={c.cases_short_of_trials} />
      <IdList label="cases not in suite" ids={c.cases_not_in_suite} />
    </div>
  );
}

export function CoveragePanel({ coverage }: { coverage: Coverage }) {
  return (
    <section aria-labelledby="cov-h" className="text-sm flex flex-col gap-2">
      <h2 id="cov-h" className="text-lg font-semibold">
        Coverage
      </h2>
      <p>
        {coverage.paired_cases} of {coverage.suite_cases} suite case(s) paired
      </p>
      <IdList label="only in baseline" ids={coverage.only_in_baseline} />
      <IdList label="only in candidate" ids={coverage.only_in_candidate} />
      <div className="grid sm:grid-cols-2 gap-3">
        <SideCoverageCard side="baseline" c={coverage.baseline} />
        <SideCoverageCard side="candidate" c={coverage.candidate} />
      </div>
    </section>
  );
}

export function MetricsPanel({ data }: { data: CompareResult }) {
  const chart = data.metrics
    .filter((m) => m.point_estimate != null)
    .map((m) => ({ metric: m.metric, delta: m.point_estimate }));
  return (
    <section aria-labelledby="metrics-h" className="flex flex-col gap-2">
      <h2 id="metrics-h" className="text-lg font-semibold">
        Metrics
      </h2>
      {data.metrics.length === 0 ? (
        <p className="text-sm">No metric could be computed for this pair.</p>
      ) : (
        <>
          <div style={{ width: "100%", height: 220 }} aria-hidden="true">
            <ResponsiveContainer>
              <BarChart data={chart} margin={{ top: 8, right: 8, bottom: 8, left: 8 }}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="metric" fontSize={11} interval={0} angle={-20} textAnchor="end" height={60} />
                {/* Deltas are read against zero: the domain always includes it, so an all-negative
                    or all-positive set still draws each bar from 0 and keeps the zero line. */}
                <YAxis
                  fontSize={11}
                  tickFormatter={axisTick}
                  domain={[(min: number) => Math.min(0, min), (max: number) => Math.max(0, max)]}
                />
                <Tooltip />
                <ReferenceLine y={0} stroke="#667085" />
                <Bar dataKey="delta" fill="#2563eb" />
              </BarChart>
            </ResponsiveContainer>
          </div>
          <table className="w-full text-sm border-collapse">
            <caption className="sr-only">Per-metric comparison (candidate minus baseline)</caption>
            <thead>
              <tr className="text-left border-b">
                <th className="p-2">Metric</th>
                <th className="p-2">Verdict</th>
                <th className="p-2">Δ (cand − base)</th>
                <th className="p-2">1−α CI bound</th>
                <th className="p-2">Threshold</th>
                <th className="p-2">p</th>
                <th className="p-2">n</th>
                <th className="p-2">Gated</th>
              </tr>
            </thead>
            <tbody>
              {data.metrics.map((m) => (
                <tr key={m.metric} className="border-b">
                  <td className="p-2">
                    {m.metric}
                    <span className="opacity-60 text-xs"> {m.direction === "lower_is_better" ? "↓ better" : "↑ better"}</span>
                  </td>
                  <td className="p-2">
                    <VerdictBadge verdict={verdictOf(m)} />
                  </td>
                  <td className="p-2">{fmt(m.point_estimate)}</td>
                  <td className="p-2">{fmt(m.ci_bound)}</td>
                  <td className="p-2">{fmt(m.threshold, 3)}</td>
                  <td className="p-2">{fmt(m.tail_prob, 3)}</td>
                  <td className="p-2">{m.n_questions}</td>
                  <td className="p-2">{m.gated ? "yes" : "info"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      {data.skipped_metrics?.length > 0 && (
        <div className="text-sm">
          <p className="font-semibold">Not compared:</p>
          <ul className="list-disc ml-5" aria-label="skipped metrics">
            {data.skipped_metrics.map((s) => (
              <li key={s.metric}>
                {s.metric}
                {s.gated ? " (gated)" : ""}: {s.reason}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
