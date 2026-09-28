package loadgen

import (
	"testing"
	"time"

	"github.com/JCHETAN26/vigil/ingest/internal/chsink"
	"github.com/JCHETAN26/vigil/ingest/internal/pricing"
)

const (
	tidA = "0af7651916cd43dd8448eb211c80319c"
	tidB = "4bf92f3577b34da6a3ce929d0e0e4736"
)

var t0 = time.Date(2026, 9, 26, 12, 0, 0, 123456789, time.UTC)

func mustPrices(t *testing.T) *pricing.Table {
	t.Helper()
	p, err := pricing.Load()
	if err != nil {
		t.Fatal(err)
	}
	return p
}

// baseRow is a stored row as the writer produces it for an agent's root span.
func baseRow(tid, sid, parent string) chsink.Row {
	return chsink.Row{
		TraceID: tid, SpanID: sid, ParentSpanID: parent,
		StartTime: t0, EndTime: t0.Add(1500 * time.Millisecond),
		SpanName: "agent.run", SpanKind: "INTERNAL", StatusCode: "UNSET",
		ServiceName: "hotpotqa-agent", ServiceVersion: "0.1.0",
		AgentID: "hotpotqa-agent", AgentVersion: "v-abc123", GitSHA: "deadbeef",
		RunID: "run-1", RunKind: "eval", EvalRunID: "er-1", EvalCaseID: "case-7", EvalTrial: 2,
		ResourceAttributes: map[string]string{"telemetry.sdk.language": "python"},
		SpanAttributes:     map[string]string{"custom.flag": "true"},
	}
}

// withCost sets the cost the writer would compute, so round trips compare cost exactly.
func withCost(t *testing.T, r chsink.Row) chsink.Row {
	model := r.GenAIResponseModel
	if model == "" {
		model = r.GenAIRequestModel
	}
	r.CostUSD = mustPrices(t).CostWithCache(model, r.GenAIUsageInputTokens,
		r.GenAIUsageCacheCreationInputTokens, r.GenAIUsageCacheReadInputTokens, r.GenAIUsageOutputTokens)
	return r
}

func TestRoundTripIsIdentity(t *testing.T) {
	llm := baseRow(tidA, "00f067aa0ba902b7", "53995c3f42cd8ad8")
	llm.SpanName, llm.SpanKind = "chat claude-haiku-4-5", "CLIENT"
	llm.GenAISystem, llm.GenAIOperationName = "anthropic", "chat"
	llm.GenAIRequestModel, llm.GenAIResponseModel = "claude-haiku-4-5", "claude-haiku-4-5-20251001"
	llm.GenAIUsageInputTokens, llm.GenAIUsageOutputTokens = 1200, 85
	llm.GenAIUsageCacheCreationInputTokens, llm.GenAIUsageCacheReadInputTokens = 300, 4000
	llm.EventsTimestamp = []time.Time{t0.Add(time.Millisecond), t0.Add(2 * time.Millisecond)}
	llm.EventsName = []string{"gen_ai.content.prompt", "gen_ai.content.completion"}
	// Stored event attributes are always the writer's json.Marshal output: sorted keys,
	// HTML-escaped <, >, & — the canonical form the round trip must reproduce exactly.
	llm.EventsAttributes = []string{
		`{"gen_ai.prompt":"\u003cb\u003eWho\u003c/b\u003e \u0026 \"why\"?","meta":{"deep":{"x":-7},"k":"v"},"n":3,"ok":true,"score":1.5,"tags":["a",1,2.25,null]}`,
		`{}`,
	}

	trial0 := baseRow(tidA, "53995c3f42cd8ad8", "")
	trial0.EvalTrial = 0 // trial 0 is valid and distinct from "absent" (-1)

	live := baseRow(tidB, "1111111111111111", "")
	live.RunKind, live.EvalRunID, live.EvalCaseID, live.EvalTrial = "live", "", "", -1
	live.Role, live.CacheHit, live.SessionID = "user_simulator", 1, "conv-9"
	live.StatusCode, live.StatusMessage = "ERROR", "tool failed"

	unknownKind := baseRow(tidB, "2222222222222222", "1111111111111111")
	unknownKind.RunKind, unknownKind.RunID = "unknown", tidB // run_id defaults to the trace id
	unknownKind.EvalRunID, unknownKind.EvalCaseID, unknownKind.EvalTrial = "", "", -1
	unknownKind.ResourceAttributes, unknownKind.SpanAttributes = map[string]string{}, map[string]string{}
	unknownKind.ServiceVersion, unknownKind.GitSHA = "", ""

	otherResource := baseRow(tidB, "3333333333333333", "1111111111111111")
	otherResource.ServiceName = "retriever" // same trace, different resource
	otherResource.RunKind, otherResource.EvalRunID, otherResource.EvalCaseID, otherResource.EvalTrial = "live", "", "", -1

	tests := []struct {
		name string
		rows []chsink.Row
	}{
		{"llm span with tokens, cache tokens, and nested JSON events", []chsink.Row{withCost(t, llm), withCost(t, trial0)}},
		{"live span, role, cache hit, error status", []chsink.Row{withCost(t, live)}},
		{"unknown run kind with defaulted run id and empty maps", []chsink.Row{withCost(t, unknownKind)}},
		{"two resources in one trace", []chsink.Row{withCost(t, live), withCost(t, otherResource)}},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			var rep FidelityReport
			if err := CheckRoundTrip(tc.rows, mustPrices(t), &rep); err != nil {
				t.Fatal(err)
			}
			if rep.Mismatches != 0 || rep.CostMismatches != 0 || rep.Spans != len(tc.rows) {
				t.Fatalf("round trip not identity: %+v", rep)
			}
		})
	}
}

