package normalize

import (
	"encoding/hex"
	"encoding/json"
	"testing"

	commonpb "go.opentelemetry.io/proto/otlp/common/v1"
	resourcepb "go.opentelemetry.io/proto/otlp/resource/v1"
	tracepb "go.opentelemetry.io/proto/otlp/trace/v1"

	"github.com/JCHETAN26/vigil/ingest/internal/pricing"
)

func kvStr(k, v string) *commonpb.KeyValue {
	return &commonpb.KeyValue{Key: k, Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: v}}}
}
func kvInt(k string, v int64) *commonpb.KeyValue {
	return &commonpb.KeyValue{Key: k, Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_IntValue{IntValue: v}}}
}

func TestNormalizePromotesAndComputesCost(t *testing.T) {
	prices, err := pricing.Load()
	if err != nil {
		t.Fatalf("pricing: %v", err)
	}
	traceID := append([]byte{0xaa}, make([]byte, 15)...)
	spanID := append([]byte{0x01}, make([]byte, 7)...)

	td := &tracepb.TracesData{ResourceSpans: []*tracepb.ResourceSpans{{
		Resource: &resourcepb.Resource{Attributes: []*commonpb.KeyValue{
			kvStr("service.name", "support-agent"),
			kvStr("vigil.agent.id", "tau-bench-retail"),
			kvStr("vigil.agent.version", "hash123"),
			kvStr("deployment.environment", "prod"), // not promoted → tail
		}},
		ScopeSpans: []*tracepb.ScopeSpans{{
			Spans: []*tracepb.Span{{
				TraceId:           traceID,
				SpanId:            spanID,
				Name:              "chat",
				Kind:              tracepb.Span_SPAN_KIND_CLIENT,
				StartTimeUnixNano: 1000,
				EndTimeUnixNano:   2000,
				Status:            &tracepb.Status{Code: tracepb.Status_STATUS_CODE_ERROR, Message: "boom"},
				Attributes: []*commonpb.KeyValue{
					kvStr("vigil.run.kind", "eval"),
					kvStr("gen_ai.response.model", "claude-haiku-4-5"),
					kvInt("gen_ai.usage.input_tokens", 1_000_000),
					kvInt("gen_ai.usage.output_tokens", 1_000_000),
					kvStr("vigil.eval.dataset", "hotpotqa@v1"), // not promoted → tail
				},
				Events: []*tracepb.Span_Event{{
					TimeUnixNano: 1500,
					Name:         "gen_ai.content.completion",
					Attributes:   []*commonpb.KeyValue{kvStr("role", "assistant")},
				}},
			}},
		}},
	}}}

	rows, err := Normalize(td, prices)
	if err != nil {
		t.Fatalf("Normalize: %v", err)
	}
	if len(rows) != 1 {
		t.Fatalf("got %d rows, want 1", len(rows))
	}
	r := rows[0]

	if r.TraceID != hex.EncodeToString(traceID) {
		t.Errorf("TraceID = %q", r.TraceID)
	}
	if r.RunID != hex.EncodeToString(traceID) {
		t.Errorf("RunID should default to trace_id, got %q", r.RunID)
	}
	if r.RunKind != "eval" {
		t.Errorf("RunKind = %q, want eval", r.RunKind)
	}
	if r.ServiceName != "support-agent" || r.AgentID != "tau-bench-retail" || r.AgentVersion != "hash123" {
		t.Errorf("resource promotion wrong: %+v", r)
	}
	if r.SpanKind != "CLIENT" {
		t.Errorf("SpanKind = %q, want CLIENT", r.SpanKind)
	}
	if r.StatusCode != "ERROR" || r.StatusMessage != "boom" {
		t.Errorf("status = %q/%q", r.StatusCode, r.StatusMessage)
	}
	// haiku 4.5: 1M in ($1) + 1M out ($5) = $6.
	if r.CostUSD != 6.0 {
		t.Errorf("CostUSD = %v, want 6.0", r.CostUSD)
	}
	if r.GenAIUsageInputTokens != 1_000_000 || r.GenAIUsageOutputTokens != 1_000_000 {
		t.Errorf("token counts wrong: in=%d out=%d", r.GenAIUsageInputTokens, r.GenAIUsageOutputTokens)
	}

	// Promoted keys must be removed from the tail; non-promoted keys must remain.
	if _, ok := r.ResourceAttributes["service.name"]; ok {
		t.Error("service.name should be promoted out of the resource tail")
	}
	if r.ResourceAttributes["deployment.environment"] != "prod" {
		t.Error("deployment.environment should remain in the resource tail")
	}
	if _, ok := r.SpanAttributes["gen_ai.response.model"]; ok {
		t.Error("gen_ai.response.model should be promoted out of the span tail")
	}
	if r.SpanAttributes["vigil.eval.dataset"] != "hotpotqa@v1" {
		t.Error("vigil.eval.dataset should remain in the span tail")
	}

	// Events flattened into parallel arrays; attributes JSON-encoded.
	if len(r.EventsName) != 1 || r.EventsName[0] != "gen_ai.content.completion" {
		t.Errorf("events name = %v", r.EventsName)
	}
	var evAttrs map[string]any
	if err := json.Unmarshal([]byte(r.EventsAttributes[0]), &evAttrs); err != nil {
		t.Fatalf("event attributes not valid JSON: %v", err)
	}
	if evAttrs["role"] != "assistant" {
		t.Errorf("event attribute role = %v", evAttrs["role"])
	}
}

func TestNormalizeRunKindDefaultAndUnmarshalError(t *testing.T) {
	prices, _ := pricing.Load()

	// Unparseable bytes → error (routed to DLQ by the writer).
	if _, err := FromOTLP([]byte{0xff, 0xff}, prices); err == nil {
		t.Error("expected error for unparseable OTLP")
	}

	// Missing vigil.run.kind → "unknown".
	td := &tracepb.TracesData{ResourceSpans: []*tracepb.ResourceSpans{{
		ScopeSpans: []*tracepb.ScopeSpans{{
			Spans: []*tracepb.Span{{
				TraceId:           append([]byte{1}, make([]byte, 15)...),
				SpanId:            append([]byte{1}, make([]byte, 7)...),
				StartTimeUnixNano: 1,
				EndTimeUnixNano:   2,
			}},
		}},
	}}}
	rows, err := Normalize(td, prices)
	if err != nil {
		t.Fatalf("Normalize: %v", err)
	}
	if rows[0].RunKind != "unknown" {
		t.Errorf("RunKind = %q, want unknown", rows[0].RunKind)
	}
	if rows[0].CostUSD != 0 {
		t.Errorf("CostUSD with no model = %v, want 0", rows[0].CostUSD)
	}
}
