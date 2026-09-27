"use client";

import { useMemo, useRef, useState } from "react";

import type { SpanRow } from "@/lib/types";

// Span categories. Each has a color AND a distinct SVG pattern AND a text label in the legend,
// so the waterfall is never distinguished by color alone (accessibility requirement).
type Cat = "run" | "llm" | "sim" | "tool" | "retrieval" | "other";

const CATS: Record<Cat, { label: string; color: string; pattern: string }> = {
  run: { label: "agent run", color: "#475467", pattern: "p-run" },
  llm: { label: "LLM call", color: "#2563eb", pattern: "p-llm" },
  sim: { label: "user simulator", color: "#b54708", pattern: "p-sim" },
  tool: { label: "tool call", color: "#067647", pattern: "p-tool" },
  retrieval: { label: "retrieval", color: "#6941c6", pattern: "p-ret" },
  other: { label: "other", color: "#98a2b3", pattern: "p-other" },
};

function categorize(s: SpanRow): Cat {
  if (s.name === "agent.run") return "run";
  if (s.role === "user_simulator") return "sim";
  if (s.name === "search") return "retrieval";
  if (s.name.startsWith("gen_ai")) return "llm";
  if (s.name.startsWith("tool.")) return "tool";
  return "other";
}

// Readable label; the full span name is available on hover/focus (SVG <title> + aria-label).
function labelOf(s: SpanRow, cat: Cat): string {
  switch (cat) {
    case "run":
      return "agent run";
    case "sim":
      return "user simulator";
    case "retrieval":
      return "Search";
    case "llm":
      return `LLM · ${s.model ?? "?"}`;
    case "tool":
      return `Tool · ${s.name.replace(/^tool\./, "")}`;
    default:
      return s.name;
  }
}

function depthOf(span: SpanRow, byId: Map<string, SpanRow>): number {
  let d = 0;
  let cur = span.parent_span_id ? byId.get(span.parent_span_id) : undefined;
  const seen = new Set<string>();
  while (cur && !seen.has(cur.span_id)) {
    seen.add(cur.span_id);
    d += 1;
    cur = cur.parent_span_id ? byId.get(cur.parent_span_id) : undefined;
  }
  return d;
}

function niceStep(target: number): number {
  const pow = Math.pow(10, Math.floor(Math.log10(target)));
  for (const m of [1, 2, 5, 10]) if (m * pow >= target) return m * pow;
  return 10 * pow;
}

const ROW_H = 26;
const LABEL_W = 220;
const BAR_AREA = 520;
const AXIS_H = 22;
const MIN_BAR = 6; // very short spans stay visible and selectable

export function TraceWaterfall({ spans }: { spans: SpanRow[] }) {
  const byId = useMemo(() => new Map(spans.map((s) => [s.span_id, s])), [spans]);
  const total = useMemo(
    () => Math.max(1, ...spans.map((s) => s.start_ms + s.duration_ms)),
    [spans],
  );
  const [selected, setSelected] = useState(0);
  const rowRefs = useRef<(SVGGElement | null)[]>([]);

  const x = (ms: number) => LABEL_W + (ms / total) * BAR_AREA;
  const step = niceStep(total / 5);
  const ticks: number[] = [];
  for (let t = 0; t <= total + 1e-9; t += step) ticks.push(t);

  const height = AXIS_H + spans.length * ROW_H + 8;

  const focusRow = (i: number) => {
    const clamped = Math.max(0, Math.min(spans.length - 1, i));
    setSelected(clamped);
    rowRefs.current[clamped]?.focus();
  };

  const sel = spans[selected];

  return (
    <div className="flex flex-col gap-4">
      <Legend />
      <svg
        width={LABEL_W + BAR_AREA + 8}
        height={height}
        role="group"
        aria-label="trace span timeline; use arrow keys to move between spans, Enter to inspect"
        className="border rounded-md bg-black/5"
      >
        <defs>
          <Patterns />
        </defs>
        {/* Time axis (ms) with gridlines down the rows. */}
        {ticks.map((t) => (
          <g key={`tick-${t}`} aria-hidden="true">
            <line x1={x(t)} y1={AXIS_H} x2={x(t)} y2={height} stroke="#98a2b3" strokeWidth={0.5} opacity={0.4} />
            <text x={x(t)} y={14} fontSize={10} fill="currentColor" textAnchor="middle">
              {Math.round(t)}ms
            </text>
          </g>
        ))}
        {spans.map((s, i) => {
          const cat = categorize(s);
          const c = CATS[cat];
          const depth = depthOf(s, byId);
          const bx = x(s.start_ms);
          const w = Math.max(MIN_BAR, (s.duration_ms / total) * BAR_AREA);
          const y = AXIS_H + i * ROW_H + 4;
          return (
            <g
              key={s.span_id}
              ref={(el) => {
                rowRefs.current[i] = el;
              }}
              tabIndex={0}
              role="button"
              aria-label={`${labelOf(s, cat)} (${s.name}), start ${s.start_ms.toFixed(0)}ms, duration ${s.duration_ms.toFixed(0)}ms`}
              aria-pressed={i === selected}
              onFocus={() => setSelected(i)}
              onClick={() => setSelected(i)}
              onKeyDown={(e) => {
                if (e.key === "ArrowDown") {
                  e.preventDefault();
                  focusRow(i + 1);
                } else if (e.key === "ArrowUp") {
                  e.preventDefault();
                  focusRow(i - 1);
                } else if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  setSelected(i);
                }
              }}
              style={{ cursor: "pointer" }}
            >
              <title>{s.name}</title>
              <rect x={0} y={y} width={LABEL_W + BAR_AREA} height={ROW_H} fill={i === selected ? "#2563eb22" : "transparent"} />
              <text x={8 + depth * 12} y={y + ROW_H / 2 + 4} fontSize={12} fill="currentColor">
                {truncate(labelOf(s, cat), 26 - depth * 2)}
              </text>
              <rect x={bx} y={y + 5} width={w} height={ROW_H - 12} rx={2} fill={c.color} />
              <rect x={bx} y={y + 5} width={w} height={ROW_H - 12} rx={2} fill={`url(#${c.pattern})`} />
            </g>
          );
        })}
      </svg>
      {sel && <SpanDetail span={sel} cat={categorize(sel)} />}
    </div>
  );
}

