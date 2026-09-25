package receiver

import (
	"errors"
	"io"
	"log/slog"
	"net/http"

	collectortracepb "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	"google.golang.org/protobuf/proto"
)

const protobufContentType = "application/x-protobuf"

// HTTPHandler serves the OTLP/HTTP trace endpoint (POST /v1/traces) for protobuf bodies.
// JSON encoding is intentionally not supported yet (returns 415).
type HTTPHandler struct {
	handler     *Handler
	maxBodyByte int64
	log         *slog.Logger
}

// NewHTTPHandler builds the OTLP/HTTP handler. maxBodyBytes bounds the request body.
func NewHTTPHandler(handler *Handler, maxBodyBytes int, log *slog.Logger) *HTTPHandler {
	return &HTTPHandler{handler: handler, maxBodyByte: int64(maxBodyBytes), log: log}
}

// ServeHTTP handles POST /v1/traces.
func (h *HTTPHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if ct := r.Header.Get("Content-Type"); ct != "" && ct != protobufContentType {
		http.Error(w, "only "+protobufContentType+" is supported", http.StatusUnsupportedMediaType)
		return
	}

	r.Body = http.MaxBytesReader(w, r.Body, h.maxBodyByte)
	body, err := io.ReadAll(r.Body)
	if err != nil {
		var mbe *http.MaxBytesError
		if errors.As(err, &mbe) {
			http.Error(w, "request body too large", http.StatusRequestEntityTooLarge)
			return
		}
		http.Error(w, "failed to read body", http.StatusBadRequest)
		return
	}

	var req collectortracepb.ExportTraceServiceRequest
	if err := proto.Unmarshal(body, &req); err != nil {
		// Unparseable OTLP → DLQ (keep the original bytes) and 400.
		if derr := h.handler.HandleParseFailure(r.Context(), body, err.Error()); derr != nil {
			h.log.Error("dlq parse failure", "err", derr)
			http.Error(w, "failed to enqueue", http.StatusInternalServerError)
			return
		}
		http.Error(w, "invalid OTLP protobuf", http.StatusBadRequest)
		return
	}

	res, err := h.handler.Process(r.Context(), &req)
	if err != nil {
		h.log.Error("process export", "err", err)
		http.Error(w, "failed to enqueue spans", http.StatusInternalServerError)
		return
	}

	resp := &collectortracepb.ExportTraceServiceResponse{}
	if res.RejectedSpans > 0 {
		resp.PartialSuccess = &collectortracepb.ExportTracePartialSuccess{
			RejectedSpans: int64(res.RejectedSpans),
			ErrorMessage:  "some spans failed validation and were routed to the DLQ",
		}
	}
	out, err := proto.Marshal(resp)
	if err != nil {
		http.Error(w, "failed to encode response", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", protobufContentType)
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(out)
}
