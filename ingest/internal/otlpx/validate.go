package otlpx

import (
	"fmt"

	tracepb "go.opentelemetry.io/proto/otlp/trace/v1"
)

// traceIDLen and spanIDLen are the fixed OTLP id widths in bytes.
const (
	traceIDLen = 16
	spanIDLen  = 8
)

// ValidationError describes why a payload failed structural validation. It is carried
// into the DLQ so failures are debuggable.
type ValidationError struct {
	Reason string
}

func (e *ValidationError) Error() string { return e.Reason }

// Validate checks that a per-trace TracesData is structurally sound enough to normalize
// downstream. It enforces the invariants the consumer relies on: a non-empty payload,
// 16-byte trace ids and 8-byte span ids, a non-zero start time, and end >= start when
// both are set. It deliberately does not police semantic/attribute content — that is the
// consumer's normalization concern (and its failures also route to the DLQ).
func Validate(td *tracepb.TracesData) error {
	spanCount := 0
	for _, rs := range td.GetResourceSpans() {
		for _, ss := range rs.GetScopeSpans() {
			for _, span := range ss.GetSpans() {
				spanCount++
				if n := len(span.GetTraceId()); n != traceIDLen {
					return &ValidationError{Reason: fmt.Sprintf("trace_id must be %d bytes, got %d", traceIDLen, n)}
				}
				if n := len(span.GetSpanId()); n != spanIDLen {
					return &ValidationError{Reason: fmt.Sprintf("span_id must be %d bytes, got %d", spanIDLen, n)}
				}
				if span.GetStartTimeUnixNano() == 0 {
					return &ValidationError{Reason: "start_time_unix_nano must be set"}
				}
				if end := span.GetEndTimeUnixNano(); end != 0 && end < span.GetStartTimeUnixNano() {
					return &ValidationError{Reason: "end_time_unix_nano is before start_time_unix_nano"}
				}
			}
		}
	}
	if spanCount == 0 {
		return &ValidationError{Reason: "payload contains no spans"}
	}
	return nil
}
