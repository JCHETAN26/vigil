"use client";

import Link from "next/link";
import { useCallback } from "react";

import { EmptyState, ErrorBox, Loading, useAsync } from "@/components/States";
import { api } from "@/lib/api";

export default function RunDetailPage({ params }: { params: { id: string } }) {
  const id = params.id;
  const load = useCallback(
    () => Promise.all([api.getRun(id), api.getCases(id, 200, 0)]),
    [id],
  );
  const { data, loading, error, reload } = useAsync(load, [id]);

  if (loading) return <Loading label="Loading run…" />;
  if (error) return <ErrorBox error={error} onRetry={reload} />;
  if (!data) return <EmptyState />;
  const [run, cases] = data;

  const pct = (x: number | null) => (x == null ? "—" : `${(x * 100).toFixed(1)}%`);

  return (
    <div className="flex flex-col gap-4">
      <div>
        <Link href="/runs" className="text-sm underline">
          ← Runs
        </Link>
        <h1 className="text-xl font-semibold mt-1">{run.suite}</h1>
        <p className="text-sm opacity-70">
          {run.agent_id} · {run.mode} · {run.trials_per_case} trial(s) · {run.status}
        </p>
      </div>

      <dl className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-sm">
        <Stat label="Cases" value={`${run.n_ok}/${run.n_cases} ok`} />
        <Stat label="Pass rate" value={pct(run.pass_rate)} />
        <Stat label="Mean cost" value={run.mean_cost_usd == null ? "—" : `$${run.mean_cost_usd.toFixed(5)}`} />
        <Stat label="Mean latency" value={run.mean_latency_ms == null ? "—" : `${run.mean_latency_ms.toFixed(0)} ms`} />
      </dl>

      <h2 className="text-lg font-semibold">Cases</h2>
      {cases.cases.length === 0 ? (
        <EmptyState label="No case results for this run." />
      ) : (
        <table className="w-full text-sm border-collapse">
          <caption className="sr-only">Per-case results</caption>
          <thead>
            <tr className="text-left border-b">
              <th className="p-2">Case</th>
              <th className="p-2">Trial</th>
              <th className="p-2">Passed</th>
              <th className="p-2">Score</th>
              <th className="p-2">Cost</th>
              <th className="p-2">Latency</th>
              <th className="p-2">Trace</th>
            </tr>
          </thead>
          <tbody>
            {cases.cases.map((c) => (
              <tr key={`${c.case_id}-${c.trial}`} className="border-b hover:bg-black/5">
                <td className="p-2 font-mono text-xs">{c.case_id}</td>
                <td className="p-2">{c.trial}</td>
                <td className="p-2">{c.passed == null ? "—" : c.passed ? "yes" : "no"}</td>
                <td className="p-2">{c.score == null ? "—" : c.score.toFixed(3)}</td>
                <td className="p-2">{c.cost_usd == null ? "—" : `$${c.cost_usd.toFixed(5)}`}</td>
                <td className="p-2">{c.latency_ms == null ? "—" : `${c.latency_ms} ms`}</td>
                <td className="p-2">
                  {c.trace_id ? (
                    <Link href={`/traces/${c.trace_id}`} className="underline">
                      timeline
                    </Link>
                  ) : (
                    "—"
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="border rounded-md p-3">
      <dt className="opacity-70">{label}</dt>
      <dd className="text-lg font-semibold">{value}</dd>
    </div>
  );
}
