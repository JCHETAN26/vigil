"use client";

import Link from "next/link";
import { useCallback } from "react";

import { EmptyState, ErrorBox, Loading, useAsync } from "@/components/States";
import { api } from "@/lib/api";

export default function RunsPage() {
  const load = useCallback(() => api.listRuns(100, 0), []);
  const { data, loading, error, reload } = useAsync(load, []);

  return (
    <div>
      <h1 className="text-xl font-semibold mb-3">Runs</h1>
      {loading && <Loading label="Loading runs…" />}
      {error && <ErrorBox error={error} onRetry={reload} />}
      {!loading && !error && data && data.runs.length === 0 && (
        <EmptyState label="No eval runs recorded yet." />
      )}
      {!loading && !error && data && data.runs.length > 0 && (
        <table className="w-full text-sm border-collapse">
          <caption className="sr-only">Eval runs, newest first</caption>
          <thead>
            <tr className="text-left border-b">
              <th className="p-2">Suite</th>
              <th className="p-2">Agent</th>
              <th className="p-2">Mode</th>
              <th className="p-2">Trials</th>
              <th className="p-2">Status</th>
              <th className="p-2">Cost</th>
              <th className="p-2">Created</th>
            </tr>
          </thead>
          <tbody>
            {data.runs.map((r) => (
              <tr key={r.id} className="border-b hover:bg-black/5">
                <td className="p-2">
                  <Link href={`/runs/${r.id}`} className="underline">
                    {r.suite}
                  </Link>
                </td>
                <td className="p-2">{r.agent_id}</td>
                <td className="p-2">{r.mode}</td>
                <td className="p-2">{r.trials_per_case}</td>
                <td className="p-2">{r.status}</td>
                <td className="p-2">${r.cost_usd.toFixed(4)}</td>
                <td className="p-2">{r.created_at?.slice(0, 19).replace("T", " ") ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
