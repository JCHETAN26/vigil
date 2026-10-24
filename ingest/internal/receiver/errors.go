package receiver

import (
	"context"
	"errors"
	"net/http"

	"google.golang.org/grpc/codes"
)

// EnqueueError reports that a payload could not be durably published to Redpanda (raw
// topic or DLQ), including because the request context ended first. It is retryable from
// the client's side, so the transports map it to gRPC Unavailable / HTTP 503 (both on the
// OTLP retryable list) rather than an internal error.
//
// A retried request may duplicate data: see Handler.Process for the duplicate window.
type EnqueueError struct{ Err error }

func (e *EnqueueError) Error() string { return "enqueue: " + e.Err.Error() }
func (e *EnqueueError) Unwrap() error { return e.Err }

// grpcCode maps a handler error to a gRPC status code. Only an enqueue failure is attributed
// to the request context: if the context has ended it reports its own cause (Canceled or
// DeadlineExceeded; OTLP clients retry both), otherwise it is Unavailable. Anything else is
// a bug and stays Internal even when the client has also gone away, so a cancellation never
// masks it.
func grpcCode(ctx context.Context, err error) codes.Code {
	if !isEnqueueError(err) {
		return codes.Internal
	}
	switch {
	case errors.Is(ctx.Err(), context.Canceled):
		return codes.Canceled
	case errors.Is(ctx.Err(), context.DeadlineExceeded):
		return codes.DeadlineExceeded
	default:
		return codes.Unavailable
	}
}

// httpStatus maps a handler error to an HTTP status: every enqueue failure, including an
// ended request context, is 503 (retryable); anything else is 500.
func httpStatus(err error) int {
	if isEnqueueError(err) {
		return http.StatusServiceUnavailable
	}
	return http.StatusInternalServerError
}

func isEnqueueError(err error) bool {
	var ee *EnqueueError
	return errors.As(err, &ee)
}
