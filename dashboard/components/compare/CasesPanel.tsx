"use client";

import Link from "next/link";

import { Pager } from "@/components/Pager";
import { EmptyState } from "@/components/States";
import { CASE_PAGE } from "@/lib/compareQuery";
import type { CaseDelta, CaseFilter, CaseSide, CompareCasesResult } from "@/lib/types";

import { fmt } from "./Summary";

const FILTER_LABELS: Record<CaseFilter, string> = {
  all: "all cases",
  adverse: "candidate worse",
  unpaired: "unpaired",
  failed: "any failed trial",
};

const EMPTY_LABELS: Record<CaseFilter, string> = {
  all: "No cases have results on either side.",
  adverse: "No case is worse on the candidate for this metric.",
  unpaired: "Every case has trials on both sides.",
  failed: "No trial failed on either side.",
};

// One bounded page of /compare/cases. Metric, filter and offset are URL state owned by the page;
// this component only renders and reports changes.
export function CasesPanel({
  data,
  onView,
}: {
  data: CompareCasesResult;
  onView: (v: { metric?: string; filter?: CaseFilter; offset: number }) => void;
}) {
  const m = data.sort_metric;
  const pager = (
    <Pager
      label="affected case pages"
      offset={data.offset}
      limit={CASE_PAGE}
      shown={data.cases.length}
      hasNext={data.offset + data.cases.length < data.total}
      total={data.total}
      onChange={(offset) => onView({ offset })}
    />
  );
  return (
    <section aria-labelledby="cases-h" className="flex flex-col gap-3">
      <h2 id="cases-h" className="text-lg font-semibold">
        Affected cases
      </h2>
      <div className="flex flex-wrap items-end gap-3 text-sm">
        <label className="flex flex-col">
          Sort metric
          <select
            value={m}
            onChange={(e) => onView({ metric: e.target.value, offset: 0 })}
            className="border rounded p-1"
          >
            {data.metrics_available.map((x) => (
              <option key={x} value={x}>
                {x}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col">
          Show
          <select
            value={data.filter}
            onChange={(e) => onView({ filter: e.target.value as CaseFilter, offset: 0 })}
            className="border rounded p-1"
          >
            {(Object.keys(FILTER_LABELS) as CaseFilter[]).map((f) => (
              <option key={f} value={f}>
                {FILTER_LABELS[f]}
              </option>
            ))}
          </select>
        </label>
        <p className="opacity-70">
          Worst first by how much the candidate is worse on <b>{m}</b> (
          {data.sort_direction === "lower_is_better" ? "lower is better" : "higher is better"}); unpaired cases last.
        </p>
      </div>

      {data.cases.length === 0 ? (
        <>
          <EmptyState label={data.offset > 0 ? "No cases on this page." : EMPTY_LABELS[data.filter]} />
          {pager}
        </>
      ) : (
        <>
          <table className="w-full text-sm border-collapse">
            <caption className="sr-only">Cases sorted by adverse delta on {m}</caption>
            <thead>
              <tr className="text-left border-b align-bottom">
                <th className="p-2">Case</th>
                <th className="p-2">Adverse Δ</th>
                <th className="p-2">Δ {m}</th>
                <th className="p-2">Baseline</th>
                <th className="p-2">Candidate</th>
              </tr>
            </thead>
            <tbody>
              {data.cases.map((c) => (
                <CaseRow key={c.case_id} c={c} metric={m} />
              ))}
            </tbody>
          </table>
          {pager}
        </>
      )}
    </section>
  );
}

function CaseRow({ c, metric }: { c: CaseDelta; metric: string }) {
  const worse = (c.adverse_delta ?? 0) > 0;
  return (
    <tr className="border-b align-top" aria-label={`case ${c.case_id}`}>
      <td className="p-2 font-mono text-xs">
        {c.case_id}
        {!c.paired && <div className="font-sans text-watch">unpaired</div>}
      </td>
      <td className={`p-2 ${worse ? "text-regression font-semibold" : ""}`}>
        {c.paired ? `${worse ? "▲ " : ""}${fmt(c.adverse_delta, 3)}` : "—"}
      </td>
      <td className="p-2">{fmt(c.delta?.[metric], 3)}</td>
      <td className="p-2">
        <SideCell side="baseline" caseId={c.case_id} s={c.baseline} metric={metric} />
      </td>
      <td className="p-2">
        <SideCell side="candidate" caseId={c.case_id} s={c.candidate} metric={metric} />
      </td>
    </tr>
  );
}

function SideCell({
  side,
  caseId,
  s,
  metric,
}: {
  side: "baseline" | "candidate";
  caseId: string;
  s: CaseSide;
  metric: string;
}) {
  if (!s || s.n_trials === 0) return <span className="opacity-70">no trials</span>;
  const statuses = Object.entries(s.statuses ?? {})
    .map(([k, n]) => `${k} ${n}`)
    .join(" · ");
  return (
    <div className="flex flex-col gap-1">
      <span>
        {s.n_ok}/{s.n_trials} ok
        {s.n_failed > 0 && <span className="text-regression"> · {s.n_failed} failed</span>}
        <span className="opacity-70"> · mean {metric} {fmt(s.mean?.[metric], 3)}</span>
      </span>
      {statuses && <span className="opacity-70 text-xs">{statuses}</span>}
      <ul className="text-xs flex flex-col gap-0.5">
        {s.trials.map((t) => (
          <li key={t.trial}>
            t{t.trial} {t.status}
            {t.passed != null && (t.passed ? " ✓ pass" : " ✗ fail")}{" "}
            {t.trace_id ? (
              <Link
                href={`/traces/${t.trace_id}`}
                className="underline"
                aria-label={`${side} trace, case ${caseId} trial ${t.trial}`}
              >
                trace
              </Link>
            ) : (
              <span className="opacity-60">no trace</span>
            )}
            {t.error && <p className="text-regression break-words whitespace-pre-wrap">{t.error}</p>}
          </li>
        ))}
      </ul>
    </div>
  );
}
