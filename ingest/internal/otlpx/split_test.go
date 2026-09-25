package otlpx

import (
	"encoding/hex"
	"testing"

	collectortracepb "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	commonpb "go.opentelemetry.io/proto/otlp/common/v1"
	resourcepb "go.opentelemetry.io/proto/otlp/resource/v1"
	tracepb "go.opentelemetry.io/proto/otlp/trace/v1"
)

func tid(b byte) []byte { return append([]byte{b}, make([]byte, 15)...) }
func sid(b byte) []byte { return append([]byte{b}, make([]byte, 7)...) }

func span(traceByte, spanByte byte, name string) *tracepb.Span {
	return &tracepb.Span{
		TraceId:           tid(traceByte),
		SpanId:            sid(spanByte),
		Name:              name,
		StartTimeUnixNano: 1,
		EndTimeUnixNano:   2,
	}
}

func res(service string) *resourcepb.Resource {
	return &resourcepb.Resource{Attributes: []*commonpb.KeyValue{{
		Key:   "service.name",
		Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: service}},
	}}}
}

func TestSplitByTraceID(t *testing.T) {
	// One request, one ResourceSpans, two scopes; spans across three trace ids, with
	// trace 0x01 appearing in both scopes to check per-scope regrouping.
	req := &collectortracepb.ExportTraceServiceRequest{
		ResourceSpans: []*tracepb.ResourceSpans{{
			Resource: res("agent-a"),
			ScopeSpans: []*tracepb.ScopeSpans{
				{
					Scope: &commonpb.InstrumentationScope{Name: "scope-1"},
					Spans: []*tracepb.Span{span(1, 1, "a"), span(2, 1, "b"), span(1, 2, "c")},
				},
				{
					Scope: &commonpb.InstrumentationScope{Name: "scope-2"},
					Spans: []*tracepb.Span{span(3, 1, "d"), span(1, 3, "e")},
				},
			},
		}},
	}

	groups := SplitByTraceID(req)

	if len(groups) != 3 {
		t.Fatalf("expected 3 trace groups, got %d", len(groups))
	}

	// Groups are sorted by trace id hex: 0x01.., 0x02.., 0x03..
	wantIDs := []string{hex.EncodeToString(tid(1)), hex.EncodeToString(tid(2)), hex.EncodeToString(tid(3))}
	for i, g := range groups {
		if g.TraceID != wantIDs[i] {
			t.Errorf("group %d: trace_id = %s, want %s", i, g.TraceID, wantIDs[i])
		}
	}

	// Trace 0x01 has 3 spans spread over two scopes; both scopes must be preserved.
	g1 := groups[0]
	total, scopes := 0, 0
	for _, rs := range g1.Data.GetResourceSpans() {
		if got := rs.GetResource().GetAttributes()[0].GetValue().GetStringValue(); got != "agent-a" {
			t.Errorf("resource not preserved: got %q", got)
		}
		for _, ss := range rs.GetScopeSpans() {
			scopes++
			total += len(ss.GetSpans())
		}
	}
	if total != 3 {
		t.Errorf("trace 0x01: got %d spans, want 3", total)
	}
	if scopes != 2 {
		t.Errorf("trace 0x01: got %d scopes, want 2", scopes)
	}

	// Traces 0x02 and 0x03 have exactly one span each.
	for _, g := range groups[1:] {
		n := 0
		for _, rs := range g.Data.GetResourceSpans() {
			for _, ss := range rs.GetScopeSpans() {
				n += len(ss.GetSpans())
			}
		}
		if n != 1 {
			t.Errorf("trace %s: got %d spans, want 1", g.TraceID, n)
		}
	}
}

func TestSplitByTraceID_Empty(t *testing.T) {
	if g := SplitByTraceID(nil); g != nil {
		t.Errorf("nil request: got %v, want nil", g)
	}
	if g := SplitByTraceID(&collectortracepb.ExportTraceServiceRequest{}); len(g) != 0 {
		t.Errorf("empty request: got %d groups, want 0", len(g))
	}
}
