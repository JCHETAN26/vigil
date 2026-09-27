import type { MetricVerdict } from "@/lib/types";

export type Verdict = "regression" | "watch" | "ok";

export function verdictOf(m: MetricVerdict): Verdict {
  if (m.regressed) return "regression";
  if (m.ci_excludes_zero) return "watch"; // statistically real but below the practical threshold
  return "ok";
}

// A badge that never relies on color alone: each verdict carries a distinct icon/shape AND a
// text label, so it is legible to color-blind users and in monochrome.
const STYLES: Record<Verdict, { label: string; icon: string; cls: string }> = {
  regression: { label: "REGRESSION", icon: "▲", cls: "bg-regression text-white" },
  watch: { label: "watch", icon: "◆", cls: "bg-watch text-white" },
  ok: { label: "ok", icon: "✓", cls: "bg-ok text-white" },
};

export function VerdictBadge({ verdict }: { verdict: Verdict }) {
  const s = STYLES[verdict];
  return (
    <span
      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-semibold ${s.cls}`}
      aria-label={`verdict: ${s.label}`}
    >
      <span aria-hidden="true">{s.icon}</span>
      {s.label}
    </span>
  );
}
