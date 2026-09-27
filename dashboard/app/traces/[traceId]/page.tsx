"use client";

import { useCallback } from "react";

import { EmptyState, ErrorBox, Loading, useAsync } from "@/components/States";
import { TraceWaterfall } from "@/components/TraceWaterfall";
import { api } from "@/lib/api";

export default function TracePage({ params }: { params: { traceId: string } }) {
  const id = params.traceId;
  const load = useCallback(() => api.getTrace(id), [id]);
  const { data, loading, error, reload } = useAsync(load, [id]);

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-xl font-semibold">Trace timeline</h1>
      <p className="font-mono text-xs opacity-70 break-all">{id}</p>
      {loading && <Loading label="Loading trace…" />}
      {error && <ErrorBox error={error} onRetry={reload} />}
      {!loading && !error && data && data.spans.length === 0 && (
        <EmptyState label="This trace has no spans." />
      )}
      {!loading && !error && data && data.spans.length > 0 && (
        <>
          <p className="text-sm opacity-70">
            {data.n_spans} spans{data.truncated ? " (truncated)" : ""}
          </p>
          <TraceWaterfall spans={data.spans} />
        </>
      )}
    </div>
  );
}
