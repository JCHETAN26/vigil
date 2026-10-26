package receiver

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	collectortracepb "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	tracepb "go.opentelemetry.io/proto/otlp/trace/v1"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// threeTraces builds a request with three valid single-span traces (0x01..0x03), plus an
// invalid trace 0x09 when withInvalid is set.
func threeTraces(withInvalid bool) *collectortracepb.ExportTraceServiceRequest {
	spans := []*tracepb.Span{mkSpan(1, 1, 10, 20), mkSpan(2, 1, 10, 20), mkSpan(3, 1, 10, 20)}
	if withInvalid {
		spans = append(spans, mkSpan(9, 1, 20, 10))
	}
	return &collectortracepb.ExportTraceServiceRequest{ResourceSpans: []*tracepb.ResourceSpans{{
		ScopeSpans: []*tracepb.ScopeSpans{{Spans: spans}},
	}}}
}

func canceledCtx() context.Context {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	return ctx
}

// expiredCtx is a context whose deadline has already passed (Err is DeadlineExceeded).
// Its timer has fired, so there is nothing to release by canceling it.
func expiredCtx() context.Context {
	ctx, cancel := context.WithDeadline(context.Background(), time.Unix(0, 0))
	_ = cancel
	return ctx
}

func TestStatusMapping(t *testing.T) {
	enq := &EnqueueError{Err: errBroker}
	tests := []struct {
		name     string
		ctx      context.Context
		err      error
		wantGRPC codes.Code
		wantHTTP int
	}{
		{"enqueue failure", context.Background(), enq, codes.Unavailable, http.StatusServiceUnavailable},
		{"wrapped enqueue failure", context.Background(), fmt.Errorf("x: %w", enq), codes.Unavailable, http.StatusServiceUnavailable},
		{"request canceled", canceledCtx(), &EnqueueError{Err: context.Canceled}, codes.Canceled, http.StatusServiceUnavailable},
		{"request deadline", expiredCtx(), &EnqueueError{Err: context.DeadlineExceeded}, codes.DeadlineExceeded, http.StatusServiceUnavailable},
		{"producer timeout, request alive", context.Background(), &EnqueueError{Err: context.DeadlineExceeded}, codes.Unavailable, http.StatusServiceUnavailable},
		{"non-enqueue error", context.Background(), errors.New("marshal bug"), codes.Internal, http.StatusInternalServerError},
		// A bug is never reported as the client's cancellation, even once the client has gone.
		{"non-enqueue error, request canceled", canceledCtx(), errors.New("marshal bug"), codes.Internal, http.StatusInternalServerError},
		{"non-enqueue error, request deadline", expiredCtx(), errors.New("marshal bug"), codes.Internal, http.StatusInternalServerError},
		{"bare context error, request canceled", canceledCtx(), context.Canceled, codes.Internal, http.StatusInternalServerError},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := grpcCode(tt.ctx, tt.err); got != tt.wantGRPC {
				t.Errorf("grpcCode = %s, want %s", got, tt.wantGRPC)
			}
			if got := httpStatus(tt.err); got != tt.wantHTTP {
				t.Errorf("httpStatus = %d, want %d", got, tt.wantHTTP)
			}
		})
	}
}

// TestProcessEnqueueFailure covers partial enqueue: groups published before the failure stay
// published (and are counted), processing stops, and the error is an *EnqueueError.
func TestProcessEnqueueFailure(t *testing.T) {
	tests := []struct {
		name         string
		ctx          context.Context
		sink         *fakeSink
		invalid      bool
		wantRaw      int
		wantRawCalls int
		wantAccepted int
		wantCause    error
	}{
		{"second raw publish fails", context.Background(), &fakeSink{rawFailAt: 2}, false, 1, 2, 1, errBroker},
		{"first raw publish fails", context.Background(), &fakeSink{rawFailAt: 1}, false, 0, 1, 0, errBroker},
		{"dlq publish of invalid group fails", context.Background(), &fakeSink{dlqErr: errBroker}, true, 3, 3, 3, errBroker},
		{"canceled before start", canceledCtx(), &fakeSink{}, false, 0, 0, 0, context.Canceled},
		{"deadline before start", expiredCtx(), &fakeSink{}, false, 0, 0, 0, context.DeadlineExceeded},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			h := NewHandler(tt.sink, testLogger())
			res, err := h.Process(tt.ctx, threeTraces(tt.invalid))
			var ee *EnqueueError
			if !errors.As(err, &ee) || !errors.Is(err, tt.wantCause) {
				t.Fatalf("err = %v, want *EnqueueError wrapping %v", err, tt.wantCause)
			}
			if len(tt.sink.raw) != tt.wantRaw || tt.sink.rawCalls != tt.wantRawCalls {
				t.Errorf("raw published=%d calls=%d, want %d/%d", len(tt.sink.raw), tt.sink.rawCalls, tt.wantRaw, tt.wantRawCalls)
			}
			if res.AcceptedSpans != tt.wantAccepted {
				t.Errorf("AcceptedSpans = %d, want %d", res.AcceptedSpans, tt.wantAccepted)
			}
		})
	}
}

