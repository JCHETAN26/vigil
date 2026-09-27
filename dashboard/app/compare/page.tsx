"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useState } from "react";
import { Bar, BarChart, CartesianGrid, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { EmptyState, ErrorBox, Loading, useAsync } from "@/components/States";
import { VerdictBadge, verdictOf } from "@/components/VerdictBadge";
import { api } from "@/lib/api";
import type { CompareResult, RunSummary } from "@/lib/types";

function Picker({ runs, baseline, candidate }: { runs: RunSummary[]; baseline: string; candidate: string }) {
  const router = useRouter();
  const [b, setB] = useState(baseline);
  const [c, setC] = useState(candidate);
  const label = (r: RunSummary) => `${r.suite} · ${r.id.slice(0, 8)} · ${r.mode}`;
  return (
    <form
      className="flex flex-wrap items-end gap-3 text-sm"
      onSubmit={(e) => {
        e.preventDefault();
        if (b && c) router.push(`/compare?baseline=${b}&candidate=${c}`);
      }}
    >
      <label className="flex flex-col">
        Baseline
        <select value={b} onChange={(e) => setB(e.target.value)} className="border rounded p-1 min-w-64">
          <option value="">— pick a run —</option>
          {runs.map((r) => (
            <option key={r.id} value={r.id}>
              {label(r)}
            </option>
          ))}
        </select>
      </label>
      <label className="flex flex-col">
        Candidate
        <select value={c} onChange={(e) => setC(e.target.value)} className="border rounded p-1 min-w-64">
          <option value="">— pick a run —</option>
          {runs.map((r) => (
            <option key={r.id} value={r.id}>
              {label(r)}
            </option>
          ))}
        </select>
      </label>
      <button type="submit" disabled={!b || !c} className="border rounded px-3 py-1 disabled:opacity-40">
        Compare
      </button>
    </form>
  );
}

function Result({ data }: { data: CompareResult }) {
  const chart = data.metrics.map((m) => ({ metric: m.metric, delta: m.point_estimate }));
  return (
    <div className="flex flex-col gap-4">
      <div
        role="status"
        className={`p-3 rounded-md border text-sm font-semibold ${data.gated_regression ? "border-regression" : "border-ok"}`}
      >
        {data.gated_regression
          ? "▲ REGRESSION detected on a gated metric (pass rate / TokenF1)"
          : "✓ No regression on gated metrics"}
        <span className="font-normal opacity-70">
          {" "}· {data.paired_cases} paired case(s) · method {data.method} · α {data.alpha}
        </span>
      </div>

      <div style={{ width: "100%", height: 220 }} aria-hidden="true">
        <ResponsiveContainer>
          <BarChart data={chart} margin={{ top: 8, right: 8, bottom: 8, left: 8 }}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey="metric" fontSize={11} interval={0} angle={-20} textAnchor="end" height={60} />
            <YAxis fontSize={11} />
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
            <th className="p-2">p</th>
            <th className="p-2">Gated</th>
          </tr>
        </thead>
        <tbody>
          {data.metrics.map((m) => (
            <tr key={m.metric} className="border-b">
              <td className="p-2">{m.metric}</td>
              <td className="p-2">
                <VerdictBadge verdict={verdictOf(m)} />
              </td>
              <td className="p-2">{m.point_estimate.toFixed(4)}</td>
              <td className="p-2">{m.ci_bound.toFixed(4)}</td>
              <td className="p-2">{m.tail_prob.toFixed(3)}</td>
              <td className="p-2">{m.gated ? "yes" : "info"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CompareInner() {
  const sp = useSearchParams();
  const baseline = sp.get("baseline") ?? "";
  const candidate = sp.get("candidate") ?? "";
  const ready = Boolean(baseline && candidate);

  const runs = useAsync(useCallback(() => api.listRuns(200, 0), []), []);
  const cmp = useAsync(
    useCallback(
      () => (ready ? api.compare(baseline, candidate) : Promise.resolve(null)),
      [baseline, candidate, ready],
    ),
    [baseline, candidate],
  );

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-xl font-semibold">Compare runs</h1>
      {runs.loading && <Loading label="Loading runs…" />}
      {runs.error && <ErrorBox error={runs.error} onRetry={runs.reload} />}
      {runs.data && <Picker runs={runs.data.runs} baseline={baseline} candidate={candidate} />}

      {!ready && <EmptyState label="Pick a baseline and a candidate run, then Compare." />}
      {ready && cmp.loading && <Loading label="Comparing…" />}
      {ready && cmp.error && <ErrorBox error={cmp.error} onRetry={cmp.reload} />}
      {ready && !cmp.loading && !cmp.error && cmp.data && <Result data={cmp.data} />}
    </div>
  );
}

export default function ComparePage() {
  return (
    <Suspense fallback={<Loading />}>
      <CompareInner />
    </Suspense>
  );
}
