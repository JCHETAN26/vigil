// Package otlpx holds pure helpers for working with OTLP trace payloads: splitting an
// export request into per-trace payloads and validating span structure. Nothing here
// does I/O, so it is fully unit-testable.
package otlpx

import (
	"encoding/hex"
	"sort"

	collectortracepb "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	tracepb "go.opentelemetry.io/proto/otlp/trace/v1"
)

// TraceGroup is one trace's spans packaged as a standalone OTLP TracesData, ready to be
// published as a single keyed record.
type TraceGroup struct {
	TraceID string // lowercase hex of the 16-byte trace id ("" if a span had no id)
	Data    *tracepb.TracesData
}

// SplitByTraceID regroups an export request so that each returned group contains exactly
// one trace's spans, preserving the original Resource/Scope structure (and schema URLs)
// for every span. This honors the topic design (§4): all spans of a trace land on one
// partition, and each record is itself valid OTLP, so it stays replayable.
//
// Groups are returned sorted by TraceID for deterministic output. Spans whose trace id
// is not 16 bytes are grouped under the hex of whatever bytes they carry (possibly "");
// structural validation (Validate) is applied per group by the caller and routes such
// groups to the DLQ.
func SplitByTraceID(req *collectortracepb.ExportTraceServiceRequest) []TraceGroup {
	if req == nil {
		return nil
	}

	// Per-trace builders, plus maps from the original Resource/Scope pointers to their
	// clones within that trace's TracesData, so structure is rebuilt without duplication.
	type builder struct {
		data *tracepb.TracesData
		rsBy map[*tracepb.ResourceSpans]*tracepb.ResourceSpans
		ssBy map[*tracepb.ScopeSpans]*tracepb.ScopeSpans
	}
	builders := map[string]*builder{}

	for _, rs := range req.GetResourceSpans() {
		for _, ss := range rs.GetScopeSpans() {
			for _, span := range ss.GetSpans() {
				tid := hex.EncodeToString(span.GetTraceId())

				b := builders[tid]
				if b == nil {
					b = &builder{
						data: &tracepb.TracesData{},
						rsBy: map[*tracepb.ResourceSpans]*tracepb.ResourceSpans{},
						ssBy: map[*tracepb.ScopeSpans]*tracepb.ScopeSpans{},
					}
					builders[tid] = b
				}

				rsClone := b.rsBy[rs]
				if rsClone == nil {
					rsClone = &tracepb.ResourceSpans{
						Resource:  rs.GetResource(),
						SchemaUrl: rs.GetSchemaUrl(),
					}
					b.rsBy[rs] = rsClone
					b.data.ResourceSpans = append(b.data.ResourceSpans, rsClone)
				}

				ssClone := b.ssBy[ss]
				if ssClone == nil {
					ssClone = &tracepb.ScopeSpans{
						Scope:     ss.GetScope(),
						SchemaUrl: ss.GetSchemaUrl(),
					}
					b.ssBy[ss] = ssClone
					rsClone.ScopeSpans = append(rsClone.ScopeSpans, ssClone)
				}

				ssClone.Spans = append(ssClone.Spans, span)
			}
		}
	}

	out := make([]TraceGroup, 0, len(builders))
	for tid, b := range builders {
		out = append(out, TraceGroup{TraceID: tid, Data: b.data})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].TraceID < out[j].TraceID })
	return out
}
