"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useMemo } from "react";

import { CasesPanel } from "@/components/compare/CasesPanel";
import { DiffPanel } from "@/components/compare/DiffPanel";
import { SelectionForm } from "@/components/compare/SelectionForm";
import {
  ContextPanel,
  CoveragePanel,
  MetricsPanel,
  PolicyPanel,
  VerdictBanner,
  shortVersion,
} from "@/components/compare/Summary";
import { EmptyState, ErrorBox, Loading, useAsync, useKeyed } from "@/components/States";
import { ApiError, api } from "@/lib/api";
import {
  casesQuery,
  compareHref,
  pairQuery,
  parseSelection,
  sameRunNeedsVersions,
  selectionProblems,
  summaryQuery,
  versionsFromError,
  type Selection,
} from "@/lib/compareQuery";

// The triage calls take the query the key encodes.
const asQuery =
  <T,>(fn: (q: URLSearchParams) => Promise<T>) =>
  (key: string) =>
    fn(new URLSearchParams(key));

function CompareInner() {
  const router = useRouter();
  const sp = useSearchParams();
  const sel = useMemo(() => parseSelection(new URLSearchParams(sp.toString())), [sp]);
  const problems = selectionProblems(sel);
  // The same run on both sides needs two different explicit versions. Without them the API can
  // only refuse, so ask for them (the form lists the run's versions) instead of requesting.
  const needsVersions = sameRunNeedsVersions(sel);
  const ready = Boolean(sel.baseline && sel.candidate) && problems.length === 0 && !needsVersions;
  const go = (next: Selection) => router.push(compareHref(next));

  const runs = useAsync(useCallback(() => api.listRuns(200, 0), []), []);

  // The summary decides the verdict. Cases are only fetched once it succeeded, with the same
  // query plus the case view. The version diff shares the run/version pair; it also loads when
  // /compare refused the pair for a reason /versions/diff accepts (different suites, same version).
  const summary = useKeyed(ready ? summaryQuery(sel).toString() : null, asQuery(api.compare));
  const sErr = summary.error;
  const versionHint = sErr ? versionsFromError(sErr.message) : null;
  const diffAllowed =
    summary.data != null || (sErr instanceof ApiError && sErr.status === 400 && versionHint == null);
  const cases = useKeyed(summary.data ? casesQuery(sel).toString() : null, asQuery(api.compareCases));
  const diff = useKeyed(diffAllowed ? pairQuery(sel).toString() : null, asQuery(api.versionDiff));

  // Version choices per run, only ever from the API.
  const knownVersions: Record<string, string[]> = {};
  if (versionHint) knownVersions[versionHint.runId] = versionHint.versions;
  if (summary.data) {
    for (const s of [summary.data.context.baseline, summary.data.context.candidate]) {
      knownVersions[s.run_id] = s.versions_in_run;
    }
  }

  // Both follow-up calls must have resolved the same versions as the summary.
  const s = summary.data;
  const casesMismatch =
    s && cases.data &&
    (cases.data.baseline_version !== s.baseline_version ||
      cases.data.candidate_version !== s.candidate_version ||
      cases.data.verdict !== s.verdict);
  const diffMismatch =
    s && diff.data &&
    (diff.data.baseline.agent_version !== s.baseline_version ||
      diff.data.candidate.agent_version !== s.candidate_version);

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-xl font-semibold">Compare runs</h1>
      {runs.loading && <Loading label="Loading runs…" />}
      {runs.error && <ErrorBox error={runs.error} onRetry={runs.reload} />}
      {runs.data && (
        <SelectionForm key={compareHref(sel)} runs={runs.data.runs} sel={sel} knownVersions={knownVersions} />
      )}

      {problems.length > 0 && <ErrorBox error={new Error(`Invalid selection: ${problems.join("; ")}`)} />}
      {!sel.baseline || !sel.candidate ? (
        <EmptyState label="Pick a baseline and a candidate run, then Compare." />
      ) : null}
      {sel.baseline && sel.candidate && problems.length === 0 && needsVersions && (
        <EmptyState label="Baseline and candidate are the same run: choose which of its agent versions is the baseline and which is the candidate above, then Compare." />
      )}

      {summary.loading && <Loading label="Loading comparison…" />}
      {sErr && (
        <div>
          <ErrorBox error={sErr} onRetry={summary.reload} />
          <p className="text-sm">No verdict is shown because the comparison request failed.</p>
          {versionHint && (
            <p className="text-sm mt-1">
              Run <span className="font-mono text-xs">{versionHint.runId}</span> holds several agent versions (
              {versionHint.versions.map(shortVersion).join(", ")}). Pick one per side in the form above.
            </p>
          )}
        </div>
      )}

      {s && (
        <>
          <VerdictBanner data={s} />
          <ContextPanel
            data={s}
            onPickVersion={(side, v) =>
              go({ ...sel, [side === "baseline" ? "baselineVersion" : "candidateVersion"]: v, offset: 0 })
            }
          />
          <MetricsPanel data={s} />
        </>
      )}

      {s && cases.loading && <Loading label="Loading affected cases…" />}
      {s && cases.error && <ErrorBox error={cases.error} onRetry={cases.reload} />}
      {casesMismatch && (
        <ErrorBox
          error={new Error("The case list was computed for different versions or a different verdict than the summary. Reload the page.")}
        />
      )}
      {s && cases.data && !casesMismatch && (
        <CasesPanel data={cases.data} onView={(v) => go({ ...sel, ...v })} />
      )}

      {diff.loading && <Loading label="Loading version diff…" />}
      {diff.error && (
        <section aria-label="version diff error">
          <h2 className="text-lg font-semibold">Version diff</h2>
          <ErrorBox error={diff.error} onRetry={diff.reload} />
        </section>
      )}
      {diffMismatch && (
        <ErrorBox error={new Error("The version diff resolved different versions than the summary. Reload the page.")} />
      )}
      {diff.data && !diffMismatch && <DiffPanel data={diff.data} />}

      {s && (
        <>
          <CoveragePanel coverage={s.coverage} />
          <PolicyPanel policy={s.policy} />
        </>
      )}
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
