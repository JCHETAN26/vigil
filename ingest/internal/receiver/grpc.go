package receiver

import (
	"context"
	"log/slog"

	collectortracepb "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// GRPCServer implements the OTLP TraceService over gRPC. A malformed protobuf body is
// rejected by the gRPC framework before Export is called, so the only DLQ path here is
// per-trace validation (handled inside Handler).
type GRPCServer struct {
	collectortracepb.UnimplementedTraceServiceServer
	handler *Handler
	log     *slog.Logger
}

// NewGRPCServer builds the gRPC trace service.
func NewGRPCServer(handler *Handler, log *slog.Logger) *GRPCServer {
	return &GRPCServer{handler: handler, log: log}
}

// Export receives a batch of spans, splits/validates/routes them, and reports rejected
// spans via OTLP partial success. A failed enqueue returns a retryable status
// (Unavailable, or the request context's Canceled/DeadlineExceeded); see grpcCode.
func (s *GRPCServer) Export(ctx context.Context, req *collectortracepb.ExportTraceServiceRequest) (*collectortracepb.ExportTraceServiceResponse, error) {
	res, err := s.handler.Process(ctx, req)
	if err != nil {
		code := grpcCode(ctx, err)
		if code == codes.Internal {
			s.log.Error("process export", "err", err)
		} else {
			s.log.Warn("enqueue failed, client should retry", "code", code.String(),
				"accepted_spans", res.AcceptedSpans, "err", err)
		}
		return nil, status.Error(code, "failed to enqueue spans")
	}
	resp := &collectortracepb.ExportTraceServiceResponse{}
	if res.RejectedSpans > 0 {
		resp.PartialSuccess = &collectortracepb.ExportTracePartialSuccess{
			RejectedSpans: int64(res.RejectedSpans),
			ErrorMessage:  "some spans failed validation and were routed to the DLQ",
		}
	}
	return resp, nil
}
