// Package receiver turns incoming OTLP export requests into keyed Redpanda records:
// split by trace id, validated, and routed to the raw topic or the DLQ. The transport
// adapters (gRPC, HTTP) are thin wrappers over Handler, which is I/O-free apart from the
// Sink it writes to and is therefore unit-testable with a fake sink.
package receiver

import (
	"context"
	"log/slog"

	collectortracepb "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	"google.golang.org/protobuf/proto"

	"github.com/JCHETAN26/vigil/ingest/internal/otlpx"
)

// Sink is where the handler writes results. Raw carries a valid per-trace OTLP payload;
// DLQ carries a payload that failed parsing or validation, with a stage and reason.
type Sink interface {
	Raw(ctx context.Context, traceID string, payload []byte) error
	DLQ(ctx context.Context, traceID string, payload []byte, stage, reason string) error
}

// DLQ stages.
const (
	StageParse    = "parse"    // request body could not be unmarshaled as OTLP
	StageValidate = "validate" // a per-trace payload failed structural validation
)

// Result summarizes what happened to a request, for the OTLP response and logging.
type Result struct {
	AcceptedSpans int
	RejectedSpans int
}

// Handler splits, validates, and routes OTLP requests to the Sink.
type Handler struct {
	sink Sink
	log  *slog.Logger
}

// NewHandler builds a Handler writing to sink.
func NewHandler(sink Sink, log *slog.Logger) *Handler {
	return &Handler{sink: sink, log: log}
}

// HandleParseFailure routes a request body that could not be unmarshaled to the DLQ. It
// keeps the original bytes for replay/debugging; the key is empty (no trace id known).
func (h *Handler) HandleParseFailure(ctx context.Context, rawBody []byte, reason string) error {
	h.log.Warn("otlp parse failure", "reason", reason, "bytes", len(rawBody))
	return h.sink.DLQ(ctx, "", rawBody, StageParse, reason)
}

// Process splits req by trace id, validates each group, and routes it: valid groups to
// the raw topic (as marshaled per-trace OTLP), invalid groups to the DLQ. It returns the
// accepted/rejected span counts for the OTLP partial-success response.
func (h *Handler) Process(ctx context.Context, req *collectortracepb.ExportTraceServiceRequest) (Result, error) {
	var res Result
	for _, g := range otlpx.SplitByTraceID(req) {
		n := countSpans(g)

		payload, err := proto.Marshal(g.Data)
		if err != nil {
			// Marshaling a message we just built should not fail; treat as a hard error.
			return res, err
		}

		if verr := otlpx.Validate(g.Data); verr != nil {
			res.RejectedSpans += n
			if err := h.sink.DLQ(ctx, g.TraceID, payload, StageValidate, verr.Error()); err != nil {
				return res, err
			}
			h.log.Warn("span group rejected", "trace_id", g.TraceID, "spans", n, "reason", verr.Error())
			continue
		}

		if err := h.sink.Raw(ctx, g.TraceID, payload); err != nil {
			return res, err
		}
		res.AcceptedSpans += n
	}
	return res, nil
}

func countSpans(g otlpx.TraceGroup) int {
	n := 0
	for _, rs := range g.Data.GetResourceSpans() {
		for _, ss := range rs.GetScopeSpans() {
			n += len(ss.GetSpans())
		}
	}
	return n
}
