"use client";

import Link from "next/link";
import { useParams, usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback } from "react";

import { Pager, parseOffset } from "@/components/Pager";
import { EmptyState, ErrorBox, Loading, useAsync } from "@/components/States";
import { api } from "@/lib/api";
import type { RunDetail } from "@/lib/types";

const PAGE = 100;
// Same 12-char prefix the compare view shows (not imported from there: that module pulls in recharts).
const shortVersion = (v: string) => v.slice(0, 12);

function RunDetailInner() {
  // Next 15 passes route `params` as a Promise; a client page reads them via useParams() instead
  // (React 18 has no stable `use`).
  const id = useParams<{ id: string }>().id;
  const load = useCallback(() => api.getRun(id), [id]);
  const { data: run, loading, error, reload } = useAsync(load, [id]);

  if (loading) return <Loading label="Loading run…" />;
  if (error) return <ErrorBox error={error} onRetry={reload} />;
  if (!run) return <EmptyState />;

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
      {run.n_versions > 1 && (
        <p className="text-sm opacity-70">
          These figures pool all {run.n_versions} agent versions in this run. Compare the versions to
          see them separately.
        </p>
      )}

      <h2 className="text-lg font-semibold">Cases</h2>
      <CasesTable run={run} />
    </div>
  );
}

// One page of case×trial rows. The offset lives in the URL so a page is linkable and Back works;
// the endpoint reports no total, so fetch one extra row to know whether a next page exists.
function CasesTable({ run }: { run: RunDetail }) {
  const router = useRouter();
  const pathname = usePathname();
  const offset = parseOffset(useSearchParams().get("offset"));
  const load = useCallback(() => api.getCases(run.id, PAGE + 1, offset), [run.id, offset]);
  const { data, loading, error, reload } = useAsync(load, [run.id, offset]);

  if (loading) return <Loading label="Loading cases…" />;
  if (error) return <ErrorBox error={error} onRetry={reload} />;
  if (!data) return <EmptyState />;
  const rows = data.cases.slice(0, PAGE);
  const pager = (
    <Pager
      label="case pages"
      offset={offset}
      limit={PAGE}
      shown={rows.length}
      hasNext={data.cases.length > PAGE}
      total={run.n_cases}
      onChange={(o) => router.push(o ? `${pathname}?offset=${o}` : pathname)}
    />
  );

  if (rows.length === 0) {
    return (
      <>
        <EmptyState label={offset ? "No cases on this page." : "No case results for this run."} />
        {pager}
      </>
    );
  }
  return (
    <>
      <table className="w-full text-sm border-collapse">
        <caption className="sr-only">Per-case results</caption>
        <thead>
          <tr className="text-left border-b">
            <th className="p-2">Case</th>
            <th className="p-2">Version</th>
            <th className="p-2">Trial</th>
            <th className="p-2">Status</th>
            <th className="p-2">Passed</th>
            <th className="p-2">Score</th>
            <th className="p-2">Cost</th>
            <th className="p-2">Latency</th>
            <th className="p-2">Trace</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((c) => (
            <tr key={`${c.case_id}-${c.agent_version}-${c.trial}`} className="border-b hover:bg-black/5">
              <td className="p-2 font-mono text-xs">{c.case_id}</td>
              <td className="p-2 font-mono text-xs" title={c.agent_version}>
                {shortVersion(c.agent_version)}
              </td>
              <td className="p-2">{c.trial}</td>
              <td className="p-2">{c.status}</td>
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
      {pager}
    </>
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

export default function RunDetailPage() {
  return (
    <Suspense fallback={<Loading />}>
      <RunDetailInner />
    </Suspense>
  );
}
