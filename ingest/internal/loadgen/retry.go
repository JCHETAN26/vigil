package loadgen

import (
	"math/rand"
	"net/http"
	"strconv"
	"time"

	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// Outcome classifies one send attempt, following the OTLP exporter retry rules so the load
// generator backs off exactly like a standard SDK exporter would.
type Outcome struct {
	OK         bool
	Retryable  bool
	RetryAfter time.Duration // server-requested delay (RetryInfo / Retry-After); 0 = none
	Code       string        // gRPC code name or HTTP status, for the request log
	Rejected   int64         // partial-success rejected span count (OK responses only)
}

// ClassifyGRPC maps an Export error per the OTLP/gRPC spec: Canceled, DeadlineExceeded,
// Aborted, OutOfRange, Unavailable and DataLoss are retryable; ResourceExhausted is
// retryable only when the server attaches RetryInfo (its throttling signal), whose delay
// is honored.
func ClassifyGRPC(err error) Outcome {
	if err == nil {
		return Outcome{OK: true, Code: codes.OK.String()}
	}
	st, _ := status.FromError(err)
	o := Outcome{Code: st.Code().String()}
	var retryInfo *errdetails.RetryInfo
	for _, d := range st.Details() {
		if ri, ok := d.(*errdetails.RetryInfo); ok {
			retryInfo = ri
		}
	}
	if retryInfo != nil && retryInfo.GetRetryDelay() != nil {
		o.RetryAfter = retryInfo.GetRetryDelay().AsDuration()
	}
	switch st.Code() {
	case codes.Canceled, codes.DeadlineExceeded, codes.Aborted, codes.OutOfRange,
		codes.Unavailable, codes.DataLoss:
		o.Retryable = true
	case codes.ResourceExhausted:
		o.Retryable = retryInfo != nil
	}
	return o
}

// ClassifyHTTP maps an OTLP/HTTP response status per the spec: 429, 502, 503 and 504 are
// retryable, honoring Retry-After (delta-seconds or HTTP-date) when present.
func ClassifyHTTP(statusCode int, retryAfter string, now time.Time) Outcome {
	o := Outcome{Code: strconv.Itoa(statusCode)}
	switch statusCode {
	case http.StatusOK:
		o.OK = true
	case http.StatusTooManyRequests, http.StatusBadGateway, http.StatusServiceUnavailable,
		http.StatusGatewayTimeout:
		o.Retryable = true
		o.RetryAfter = parseRetryAfter(retryAfter, now)
	}
	return o
}

func parseRetryAfter(v string, now time.Time) time.Duration {
	if v == "" {
		return 0
	}
	if secs, err := strconv.Atoi(v); err == nil && secs >= 0 {
		return time.Duration(secs) * time.Second
	}
	if t, err := http.ParseTime(v); err == nil && t.After(now) {
		return t.Sub(now)
	}
	return 0
}

// Backoff is exponential backoff with jitter for retries without a server-requested
// delay, bounded in total by MaxElapsed (after which the request is given up and logged
// as failed — never silently dropped).
type Backoff struct {
	Initial, Max, MaxElapsed time.Duration
	rng                      *rand.Rand
}

// NewBackoff returns the defaults used by the OpenTelemetry SDK exporters' retry policy
// (initial 1s, doubling, capped per attempt), with a configurable total budget.
func NewBackoff(maxElapsed time.Duration, seed int64) *Backoff {
	return &Backoff{Initial: time.Second, Max: 30 * time.Second, MaxElapsed: maxElapsed,
		rng: rand.New(rand.NewSource(seed))}
}

// Delay returns the wait before retry number attempt (1-based): the server's delay if it
// gave one, else Initial·2^(attempt-1) capped at Max, with ±20% jitter.
func (b *Backoff) Delay(attempt int, serverDelay time.Duration) time.Duration {
	if serverDelay > 0 {
		return serverDelay
	}
	d := b.Initial
	for i := 1; i < attempt && d < b.Max; i++ {
		d *= 2
	}
	if d > b.Max {
		d = b.Max
	}
	return time.Duration(float64(d) * (0.8 + 0.4*b.rng.Float64()))
}
