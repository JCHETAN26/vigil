package receiver

import (
	"context"
	"io"
	"log/slog"
	"testing"

	collectortracepb "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	tracepb "go.opentelemetry.io/proto/otlp/trace/v1"
)

// fakeSink records what the handler routes, for assertions.
type fakeSink struct {
	raw []string // trace ids sent to the raw topic
	dlq []dlqCall
}

type dlqCall struct {
	traceID string
	stage   string
	reason  string
	payload int // payload length
}

func (f *fakeSink) Raw(_ context.Context, traceID string, _ []byte) error {
	f.raw = append(f.raw, traceID)
	return nil
}

func (f *fakeSink) DLQ(_ context.Context, traceID string, payload []byte, stage, reason string) error {
	f.dlq = append(f.dlq, dlqCall{traceID: traceID, stage: stage, reason: reason, payload: len(payload)})
	return nil
}

func testLogger() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

func mkSpan(traceByte, spanByte byte, start, end uint64) *tracepb.Span {
	return &tracepb.Span{
		TraceId:           append([]byte{traceByte}, make([]byte, 15)...),
		SpanId:            append([]byte{spanByte}, make([]byte, 7)...),
		StartTimeUnixNano: start,
		EndTimeUnixNano:   end,
	}
}

func TestProcess_RoutesValidAndInvalid(t *testing.T) {
	// Two traces: 0x01 is valid; 0x02 has an end-before-start span (invalid).
	req := &collectortracepb.ExportTraceServiceRequest{
		ResourceSpans: []*tracepb.ResourceSpans{{
			ScopeSpans: []*tracepb.ScopeSpans{{
				Spans: []*tracepb.Span{
					mkSpan(1, 1, 10, 20),
					mkSpan(2, 1, 20, 10), // invalid
				},
			}},
		}},
	}

	sink := &fakeSink{}
	h := NewHandler(sink, testLogger())

	res, err := h.Process(context.Background(), req)
	if err != nil {
		t.Fatalf("Process() error: %v", err)
	}

	if len(sink.raw) != 1 {
		t.Fatalf("expected 1 raw publish, got %d", len(sink.raw))
	}
	if res.AcceptedSpans != 1 {
		t.Errorf("AcceptedSpans = %d, want 1", res.AcceptedSpans)
	}
	if len(sink.dlq) != 1 {
		t.Fatalf("expected 1 dlq publish, got %d", len(sink.dlq))
	}
	if res.RejectedSpans != 1 {
		t.Errorf("RejectedSpans = %d, want 1", res.RejectedSpans)
	}
	if sink.dlq[0].stage != StageValidate {
		t.Errorf("dlq stage = %q, want %q", sink.dlq[0].stage, StageValidate)
	}
	if sink.dlq[0].payload == 0 {
		t.Errorf("dlq payload should carry the original per-trace bytes, got 0 length")
	}
}

func TestHandleParseFailure_RoutesToDLQ(t *testing.T) {
	sink := &fakeSink{}
	h := NewHandler(sink, testLogger())

	raw := []byte{0xde, 0xad, 0xbe, 0xef}
	if err := h.HandleParseFailure(context.Background(), raw, "bad wire format"); err != nil {
		t.Fatalf("HandleParseFailure() error: %v", err)
	}
	if len(sink.dlq) != 1 {
		t.Fatalf("expected 1 dlq publish, got %d", len(sink.dlq))
	}
	if sink.dlq[0].stage != StageParse {
		t.Errorf("dlq stage = %q, want %q", sink.dlq[0].stage, StageParse)
	}
	if sink.dlq[0].traceID != "" {
		t.Errorf("parse-failure trace id should be empty, got %q", sink.dlq[0].traceID)
	}
	if sink.dlq[0].payload != len(raw) {
		t.Errorf("parse-failure payload = %d bytes, want %d (original bytes preserved)", sink.dlq[0].payload, len(raw))
	}
}
