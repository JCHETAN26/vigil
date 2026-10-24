"use client";

// Offset pagination for endpoints that cap `limit` server-side. When the API reports no total,
// callers fetch `limit + 1` rows and pass `hasNext` from whether the extra row came back.
export function Pager({
  offset,
  limit,
  shown,
  hasNext,
  total,
  onChange,
  label,
}: {
  offset: number;
  limit: number;
  shown: number;
  hasNext: boolean;
  total?: number | null;
  onChange: (offset: number) => void;
  label: string;
}) {
  if (offset === 0 && !hasNext) return null;
  const range = shown === 0 ? "none" : `${offset + 1}–${offset + shown}`;
  return (
    <nav aria-label={label} className="flex items-center gap-3 text-sm">
      <button
        type="button"
        className="border rounded px-3 py-1 disabled:opacity-40"
        disabled={offset === 0}
        onClick={() => onChange(Math.max(0, offset - limit))}
      >
        ← Previous
      </button>
      <span aria-live="polite">
        {range}
        {total != null ? ` of ${total}` : ""}
      </span>
      <button
        type="button"
        className="border rounded px-3 py-1 disabled:opacity-40"
        disabled={!hasNext}
        onClick={() => onChange(offset + limit)}
      >
        Next →
      </button>
    </nav>
  );
}

// Parse a non-negative integer offset from a query-string value; anything else is 0.
export function parseOffset(raw: string | null): number {
  const n = Number(raw);
  return Number.isInteger(n) && n > 0 ? n : 0;
}
