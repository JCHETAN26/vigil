// Package chsink writes normalized span rows to ClickHouse. Inserts carry the two
// settings that make redelivered batches idempotent all the way through the on-insert
// rollup materialized views (see the design doc §3.5 and §3.3 correctness note).
package chsink

import "time"

// Row is one span, matching the insertable columns of the `spans` table. The MATERIALIZED
// columns (duration_ns, gen_ai_usage_total_tokens) and the DEFAULT column (ingested_at)
// are intentionally omitted — ClickHouse fills them.
type Row struct {
	TraceID      string
	SpanID       string
	ParentSpanID string
	TraceState   string

	StartTime time.Time
	EndTime   time.Time

	SpanName      string
	SpanKind      string // Enum8 name: UNSPECIFIED|INTERNAL|SERVER|CLIENT|PRODUCER|CONSUMER
	StatusCode    string // Enum8 name: UNSET|OK|ERROR
	StatusMessage string

	ServiceName    string
	ServiceVersion string

	AgentID      string
	AgentVersion string
	GitSHA       string
	RunID        string
	RunKind      string // Enum8 name: unknown|live|eval
	EvalRunID    string
	EvalCaseID   string
	EvalTrial    int32 // 0-based repeat index within an eval run; -1 = not an eval trial
	SessionID    string
	Role         string // vigil.role, e.g. "user_simulator"; "" = the agent itself

	GenAISystem            string
	GenAIOperationName     string
	GenAIRequestModel      string
	GenAIResponseModel     string
	GenAIUsageInputTokens  uint32
	GenAIUsageOutputTokens uint32
	CostUSD                float64
	CacheHit               uint8 // 1 if this span was served from the dev LLM cache, else 0

	ResourceAttributes map[string]string
	SpanAttributes     map[string]string

	EventsTimestamp  []time.Time
	EventsName       []string
	EventsAttributes []string
}

// insertColumns is the explicit column list for INSERT INTO spans, in the exact order
// Row fields are appended in sink.go. Nested events flatten to events.timestamp/name/
// attributes.
const insertColumns = `trace_id, span_id, parent_span_id, trace_state,
start_time, end_time,
span_name, span_kind, status_code, status_message,
service_name, service_version,
agent_id, agent_version, git_sha, run_id, run_kind, eval_run_id, eval_case_id, eval_trial, session_id, role,
gen_ai_system, gen_ai_operation_name, gen_ai_request_model, gen_ai_response_model,
gen_ai_usage_input_tokens, gen_ai_usage_output_tokens, cost_usd, cache_hit,
resource_attributes, span_attributes,
` + "`events.timestamp`, `events.name`, `events.attributes`"