function truncate(s: string, n: number): string {
  return s.length > n ? s.slice(0, Math.max(1, n - 1)) + "…" : s;
}

function Legend() {
  return (
    <ul className="flex flex-wrap gap-3 text-xs" aria-label="legend">
      {(Object.keys(CATS) as Cat[]).map((k) => (
        <li key={k} className="flex items-center gap-1">
          <svg width="16" height="12" aria-hidden="true">
            <defs>
              <Patterns />
            </defs>
            <rect width="16" height="12" fill={CATS[k].color} />
            <rect width="16" height="12" fill={`url(#${CATS[k].pattern})`} />
          </svg>
          <span>{CATS[k].label}</span>
        </li>
      ))}
    </ul>
  );
}

// Distinct fill patterns per category (stripes / dots / crosshatch), distinguishable in mono.
function Patterns() {
  return (
    <>
      <pattern id="p-run" width="6" height="6" patternUnits="userSpaceOnUse" />
      <pattern id="p-llm" width="6" height="6" patternUnits="userSpaceOnUse">
        <rect width="6" height="6" fill="none" />
      </pattern>
      <pattern id="p-sim" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
        <line x1="0" y1="0" x2="0" y2="6" stroke="#ffffff" strokeWidth="2" />
      </pattern>
      <pattern id="p-tool" width="6" height="6" patternUnits="userSpaceOnUse">
        <circle cx="2" cy="2" r="1.2" fill="#ffffff" />
      </pattern>
      <pattern id="p-ret" width="6" height="6" patternUnits="userSpaceOnUse">
        <path d="M0 0 L6 6 M6 0 L0 6" stroke="#ffffff" strokeWidth="1" />
      </pattern>
      <pattern id="p-other" width="6" height="6" patternUnits="userSpaceOnUse">
        <line x1="0" y1="3" x2="6" y2="3" stroke="#ffffff" strokeWidth="1" />
      </pattern>
    </>
  );
}

function SpanDetail({ span, cat }: { span: SpanRow; cat: Cat }) {
  const query = span.events.find((e) => e.name.endsWith("retrieval.query"))?.content;
  return (
    <section aria-label="selected span detail" className="border rounded-md p-3 text-sm">
      <h3 className="font-semibold">
        {labelOf(span, cat)}
        <span className="font-normal opacity-60"> · {span.name}</span>
      </h3>
      <p className="opacity-70 mt-1">
        start {span.start_ms.toFixed(1)}ms · duration {span.duration_ms.toFixed(1)}ms
      </p>

      {/* Tokens & cost only for LLM calls (where they apply). */}
      {cat === "llm" && (
        <p className="opacity-70 mt-1">
          {span.input_tokens ?? 0} in / {span.output_tokens ?? 0} out tok
          {span.cache_read_tokens ? ` · cache-read ${span.cache_read_tokens}` : ""}
          {span.cache_write_tokens ? ` · cache-write ${span.cache_write_tokens}` : ""}
          {span.cost_usd != null ? ` · $${span.cost_usd.toFixed(6)}` : ""}
        </p>
      )}

      {/* Retrieval spans: the retrieved document titles/ids, not just the query. */}
      {cat === "retrieval" && (
        <div className="mt-2">
          {query && (
            <p>
              <span className="font-medium">Query:</span> {query}
            </p>
          )}
          <p className="font-medium mt-1">
            Retrieved{span.retrieval_k != null ? ` (top ${span.retrieval_k})` : ""}:
          </p>
          {span.retrieved_doc_ids.length > 0 ? (
            <ol className="list-decimal ml-5">
              {span.retrieved_doc_ids.map((d, i) => (
                <li key={i}>{d}</li>
              ))}
            </ol>
          ) : (
            <p className="opacity-60">no documents recorded</p>
          )}
        </div>
      )}

      {span.events.length > 0 && (
        <div className="mt-2">
          <p className="font-medium">Captured content</p>
          {span.events.map((ev, i) => (
            <details key={i} className="mt-1">
              <summary className="cursor-pointer">
                {ev.name}
                {ev.truncated && (
                  <span className="ml-2 px-1 rounded bg-black/20 text-xs" title="content was truncated at capture">
                    truncated
                  </span>
                )}
              </summary>
              {/* Text only — React escapes text nodes; never dangerouslySetInnerHTML. */}
              <pre className="mt-1 whitespace-pre-wrap break-words text-xs bg-black/5 p-2 rounded">
                {ev.content ?? ""}
              </pre>
            </details>
          ))}
        </div>
      )}
    </section>
  );
}
