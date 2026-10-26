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
  // eval_runs.notes (free text; the demo fixture's runs say SYNTHETIC here). Absent from older APIs.
  notes?: string | null;
}

export interface RunDetail extends RunSummary {
  n_cases: number;
  n_ok: number;
  pass_rate: number | null;
  mean_cost_usd: number | null;
  mean_latency_ms: number | null;
  // Distinct agent versions in the run. The aggregates above pool all of them.
  n_versions: number;
}

// GET /runs/{id}/versions: the versions a run holds, ordered by hash (not by role).
export interface RunVersion {
  agent_version: string; // full hash
  // false: the version only appears in results (never registered); status/cases are then null.
  registered: boolean;
  status: string | null;
  cases_total: number | null;
  cases_done: number | null;
}

export interface RunVersionsPage {
  run_id: string;
  versions: RunVersion[];
  total: number;
  limit: number;
  offset: number;
}

export interface CaseRow {
  // A multi-version run repeats each (case_id, trial) once per version; this tells them apart.
  agent_version: string;
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
  content: string | null;
  truncated: boolean;
  tool_name?: string | null;
}

export interface SpanRow {
  span_id: string;
  parent_span_id: string | null;
  name: string;
  kind: string;
  role: string | null;
  model: string | null;
  start_ms: number;
  duration_ms: number;
  input_tokens: number | null;
  output_tokens: number | null;
  cache_write_tokens: number | null;
  cache_read_tokens: number | null;
  cost_usd: number | null;
  retrieved_doc_ids: string[];
  retrieval_k: number | null;
  events: TraceEvent[];
}

export interface TraceDetail {
  trace_id: string;
  n_spans: number;
  truncated: boolean;
  spans: SpanRow[];
}

// --- Regression triage (contract: engine/TRIAGE_API.md). Non-finite numbers arrive as null. ---

export type MetricStatus = "regression" | "watch" | "ok";

export interface MetricVerdict {
  metric: string;
  gated: boolean;
  threshold: number | null;
  point_estimate: number | null;
  ci_bound: number | null;
  ci_excludes_zero: boolean;
  beyond_threshold: boolean;
  regressed: boolean;
  direction: "higher_is_better" | "lower_is_better";
  tail_prob: number | null;
  n_questions: number;
  status: MetricStatus;
}

export type CompareVerdict = "regression" | "no_regression" | "incomplete";

export interface RunSide {
  run_id: string;
  agent_version: string;
  agent_id: string;
  suite_id: string;
  suite_name: string;
  run_status: string;
  version_status: string | null;
  mode: string;
  trials_per_case: number;
  cache_mode: string | null;
  git_sha: string | null;
  versions_in_run: string[];
  version_selection: "explicit" | "single_version_fallback";
}

export interface Policy {
  alpha: number;
  method: string;
  n_boot: number;
  seed: number;
  two_level: boolean;
  thresholds: Record<string, number>;
  gated: string[];
  failed_trials: string;
  default_threshold: number;
}

export interface SideCoverage {
  cases: number;
  trials: number;
  expected_trials: number;
  ok_trials: number;
  failed_trials: number;
  statuses: Record<string, number>;
  missing_cases: string[];
  cases_short_of_trials: string[];
  cases_not_in_suite: string[];
}

export interface Coverage {
  suite_cases: number;
  paired_cases: number;
  only_in_baseline: string[];
  only_in_candidate: string[];
  baseline: SideCoverage;
  candidate: SideCoverage;
}

// Fields shared verbatim by /compare and /compare/cases.
export interface TriageSummary {
  baseline: string;
  candidate: string;
  baseline_version: string;
  candidate_version: string;
  verdict: CompareVerdict;
  complete: boolean;
  incomplete_reasons: string[];
  context: { baseline: RunSide; candidate: RunSide };
  policy: Policy;
  coverage: Coverage;
}

export interface CompareResult extends TriageSummary {
  method: string;
  alpha: number;
  paired_cases: number;
  gated_regression: boolean;
  metrics: MetricVerdict[];
  skipped_metrics: { metric: string; gated: boolean; reason: string }[];
}

export type CaseFilter = "all" | "adverse" | "unpaired" | "failed";

export interface TrialRef {
  trial: number;
  status: string;
  passed: boolean | null;
  trace_id: string | null;
  error: string | null;
}

export interface CaseSide {
  n_trials: number;
  n_ok: number;
  n_failed: number;
  statuses: Record<string, number>;
  mean: Record<string, number | null>;
  trials: TrialRef[];
}

export interface CaseDelta {
  case_id: string;
  paired: boolean;
  delta: Record<string, number | null>;
  adverse_delta: number | null;
  baseline: CaseSide;
  candidate: CaseSide;
}

export interface CompareCasesResult extends TriageSummary {
  sort_metric: string;
  sort_direction: "higher_is_better" | "lower_is_better";
  metrics_available: string[];
  filter: CaseFilter;
  total: number;
  limit: number;
  offset: number;
  cases: CaseDelta[];
}

export interface DiffSide {
  run_id: string;
  agent_id: string;
  agent_version: string;
  run_git_sha: string | null;
  version_selection: "explicit" | "single_version_fallback";
  suite_id: string;
}

export interface FieldDiff {
  changed: boolean | null;
  added?: string[];
  removed?: string[];
  modified?: string[];
}

export interface Manifest {
  model: unknown;
  git_sha: unknown;
  params: unknown;
  prompts: unknown;
  tools: unknown;
  code_ref: unknown;
  created_at: string | null;
}

export interface VersionDiff {
  baseline: DiffSide;
  candidate: DiffSide;
  same_suite: boolean;
  same_version: boolean;
  identical: boolean | null;
  missing_manifests: ("baseline" | "candidate")[];
  fields: Record<string, FieldDiff>;
  baseline_manifest: Manifest | null;
  candidate_manifest: Manifest | null;
  redaction: {
    redacted_paths: { baseline: string[]; candidate: string[] };
    truncated_paths: { baseline: string[]; candidate: string[] };
    note: string;
  };
}
