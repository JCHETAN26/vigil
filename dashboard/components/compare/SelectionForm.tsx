"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { Pager } from "@/components/Pager";
import { ErrorBox, useKeyed } from "@/components/States";
import { api } from "@/lib/api";
import {
  compareHref,
  isRunId,
  sameRunNeedsVersions,
  VERSION_RE,
  versionLabels,
  type PolicyKey,
  type Selection,
} from "@/lib/compareQuery";
import type { RunSummary, RunVersion, RunVersionsPage } from "@/lib/types";

export const VERSION_PAGE = 50; // versions per page of a run's list (the API caps at 200)

// One page of the versions a picked run holds, listed as soon as the run is picked (independent
// of any comparison request). Keyed by run and page: a late answer for a previously picked run
// or page is never shown.
export function useRunVersions(run: string, offset: number) {
  return useKeyed<RunVersionsPage>(isRunId(run) ? `${run.toLowerCase()}@${offset}` : null, (key) => {
    const [id, off] = key.split("@");
    return api.getRunVersions(id, VERSION_PAGE, Number(off));
  });
}
type Discovery = ReturnType<typeof useRunVersions>;

// Picks baseline/candidate runs, an explicit agent version per side, and policy overrides.
// The only fetch before Compare is each picked run's version list; the comparison itself is
// requested once the selection is written to the URL, the single source every triage request
// reads from. `knownVersions` (run id → version prefixes) are further choices from the API: the
// multi-version 400 message, or `versions_in_run` of a successful comparison.
export function SelectionForm({
  runs,
  sel,
  knownVersions,
}: {
  runs: RunSummary[];
  sel: Selection;
  knownVersions: Record<string, string[]>;
}) {
  const router = useRouter();
  const [b, setB] = useState(sel.baseline);
  const [c, setC] = useState(sel.candidate);
  const [bv, setBv] = useState(sel.baselineVersion);
  const [cv, setCv] = useState(sel.candidateVersion);
  const [policy, setPolicy] = useState(sel.policy);
  // The page of each side's version list. A picked version is a full hash in bv/cv, so it
  // survives paging; changing the run starts its list at the first page.
  const [bOff, setBOff] = useState(0);
  const [cOff, setCOff] = useState(0);
  const bVersions = useRunVersions(b, bOff);
  const cVersions = useRunVersions(c, cOff);

  const versionOk = (v: string) => v === "" || VERSION_RE.test(v);
  const needsVersions = sameRunNeedsVersions({ baseline: b, candidate: c, baselineVersion: bv, candidateVersion: cv });
  const sameRunTotal = needsVersions ? bVersions.data?.total : undefined;
  const canSubmit = Boolean(b && c) && versionOk(bv) && versionOk(cv) && !needsVersions;

  const setPol = (k: PolicyKey, v: string) => setPolicy((p) => ({ ...p, [k]: v || undefined }));

  return (
    <form
      aria-label="comparison selection"
      className="flex flex-col gap-3 text-sm border rounded-md p-3"
      onSubmit={(e) => {
        e.preventDefault();
        if (!canSubmit) return;
        // A new selection starts the case view from the top with the default metric/filter.
        router.push(
          compareHref({
            baseline: b,
            candidate: c,
            baselineVersion: bv,
            candidateVersion: cv,
            policy,
            metric: "",
            filter: "all",
            offset: 0,
          }),
        );
      }}
    >
      <div className="flex flex-wrap items-start gap-4">
        <Side
          label="Baseline"
          runs={runs}
          run={b}
          version={bv}
          discovery={bVersions}
          offset={bOff}
          onOffset={setBOff}
          hints={knownVersions[b] ?? []}
          onRun={(v) => {
            setB(v);
            setBv("");
            setBOff(0);
          }}
          onVersion={setBv}
          versionOk={versionOk(bv)}
        />
        <Side
          label="Candidate"
          runs={runs}
          run={c}
          version={cv}
          discovery={cVersions}
          offset={cOff}
          onOffset={setCOff}
          hints={knownVersions[c] ?? []}
          onRun={(v) => {
            setC(v);
            setCv("");
            setCOff(0);
          }}
          onVersion={setCv}
          versionOk={versionOk(cv)}
        />
      </div>
      {needsVersions &&
        (sameRunTotal != null && sameRunTotal < 2 ? (
          <p className="text-watch">
            Baseline and candidate are the same run, which holds {sameRunTotal === 0 ? "no agent versions" : "a single agent version"}: pick a different run for one side.
          </p>
        ) : (
          <p className="text-watch">
            Baseline and candidate are the same run: select two different agent versions.
          </p>
        ))}

      <details open={Object.keys(sel.policy).length > 0}>
        <summary className="cursor-pointer">Policy overrides (blank = API default)</summary>
        <div className="flex flex-wrap gap-3 mt-2">
          <label className="flex flex-col">
            Method
            <select
              value={policy.method ?? ""}
              onChange={(e) => setPol("method", e.target.value)}
              className="border rounded p-1"
            >
              <option value="">default</option>
              <option value="t">t</option>
              <option value="percentile">percentile</option>
              <option value="bca">bca</option>
            </select>
          </label>
          <NumberField label="α (0–0.5)" value={policy.alpha} onChange={(v) => setPol("alpha", v)} />
          <NumberField
            label="Pass-rate threshold"
            value={policy.threshold_pass}
            onChange={(v) => setPol("threshold_pass", v)}
          />
          <NumberField
            label="TokenF1 threshold"
            value={policy.threshold_f1}
            onChange={(v) => setPol("threshold_f1", v)}
          />
        </div>
      </details>

      <div>
        <button type="submit" disabled={!canSubmit} className="border rounded px-3 py-1 disabled:opacity-40">
          Compare
        </button>
      </div>
    </form>
  );
}