// TestGRPCExportStatus checks end-to-end gRPC mapping, including that validation
// rejections stay an OK partial success.
func TestGRPCExportStatus(t *testing.T) {
	tests := []struct {
		name        string
		ctx         context.Context
		sink        *fakeSink
		invalid     bool
		wantCode    codes.Code
		wantPartial int64
	}{
		{"all accepted", context.Background(), &fakeSink{}, false, codes.OK, 0},
		{"validation partial success", context.Background(), &fakeSink{}, true, codes.OK, 1},
		{"raw enqueue fails", context.Background(), &fakeSink{rawFailAt: 2}, false, codes.Unavailable, 0},
		{"dlq enqueue fails", context.Background(), &fakeSink{dlqErr: errBroker}, true, codes.Unavailable, 0},
		{"canceled", canceledCtx(), &fakeSink{}, false, codes.Canceled, 0},
		{"deadline", expiredCtx(), &fakeSink{}, false, codes.DeadlineExceeded, 0},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			s := NewGRPCServer(NewHandler(tt.sink, testLogger()), testLogger())
			resp, err := s.Export(tt.ctx, threeTraces(tt.invalid))
			if got := status.Code(err); got != tt.wantCode {
				t.Fatalf("code = %s, want %s (err %v)", got, tt.wantCode, err)
			}
			if tt.wantCode == codes.OK && resp.GetPartialSuccess().GetRejectedSpans() != tt.wantPartial {
				t.Errorf("rejected spans = %d, want %d", resp.GetPartialSuccess().GetRejectedSpans(), tt.wantPartial)
			}
		})
	}
}

// TestHTTPExportStatus checks end-to-end HTTP mapping for export and parse-failure paths.
func TestHTTPExportStatus(t *testing.T) {
	valid, _ := proto.Marshal(threeTraces(false))
	withInvalid, _ := proto.Marshal(threeTraces(true))
	garbage := []byte{0xff, 0xff, 0xff, 0xff}
	tests := []struct {
		name        string
		ctx         context.Context
		body        []byte
		sink        *fakeSink
		wantStatus  int
		wantPartial int64
	}{
		{"all accepted", context.Background(), valid, &fakeSink{}, http.StatusOK, 0},
		{"validation partial success", context.Background(), withInvalid, &fakeSink{}, http.StatusOK, 1},
		{"raw enqueue fails", context.Background(), valid, &fakeSink{rawFailAt: 1}, http.StatusServiceUnavailable, 0},
		{"canceled request", canceledCtx(), valid, &fakeSink{}, http.StatusServiceUnavailable, 0},
		{"parse failure routed to dlq", context.Background(), garbage, &fakeSink{}, http.StatusBadRequest, 0},
		{"parse failure, dlq enqueue fails", context.Background(), garbage, &fakeSink{dlqErr: errBroker}, http.StatusServiceUnavailable, 0},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			h := NewHTTPHandler(NewHandler(tt.sink, testLogger()), 1<<20, testLogger())
			req := httptest.NewRequestWithContext(tt.ctx, http.MethodPost, "/v1/traces", bytes.NewReader(tt.body))
			req.Header.Set("Content-Type", protobufContentType)
			rr := httptest.NewRecorder()
			h.ServeHTTP(rr, req)
			if rr.Code != tt.wantStatus {
				t.Fatalf("status = %d, want %d (body %q)", rr.Code, tt.wantStatus, rr.Body.String())
			}
			if tt.wantStatus == http.StatusOK {
				var resp collectortracepb.ExportTraceServiceResponse
				if err := proto.Unmarshal(rr.Body.Bytes(), &resp); err != nil {
					t.Fatalf("decode response: %v", err)
				}
				if got := resp.GetPartialSuccess().GetRejectedSpans(); got != tt.wantPartial {
					t.Errorf("rejected spans = %d, want %d", got, tt.wantPartial)
				}
			}
		})
	}
}
