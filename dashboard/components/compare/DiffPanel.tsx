"use client";

import type { FieldDiff, Manifest, VersionDiff } from "@/lib/types";

import { shortVersion } from "./Summary";

const FIELD_ORDER = ["model", "git_sha", "tools", "params", "prompts", "code_ref"] as const;

const changedLabel = (c: boolean | null | undefined) => (c == null ? "unknown" : c ? "changed" : "same");

// What changed between the two agent versions, from /versions/diff. A missing manifest makes every
// field "unknown" rather than "same", and the redaction note is always shown because prompt and
// tool bodies are not scanned for secrets.
export function DiffPanel({ data }: { data: VersionDiff }) {
  const missing = data.missing_manifests ?? [];
  const fields = Object.keys(data.fields ?? {}).sort(
    (a, b) => order(a) - order(b) || a.localeCompare(b),
  );
  return (
    <section aria-labelledby="diff-h" className="flex flex-col gap-2 text-sm">
      <h2 id="diff-h" className="text-lg font-semibold">
        Version diff
      </h2>
      <p>
        <span className="font-mono text-xs">{shortVersion(data.baseline.agent_version)}</span> →{" "}
        <span className="font-mono text-xs">{shortVersion(data.candidate.agent_version)}</span>
        {data.baseline.agent_id !== data.candidate.agent_id &&
          ` (agents ${data.baseline.agent_id} → ${data.candidate.agent_id})`}
      </p>
      <p role="note" aria-label="diff summary" className="font-semibold">
        {data.identical === true && "Manifests are identical."}
        {data.identical === false && "Manifests differ."}
        {data.identical == null &&
          `Cannot tell what changed: no manifest recorded for ${missing.length ? missing.join(" and ") : "one side"}.`}
      </p>
      {data.same_version && <p>Both sides use the same agent version.</p>}
      {!data.same_suite && <p className="text-watch">The runs come from different suites.</p>}

      {fields.length > 0 && (
        <table className="w-full border-collapse">
          <caption className="sr-only">Manifest fields</caption>
          <thead>
            <tr className="text-left border-b">
              <th className="p-2">Field</th>
              <th className="p-2">Change</th>
              <th className="p-2">Keys</th>
            </tr>
          </thead>
          <tbody>
            {fields.map((f) => (
              <FieldRow
                key={f}
                name={f}
                d={data.fields[f]}
                base={data.baseline_manifest}
                cand={data.candidate_manifest}
              />
            ))}
          </tbody>
        </table>
      )}

      <Redaction data={data} />
    </section>
  );
}

function order(f: string) {
  const i = (FIELD_ORDER as readonly string[]).indexOf(f);
  return i === -1 ? FIELD_ORDER.length : i;
}

function FieldRow({
  name,
  d,
  base,
  cand,
}: {
  name: string;
  d: FieldDiff;
  base: Manifest | null;
  cand: Manifest | null;
}) {
  const b = base ? (base as unknown as Record<string, unknown>)[name] : undefined;
  const c = cand ? (cand as unknown as Record<string, unknown>)[name] : undefined;
  const keys = [
    ["added", d.added],
    ["removed", d.removed],
    ["modified", d.modified],
  ].filter(([, v]) => Array.isArray(v) && v.length > 0) as [string, string[]][];
  return (
    <tr className="border-b align-top" aria-label={`field ${name}`}>
      <td className="p-2 font-mono text-xs">{name}</td>
      <td className={`p-2 ${d.changed ? "font-semibold text-watch" : d.changed == null ? "opacity-70" : ""}`}>
        {changedLabel(d.changed)}
      </td>
      <td className="p-2">
        {keys.map(([k, v]) => (
          <div key={k}>
            {k}: <span className="font-mono text-xs">{v.join(", ")}</span>
          </div>
        ))}
        {d.changed && (b !== undefined || c !== undefined) && (
          <details className="mt-1">
            <summary className="cursor-pointer text-xs">values</summary>
            <div className="grid sm:grid-cols-2 gap-2 mt-1">
              <Value label="baseline" v={b} />
              <Value label="candidate" v={c} />
            </div>
          </details>
        )}
      </td>
    </tr>
  );
}

function Value({ label, v }: { label: string; v: unknown }) {
  const text = v === undefined ? "(no manifest)" : typeof v === "string" ? v : JSON.stringify(v, null, 2);
  return (
    <div>
      <p className="text-xs opacity-70">{label}</p>
      <pre className="text-xs whitespace-pre-wrap break-words max-h-64 overflow-auto border rounded p-1">{text}</pre>
    </div>
  );
}

function Redaction({ data }: { data: VersionDiff }) {
  const r = data.redaction;
  if (!r) return null;
  const list = (label: string, ids: string[] | undefined) =>
    ids && ids.length > 0 ? (
      <p>
        {label}: <span className="font-mono text-xs">{ids.join(", ")}</span>
      </p>
    ) : null;
  return (
    <div className="border rounded-md p-2 text-xs" role="note" aria-label="redaction">
      {list("Redacted (baseline)", r.redacted_paths?.baseline)}
      {list("Redacted (candidate)", r.redacted_paths?.candidate)}
      {list("Truncated (baseline)", r.truncated_paths?.baseline)}
      {list("Truncated (candidate)", r.truncated_paths?.candidate)}
      <p className="opacity-70">{r.note}</p>
    </div>
  );
}
