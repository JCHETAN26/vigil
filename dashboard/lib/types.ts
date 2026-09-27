// Shapes returned by the read-only dashboard API (engine/engine/api/app.py).

export interface RunSummary {
  id: string;
  suite: string;
  agent_id: string;
  mode: string;
  trials_per_case: number;
  status: string;
  cost_usd: number;
  created_at: string | null;
}

export interface RunDetail extends RunSummary {
  n_cases: number;
  n_ok: number;
  pass_rate: number | null;
  mean_cost_usd: number | null;
  mean_latency_ms: number | null;
}

export interface CaseRow {
  case_id: string;
  trial: number;
  status: string;
  passed: boolean | null;
  score: number | null;
  scores: Record<string, { passed: boolean; score: number; detail?: unknown }> | null;
  cost_usd: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  latency_ms: number | null;
  trace_id: string | null;
  tags: string[] | null;
}

export interface TraceEvent {
  name: string;
  content: string;
}

export interface SpanRow {
  span_id: string;
  parent_span_id: string | null;
  name: string;
  kind: string;
  role: string | null;
  start_ms: number;
  duration_ms: number;
  input_tokens: number | null;
  output_tokens: number | null;
  cache_write_tokens: number | null;
  cache_read_tokens: number | null;
  cost_usd: number | null;
  events: TraceEvent[];
}

export interface TraceDetail {
  trace_id: string;
  n_spans: number;
  truncated: boolean;
  spans: SpanRow[];
}

export interface MetricVerdict {
  metric: string;
  gated: boolean;
  point_estimate: number;
  ci_bound: number;
  ci_excludes_zero: boolean;
  beyond_threshold: boolean;
  regressed: boolean;
  direction: string;
  tail_prob: number;
  n_questions: number;
}

export interface CompareResult {
  baseline: string;
  candidate: string;
  method: string;
  alpha: number;
  paired_cases: number;
  gated_regression: boolean;
  metrics: MetricVerdict[];
}