function Side({
  label,
  runs,
  run,
  version,
  discovery,
  offset,
  onOffset,
  hints,
  onRun,
  onVersion,
  versionOk,
}: {
  label: string;
  runs: RunSummary[];
  run: string;
  version: string;
  discovery: Discovery;
  offset: number;
  onOffset: (offset: number) => void;
  hints: string[];
  onRun: (v: string) => void;
  onVersion: (v: string) => void;
  versionOk: boolean;
}) {
  // Full hashes from the current page of the run's version list. The API's hint prefixes are
  // the same run's versions, so they are only a fallback while no list page is available.
  const listed = discovery.data?.versions ?? [];
  const byHash = new Map<string, RunVersion>(listed.map((v) => [v.agent_version, v]));
  const choices = discovery.data ? listed.map((v) => v.agent_version) : hints;
  const labels = versionLabels(choices);
  // A typed prefix picks a choice only when it is unambiguous among the choices.
  const prefixMatches = version ? choices.filter((v) => v.startsWith(version)) : [];
  const picked = (v: string) =>
    version !== "" && (v === version || (prefixMatches.length === 1 && prefixMatches[0] === v) || version.startsWith(v));
  const describe = (v: string) => {
    const meta = byHash.get(v);
    if (!meta) return v;
    if (!meta.registered) return `${v} · results only (not registered)`;
    return `${v} · ${meta.status} · ${meta.cases_done}/${meta.cases_total} units`;
  };
  const pickedElsewhere = version !== "" && discovery.data != null && !choices.some(picked);
  const runLabel = (r: RunSummary) =>
    `${r.suite} · ${r.id.slice(0, 8)} · ${r.mode} · ${r.status}${r.created_at ? ` · ${r.created_at.slice(0, 10)}` : ""}`;
  const runListed = runs.some((r) => r.id === run);
  const errId = `${label.toLowerCase()}-version-error`;
  return (
    <fieldset className="flex flex-col gap-2 min-w-72">
      <legend className="font-semibold">{label}</legend>
      <label className="flex flex-col">
        {label} run
        <select value={run} onChange={(e) => onRun(e.target.value)} className="border rounded p-1">
          <option value="">— pick a run —</option>
          {/* A run from the URL that is older than the listed page stays selectable. */}
          {run && !runListed && <option value={run}>{run}</option>}
          {runs.map((r) => (
            <option key={r.id} value={r.id}>
              {runLabel(r)}
            </option>
          ))}
        </select>
      </label>
      <label className="flex flex-col">
        {label} agent version
        <input
          value={version}
          onChange={(e) => onVersion(e.target.value.trim().toLowerCase())}
          placeholder="hash or ≥8-char prefix (needed for mixed-version runs)"
          className="border rounded p-1 font-mono text-xs"
          aria-invalid={!versionOk}
          aria-describedby={versionOk ? undefined : errId}
          spellCheck={false}
        />
      </label>
      {!versionOk && (
        <p id={errId} className="text-regression text-xs">
          Use 8–64 lowercase hex characters.
        </p>
      )}
      {discovery.loading && (
        <span role="status" className="text-xs opacity-70">
          Loading versions…
        </span>
      )}
      {discovery.error && (
        <ErrorBox
          error={new Error(`Could not list the agent versions of this run: ${discovery.error.message}`)}
          onRetry={discovery.reload}
        />
      )}
      {discovery.data && discovery.data.total === 0 && hints.length === 0 && (
        <p className="text-xs opacity-70">This run has no agent versions recorded.</p>
      )}
      {choices.length > 0 && (
        <div className="flex flex-wrap items-center gap-1 text-xs" role="group" aria-label={`${label} versions in run`}>
          {/* Sorted by hash: the order says nothing about which version is the baseline. */}
          <span className="opacity-70">Versions in this run{discovery.data ? ` (${discovery.data.total})` : ""}:</span>
          {choices.map((v) => (
            <button
              key={v}
              type="button"
              title={describe(v)}
              aria-pressed={picked(v)}
              onClick={() => onVersion(v)}
              className="border rounded px-1 font-mono aria-pressed:bg-black/10"
            >
              {labels[v]}
            </button>
          ))}
        </div>
      )}
      {pickedElsewhere && (
        <p className="text-xs opacity-70">
          Selected <span className="font-mono">{version.slice(0, 12)}</span> is not on this page of the list.
        </p>
      )}
      {discovery.data && (
        <Pager
          label={`${label} version pages`}
          offset={offset}
          limit={VERSION_PAGE}
          shown={listed.length}
          hasNext={offset + listed.length < discovery.data.total}
          total={discovery.data.total}
          onChange={onOffset}
        />
      )}
    </fieldset>
  );
}

function NumberField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string | undefined;
  onChange: (v: string) => void;
}) {
  return (
    <label className="flex flex-col">
      {label}
      <input
        type="number"
        step="any"
        min={0}
        value={value ?? ""}
        onChange={(e) => onChange(e.target.value)}
        className="border rounded p-1 w-32"
      />
    </label>
  );
}
