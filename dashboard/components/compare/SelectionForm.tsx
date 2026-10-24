"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { compareHref, VERSION_RE, type PolicyKey, type Selection } from "@/lib/compareQuery";
import type { RunSummary } from "@/lib/types";

// Picks baseline/candidate runs, an explicit agent version per side, and policy overrides.
// Nothing is fetched until Compare is pressed; the selection is then written to the URL, which is
// the single source every request reads from. `knownVersions` (run id → version prefixes) comes
// from the API: the multi-version 400 message, or `versions_in_run` of a successful comparison.
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

  const versionOk = (v: string) => v === "" || VERSION_RE.test(v);
  const sameRun = Boolean(b) && b === c;
  const sameRunNeedsVersions = sameRun && (!bv || !cv || bv === cv);
  const canSubmit = Boolean(b && c) && versionOk(bv) && versionOk(cv) && !sameRunNeedsVersions;

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
          known={knownVersions[b] ?? []}
          onRun={(v) => {
            setB(v);
            setBv("");
          }}
          onVersion={setBv}
          versionOk={versionOk(bv)}
        />
        <Side
          label="Candidate"
          runs={runs}
          run={c}
          version={cv}
          known={knownVersions[c] ?? []}
          onRun={(v) => {
            setC(v);
            setCv("");
          }}
          onVersion={setCv}
          versionOk={versionOk(cv)}
        />
      </div>
      {sameRunNeedsVersions && (
        <p className="text-watch">
          Baseline and candidate are the same run: select two different agent versions.
        </p>
      )}

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
  known,
  onRun,
  onVersion,
  versionOk,
}: {
  label: string;
  runs: RunSummary[];
  run: string;
  version: string;
  known: string[];
  onRun: (v: string) => void;
  onVersion: (v: string) => void;
  versionOk: boolean;
}) {
  const runLabel = (r: RunSummary) =>
    `${r.suite} · ${r.id.slice(0, 8)} · ${r.mode} · ${r.status}${r.created_at ? ` · ${r.created_at.slice(0, 10)}` : ""}`;
  const listed = runs.some((r) => r.id === run);
  const errId = `${label.toLowerCase()}-version-error`;
  return (
    <fieldset className="flex flex-col gap-2 min-w-72">
      <legend className="font-semibold">{label}</legend>
      <label className="flex flex-col">
        {label} run
        <select value={run} onChange={(e) => onRun(e.target.value)} className="border rounded p-1">
          <option value="">— pick a run —</option>
          {/* A run from the URL that is older than the listed page stays selectable. */}
          {run && !listed && <option value={run}>{run}</option>}
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
      {known.length > 0 && (
        <div className="flex flex-wrap items-center gap-1 text-xs" role="group" aria-label={`${label} versions in run`}>
          <span className="opacity-70">Versions in this run:</span>
          {known.map((v) => (
            <button
              key={v}
              type="button"
              aria-pressed={version !== "" && (v.startsWith(version) || version.startsWith(v))}
              onClick={() => onVersion(v)}
              className="border rounded px-1 font-mono aria-pressed:bg-black/10"
            >
              {v.slice(0, 12)}
            </button>
          ))}
        </div>
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