func TestDenormalizeGroupsByResource(t *testing.T) {
	a := baseRow(tidA, "1111111111111111", "")
	b := baseRow(tidA, "2222222222222222", "1111111111111111")
	c := baseRow(tidA, "3333333333333333", "1111111111111111")
	c.ServiceName = "retriever"
	td, err := Denormalize([]chsink.Row{a, b, c})
	if err != nil {
		t.Fatal(err)
	}
	if got := len(td.GetResourceSpans()); got != 2 {
		t.Fatalf("resources = %d, want 2", got)
	}
	if got := len(td.GetResourceSpans()[0].GetScopeSpans()[0].GetSpans()); got != 2 {
		t.Fatalf("first resource spans = %d, want 2", got)
	}
}

func TestDiffRowDetectsEachKindOfChange(t *testing.T) {
	base := baseRow(tidA, "1111111111111111", "")
	base.EventsTimestamp, base.EventsName, base.EventsAttributes = []time.Time{t0}, []string{"e"}, []string{`{"a":1}`}
	tests := []struct {
		name   string
		mutate func(r *chsink.Row)
	}{
		{"scalar", func(r *chsink.Row) { r.AgentVersion = "other" }},
		{"eval trial sentinel", func(r *chsink.Row) { r.EvalTrial = -1 }},
		{"time", func(r *chsink.Row) { r.EndTime = r.EndTime.Add(time.Nanosecond) }},
		{"span attribute value", func(r *chsink.Row) { r.SpanAttributes = map[string]string{"custom.flag": "false"} }},
		{"resource attribute key", func(r *chsink.Row) { r.ResourceAttributes = map[string]string{} }},
		{"event attributes", func(r *chsink.Row) { r.EventsAttributes = []string{`{"a":2}`} }},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			b := base
			b.SpanAttributes, b.ResourceAttributes = copyMap(base.SpanAttributes), copyMap(base.ResourceAttributes)
			b.EventsAttributes = append([]string(nil), base.EventsAttributes...)
			tc.mutate(&b)
			if DiffRow(&base, &b) == "" {
				t.Fatal("difference not detected")
			}
		})
	}
	same := base
	same.EventsTimestamp = nil
	same.EventsName, same.EventsAttributes = nil, nil
	emptyA, emptyB := same, same
	emptyA.SpanAttributes, emptyB.SpanAttributes = nil, map[string]string{}
	if d := DiffRow(&emptyA, &emptyB); d != "" {
		t.Fatalf("nil and empty maps should compare equal: %s", d)
	}
}

// A row written before migration 005 keeps vigil.eval.trial in span_attributes with the
// column at -1; the payload did carry it, so the round trip must match the row upgraded to
// the current schema, and count it.
func TestRoundTripUpgradesLegacyRows(t *testing.T) {
	legacy := baseRow(tidA, "1111111111111111", "")
	legacy.EvalTrial = -1
	legacy.SpanAttributes = map[string]string{"custom.flag": "true", keyEvalTrial: "1"}
	current := baseRow(tidA, "2222222222222222", "1111111111111111")

	var rep FidelityReport
	if err := CheckRoundTrip([]chsink.Row{withCost(t, legacy), withCost(t, current)}, mustPrices(t), &rep); err != nil {
		t.Fatal(err)
	}
	if rep.Mismatches != 0 || rep.LegacyUpgraded != 1 {
		t.Fatalf("got %+v", rep)
	}
	up, changed := UpgradeLegacy(legacy)
	if !changed || up.EvalTrial != 1 || up.SpanAttributes[keyEvalTrial] != "" || legacy.SpanAttributes[keyEvalTrial] != "1" {
		t.Fatalf("upgrade must lift the attribute without mutating the input: %+v", up)
	}
	if _, changed := UpgradeLegacy(current); changed {
		t.Fatal("a current row must not change")
	}
}
