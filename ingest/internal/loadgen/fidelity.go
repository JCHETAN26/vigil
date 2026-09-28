package loadgen

import (
	"fmt"
	"math"
	"sort"
	"strconv"

	"github.com/JCHETAN26/vigil/ingest/internal/chsink"
	"github.com/JCHETAN26/vigil/ingest/internal/normalize"
	"github.com/JCHETAN26/vigil/ingest/internal/pricing"
)

// FidelityReport is the result of round-tripping stored rows through Denormalize and the
// production normalizer. Mismatches must be zero for a template set to be used. Cost is
// reported separately: it is computed by the writer from the current price table, not
// carried in the OTLP payload, so a price change since ingestion does not affect the
// payload's fidelity.
//
// Rows written before a column was promoted (migrations 005/007) still hold that attribute
// in span_attributes with the column at its default. The recorded payload did carry the
// attribute, so such rows are compared after upgrading them to the current schema
// (UpgradeLegacy) and counted in LegacyUpgraded rather than hidden.
type FidelityReport struct {
	Traces         int      `json:"traces"`
	Spans          int      `json:"spans"`
	Mismatches     int      `json:"mismatches"`
	CostMismatches int      `json:"cost_mismatches"`
	LegacyUpgraded int      `json:"legacy_rows_upgraded"`
	Examples       []string `json:"examples,omitempty"` // first few mismatch descriptions
}

// UpgradeLegacy returns row as the current writer would store it: attributes that later
// migrations promoted to columns are lifted out of the span_attributes tail when the
// column still holds its default. It reports whether anything changed.
func UpgradeLegacy(row chsink.Row) (chsink.Row, bool) {
	lift := []struct {
		key   string
		unset func(r *chsink.Row) bool
		apply func(r *chsink.Row, v string)
	}{
		{keyEvalTrial, func(r *chsink.Row) bool { return r.EvalTrial == -1 },
			func(r *chsink.Row, v string) { r.EvalTrial = parseInt32(v, -1) }},
		{keyRole, func(r *chsink.Row) bool { return r.Role == "" },
			func(r *chsink.Row, v string) { r.Role = v }},
		{keyCacheHit, func(r *chsink.Row) bool { return r.CacheHit == 0 },
			func(r *chsink.Row, v string) { r.CacheHit = parseBool01(v) }},
		{keyCacheWrite, func(r *chsink.Row) bool { return r.GenAIUsageCacheCreationInputTokens == 0 },
			func(r *chsink.Row, v string) { r.GenAIUsageCacheCreationInputTokens = parseUint32(v) }},
		{keyCacheRead, func(r *chsink.Row) bool { return r.GenAIUsageCacheReadInputTokens == 0 },
			func(r *chsink.Row, v string) { r.GenAIUsageCacheReadInputTokens = parseUint32(v) }},
	}
	changed := false
	for _, l := range lift {
		v, ok := row.SpanAttributes[l.key]
		if !ok || !l.unset(&row) {
			continue
		}
		if !changed {
			row.SpanAttributes = copyMap(row.SpanAttributes)
			changed = true
		}
		delete(row.SpanAttributes, l.key)
		l.apply(&row, v)
	}
	return row, changed
}

func parseInt32(s string, def int32) int32 {
	n, err := strconv.ParseInt(s, 10, 32)
	if err != nil {
		return def
	}
	return int32(n)
}

func parseUint32(s string) uint32 {
	n, err := strconv.ParseUint(s, 10, 32)
	if err != nil {
		return 0
	}
	return uint32(n)
}

func parseBool01(s string) uint8 {
	if s == "true" || s == "1" {
		return 1
	}
	return 0
}

// CheckRoundTrip denormalizes one trace's rows, runs the result through
// normalize.Normalize, and compares every field against the originals, accumulating into
// rep. Rows are matched by span id.
func CheckRoundTrip(rows []chsink.Row, prices *pricing.Table, rep *FidelityReport) error {
	td, err := Denormalize(rows)
	if err != nil {
		return err
	}
	got, err := normalize.Normalize(td, prices)
	if err != nil {
		return err
	}
	rep.Traces++
	rep.Spans += len(rows)
	if len(got) != len(rows) {
		rep.Mismatches += len(rows)
		rep.note(fmt.Sprintf("trace %s: %d rows in, %d out", rows[0].TraceID, len(rows), len(got)))
		return nil
	}
	bySpan := make(map[string]*chsink.Row, len(got))
	for i := range got {
		bySpan[got[i].SpanID] = &got[i]
	}
	for i := range rows {
		upgraded, legacy := UpgradeLegacy(rows[i])
		if legacy {
			rep.LegacyUpgraded++
		}
		want := &upgraded
		g := bySpan[want.SpanID]
		if g == nil {
			rep.Mismatches++
			rep.note(fmt.Sprintf("span %s/%s missing after round trip", want.TraceID, want.SpanID))
			continue
		}
		if diff := DiffRow(want, g); diff != "" {
			rep.Mismatches++
			rep.note(fmt.Sprintf("span %s/%s: %s", want.TraceID, want.SpanID, diff))
		}
		if !costEqual(want.CostUSD, g.CostUSD) {
			rep.CostMismatches++
		}
	}
	return nil
}

