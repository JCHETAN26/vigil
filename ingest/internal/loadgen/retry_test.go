package loadgen

import (
	"errors"
	"testing"
	"time"

	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/durationpb"
)

func withRetryInfo(t *testing.T, c codes.Code, d time.Duration) error {
	t.Helper()
	st, err := status.New(c, "busy").WithDetails(&errdetails.RetryInfo{RetryDelay: durationpb.New(d)})
	if err != nil {
		t.Fatal(err)
	}
	return st.Err()
}

func TestClassifyGRPC(t *testing.T) {
	tests := []struct {
		name      string
		err       error
		ok, retry bool
		after     time.Duration
	}{
		{"success", nil, true, false, 0},
		{"unavailable is retryable", status.Error(codes.Unavailable, "down"), false, true, 0},
		{"deadline is retryable", status.Error(codes.DeadlineExceeded, "slow"), false, true, 0},
		{"resource exhausted without RetryInfo is permanent", status.Error(codes.ResourceExhausted, "full"), false, false, 0},
		{"resource exhausted with RetryInfo is retryable after its delay", withRetryInfo(t, codes.ResourceExhausted, 1500*time.Millisecond), false, true, 1500 * time.Millisecond},
		{"invalid argument is permanent", status.Error(codes.InvalidArgument, "bad"), false, false, 0},
		{"non-status error maps to unknown (permanent)", errors.New("boom"), false, false, 0},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			o := ClassifyGRPC(tc.err)
			if o.OK != tc.ok || o.Retryable != tc.retry || o.RetryAfter != tc.after {
				t.Fatalf("got %+v", o)
			}
		})
	}
}

func TestClassifyHTTP(t *testing.T) {
	now := time.Date(2026, 9, 28, 12, 0, 0, 0, time.UTC)
	tests := []struct {
		name       string
		status     int
		retryAfter string
		ok, retry  bool
		after      time.Duration
	}{
		{"200", 200, "", true, false, 0},
		{"429 with seconds", 429, "3", false, true, 3 * time.Second},
		{"503 with http date", 503, "Mon, 28 Sep 2026 12:00:10 GMT", false, true, 10 * time.Second},
		{"504 without header", 504, "", false, true, 0},
		{"429 with garbage header", 429, "soon", false, true, 0},
		{"400 is permanent", 400, "", false, false, 0},
		{"500 is permanent per OTLP spec", 500, "", false, false, 0},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			o := ClassifyHTTP(tc.status, tc.retryAfter, now)
			if o.OK != tc.ok || o.Retryable != tc.retry || o.RetryAfter != tc.after {
				t.Fatalf("got %+v", o)
			}
		})
	}
}

func TestBackoff(t *testing.T) {
	b := NewBackoff(time.Minute, 1)
	if d := b.Delay(3, 700*time.Millisecond); d != 700*time.Millisecond {
		t.Fatalf("server delay must be honored exactly, got %v", d)
	}
	for attempt, base := range map[int]time.Duration{1: time.Second, 2: 2 * time.Second, 3: 4 * time.Second, 10: 30 * time.Second} {
		for i := 0; i < 20; i++ {
			d := b.Delay(attempt, 0)
			if d < time.Duration(0.8*float64(base)) || d > time.Duration(1.2*float64(base)) {
				t.Fatalf("attempt %d: %v outside ±20%% of %v", attempt, d, base)
			}
		}
	}
}
