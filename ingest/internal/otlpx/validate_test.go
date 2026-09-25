package otlpx

import (
	"testing"

	tracepb "go.opentelemetry.io/proto/otlp/trace/v1"
)

func wrap(spans ...*tracepb.Span) *tracepb.TracesData {
	return &tracepb.TracesData{ResourceSpans: []*tracepb.ResourceSpans{{
		ScopeSpans: []*tracepb.ScopeSpans{{Spans: spans}},
	}}}
}

func TestValidate(t *testing.T) {
	tests := []struct {
		name    string
		td      *tracepb.TracesData
		wantErr bool
	}{
		{
			name:    "valid",
			td:      wrap(&tracepb.Span{TraceId: tid(1), SpanId: sid(1), StartTimeUnixNano: 1, EndTimeUnixNano: 2}),
			wantErr: false,
		},
		{
			name:    "end equal to start is allowed",
			td:      wrap(&tracepb.Span{TraceId: tid(1), SpanId: sid(1), StartTimeUnixNano: 5, EndTimeUnixNano: 5}),
			wantErr: false,
		},
		{
			name:    "zero end time is allowed",
			td:      wrap(&tracepb.Span{TraceId: tid(1), SpanId: sid(1), StartTimeUnixNano: 5, EndTimeUnixNano: 0}),
			wantErr: false,
		},
		{
			name:    "short trace id",
			td:      wrap(&tracepb.Span{TraceId: []byte{1, 2, 3}, SpanId: sid(1), StartTimeUnixNano: 1}),
			wantErr: true,
		},
		{
			name:    "short span id",
			td:      wrap(&tracepb.Span{TraceId: tid(1), SpanId: []byte{1}, StartTimeUnixNano: 1}),
			wantErr: true,
		},
		{
			name:    "zero start time",
			td:      wrap(&tracepb.Span{TraceId: tid(1), SpanId: sid(1), StartTimeUnixNano: 0}),
			wantErr: true,
		},
		{
			name:    "end before start",
			td:      wrap(&tracepb.Span{TraceId: tid(1), SpanId: sid(1), StartTimeUnixNano: 10, EndTimeUnixNano: 5}),
			wantErr: true,
		},
		{
			name:    "no spans",
			td:      &tracepb.TracesData{},
			wantErr: true,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := Validate(tt.td)
			if (err != nil) != tt.wantErr {
				t.Fatalf("Validate() err = %v, wantErr = %v", err, tt.wantErr)
			}
		})
	}
}