func (r *FidelityReport) note(s string) {
	if len(r.Examples) < 5 {
		r.Examples = append(r.Examples, s)
	}
}

func costEqual(a, b float64) bool {
	return math.Abs(a-b) <= 1e-12*math.Max(1, math.Max(math.Abs(a), math.Abs(b)))
}

// DiffRow compares every payload-derived field of two rows (everything but CostUSD) and
// returns a description of the first difference, or "" if equal. Nil and empty maps/slices
// compare equal; times compare by instant.
func DiffRow(a, b *chsink.Row) string {
	type field struct {
		name string
		x, y any
	}
	scalars := []field{
		{"trace_id", a.TraceID, b.TraceID}, {"span_id", a.SpanID, b.SpanID},
		{"parent_span_id", a.ParentSpanID, b.ParentSpanID}, {"trace_state", a.TraceState, b.TraceState},
		{"span_name", a.SpanName, b.SpanName}, {"span_kind", a.SpanKind, b.SpanKind},
		{"status_code", a.StatusCode, b.StatusCode}, {"status_message", a.StatusMessage, b.StatusMessage},
		{"service_name", a.ServiceName, b.ServiceName}, {"service_version", a.ServiceVersion, b.ServiceVersion},
		{"agent_id", a.AgentID, b.AgentID}, {"agent_version", a.AgentVersion, b.AgentVersion},
		{"git_sha", a.GitSHA, b.GitSHA}, {"run_id", a.RunID, b.RunID}, {"run_kind", a.RunKind, b.RunKind},
		{"eval_run_id", a.EvalRunID, b.EvalRunID}, {"eval_case_id", a.EvalCaseID, b.EvalCaseID},
		{"eval_trial", a.EvalTrial, b.EvalTrial}, {"session_id", a.SessionID, b.SessionID},
		{"role", a.Role, b.Role}, {"cache_hit", a.CacheHit, b.CacheHit},
		{"gen_ai_system", a.GenAISystem, b.GenAISystem},
		{"gen_ai_operation_name", a.GenAIOperationName, b.GenAIOperationName},
		{"gen_ai_request_model", a.GenAIRequestModel, b.GenAIRequestModel},
		{"gen_ai_response_model", a.GenAIResponseModel, b.GenAIResponseModel},
		{"input_tokens", a.GenAIUsageInputTokens, b.GenAIUsageInputTokens},
		{"output_tokens", a.GenAIUsageOutputTokens, b.GenAIUsageOutputTokens},
		{"cache_creation_tokens", a.GenAIUsageCacheCreationInputTokens, b.GenAIUsageCacheCreationInputTokens},
		{"cache_read_tokens", a.GenAIUsageCacheReadInputTokens, b.GenAIUsageCacheReadInputTokens},
	}
	for _, f := range scalars {
		if f.x != f.y {
			return fmt.Sprintf("%s: %v != %v", f.name, f.x, f.y)
		}
	}
	if !a.StartTime.Equal(b.StartTime) {
		return fmt.Sprintf("start_time: %v != %v", a.StartTime, b.StartTime)
	}
	if !a.EndTime.Equal(b.EndTime) {
		return fmt.Sprintf("end_time: %v != %v", a.EndTime, b.EndTime)
	}
	if d := diffMap(a.ResourceAttributes, b.ResourceAttributes); d != "" {
		return "resource_attributes: " + d
	}
	if d := diffMap(a.SpanAttributes, b.SpanAttributes); d != "" {
		return "span_attributes: " + d
	}
	if len(a.EventsName) != len(b.EventsName) || len(a.EventsTimestamp) != len(b.EventsTimestamp) ||
		len(a.EventsAttributes) != len(b.EventsAttributes) {
		return fmt.Sprintf("events: %d != %d", len(a.EventsName), len(b.EventsName))
	}
	for i := range a.EventsName {
		switch {
		case a.EventsName[i] != b.EventsName[i]:
			return fmt.Sprintf("event %d name: %q != %q", i, a.EventsName[i], b.EventsName[i])
		case !a.EventsTimestamp[i].Equal(b.EventsTimestamp[i]):
			return fmt.Sprintf("event %d time: %v != %v", i, a.EventsTimestamp[i], b.EventsTimestamp[i])
		case a.EventsAttributes[i] != b.EventsAttributes[i]:
			return fmt.Sprintf("event %d attributes: %s != %s", i, a.EventsAttributes[i], b.EventsAttributes[i])
		}
	}
	return ""
}

func diffMap(a, b map[string]string) string {
	if len(a) != len(b) {
		return fmt.Sprintf("%d keys != %d keys (%v vs %v)", len(a), len(b), keysOf(a), keysOf(b))
	}
	for k, v := range a {
		if w, ok := b[k]; !ok || w != v {
			return fmt.Sprintf("key %q differs", k)
		}
	}
	return ""
}

func keysOf(m map[string]string) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
