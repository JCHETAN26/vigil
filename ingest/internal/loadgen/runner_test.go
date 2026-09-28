package loadgen

import (
	"bytes"
	"context"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	collectortracepb "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/durationpb"
)

// fakeCollector records every accepted span id and throttles the first `throttle` calls.
type fakeCollector struct {
	mu       sync.Mutex
	calls    int
	throttle int
	spans    map[string]int // trace+span id → times accepted
	unstamp  int            // resources missing the sent_at stamp
}

func newFakeCollector(throttle int) *fakeCollector {
	return &fakeCollector{throttle: throttle, spans: map[string]int{}}
}

// admit returns false when this call should be throttled.
func (f *fakeCollector) admit(req *collectortracepb.ExportTraceServiceRequest) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls++
	if f.calls <= f.throttle {
		return false
	}
	for _, rs := range req.GetResourceSpans() {
		stamped := false
		for _, kv := range rs.GetResource().GetAttributes() {
			stamped = stamped || kv.GetKey() == KeySentAtNano
		}
		if !stamped {
			f.unstamp++
		}
		for _, ss := range rs.GetScopeSpans() {
			for _, sp := range ss.GetSpans() {
				f.spans[string(sp.GetTraceId())+string(sp.GetSpanId())]++
			}
		}
	}
	return true
}

type grpcCollector struct {
	collectortracepb.UnimplementedTraceServiceServer
	f *fakeCollector
}

func (g *grpcCollector) Export(_ context.Context, req *collectortracepb.ExportTraceServiceRequest) (*collectortracepb.ExportTraceServiceResponse, error) {
	if !g.f.admit(req) {
		return nil, withRetryInfoErr(codes.ResourceExhausted, 50*time.Millisecond)
	}
	return &collectortracepb.ExportTraceServiceResponse{}, nil
}

func withRetryInfoErr(c codes.Code, d time.Duration) error {
	st, _ := status.New(c, "throttled").WithDetails(&errdetails.RetryInfo{RetryDelay: durationpb.New(d)})
	return st.Err()
}

func testTemplates(t *testing.T) []*Template {
	t.Helper()
	td := recordedTrace(t)
	return []*Template{Prepare(td, "run-test"), Prepare(td, "run-test")}
}

func testConfig(protocol, endpoint string) Config {
	return Config{Protocol: protocol, Endpoint: endpoint, RunID: "run-test", Rate: 400,
		Duration: 500 * time.Millisecond, MaxSpans: 16, MaxBytes: 1 << 20, Concurrency: 4,
		Timeout: 2 * time.Second, MaxRetryElapsed: 10 * time.Second, Seed: 1}
}

// checkRun asserts the generator's bookkeeping agrees with what the collector accepted:
// every span acked exactly once, all resources stamped, and the request log readable.
func checkRun(t *testing.T, sum Summary, log *bytes.Buffer, f *fakeCollector) {
	t.Helper()
	if sum.RequestsFail != 0 || sum.SpansUnacked != 0 {
		t.Fatalf("unexpected failures: %+v", sum)
	}
	if sum.Retries < f.throttle {
		t.Fatalf("retries = %d, want >= %d (throttled calls must be retried)", sum.Retries, f.throttle)
	}
	if int64(len(f.spans)) != sum.SpansAcked {
		t.Fatalf("collector holds %d distinct spans, generator acked %d", len(f.spans), sum.SpansAcked)
	}
	for id, n := range f.spans {
		if n != 1 {
			t.Fatalf("span %x accepted %d times", id, n)
		}
	}
	if f.unstamp != 0 {
		t.Fatalf("%d resources without sent_at", f.unstamp)
	}
	recs, err := ReadRecords(log)
	if err != nil {
		t.Fatal(err)
	}
	if len(recs) != sum.Requests {
		t.Fatalf("log has %d records, summary says %d requests", len(recs), sum.Requests)
	}
	var spans int64
	for _, r := range recs {
		spans += int64(r.Spans)
		if r.FirstSendNs < r.IntendedNs-int64(time.Millisecond) || r.DoneNs < r.FirstSendNs {
			t.Fatalf("record timestamps out of order: %+v", r)
		}
	}
	if spans != sum.SpansSent || sum.SpansSent < 100 {
		t.Fatalf("log spans %d, summary sent %d (want ~200 at 400/s for 0.5 s)", spans, sum.SpansSent)
	}
}

func TestRunGRPCHonorsRetryInfo(t *testing.T) {
	f := newFakeCollector(2)
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	srv := grpc.NewServer()
	collectortracepb.RegisterTraceServiceServer(srv, &grpcCollector{f: f})
	go func() { _ = srv.Serve(ln) }()
	defer srv.Stop()

	var log bytes.Buffer
	sum, err := Run(context.Background(), testConfig("grpc", ln.Addr().String()), testTemplates(t), &log)
	if err != nil {
		t.Fatal(err)
	}
	checkRun(t, sum, &log, f)
	if sum.Codes[codes.ResourceExhausted.String()] != 2 {
		t.Fatalf("attempt codes = %v", sum.Codes)
	}
}

func TestRunHTTPHonorsRetryAfter(t *testing.T) {
	f := newFakeCollector(1)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/traces" || r.Header.Get("Content-Type") != "application/x-protobuf" {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		body, _ := io.ReadAll(r.Body)
		var req collectortracepb.ExportTraceServiceRequest
		if err := proto.Unmarshal(body, &req); err != nil {
			http.Error(w, "bad proto", http.StatusBadRequest)
			return
		}
		if !f.admit(&req) {
			w.Header().Set("Retry-After", "1")
			http.Error(w, "slow down", http.StatusTooManyRequests)
			return
		}
		out, _ := proto.Marshal(&collectortracepb.ExportTraceServiceResponse{})
		w.Header().Set("Content-Type", "application/x-protobuf")
		_, _ = w.Write(out)
	}))
	defer srv.Close()

	var log bytes.Buffer
	sum, err := Run(context.Background(), testConfig("http", strings.TrimPrefix(srv.URL, "http://")), testTemplates(t), &log)
	if err != nil {
		t.Fatal(err)
	}
	checkRun(t, sum, &log, f)
	if sum.Codes["429"] != 1 || sum.ServiceMs.Max < 1000 {
		t.Fatalf("429 must be retried after Retry-After (1 s): codes %v, max service %.0f ms", sum.Codes, sum.ServiceMs.Max)
	}
}

func TestRunGivesUpOnPermanentErrors(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "nope", http.StatusBadRequest)
	}))
	defer srv.Close()
	var log bytes.Buffer
	sum, err := Run(context.Background(), testConfig("http", strings.TrimPrefix(srv.URL, "http://")), testTemplates(t), &log)
	if err != nil {
		t.Fatal(err)
	}
	if sum.RequestsOK != 0 || sum.Retries != 0 || sum.SpansUnacked != sum.SpansSent {
		t.Fatalf("400s must fail without retry and count as unacked: %+v", sum)
	}
}

func TestSummarize(t *testing.T) {
	xs := make([]float64, 100)
	for i := range xs {
		xs[i] = float64(100 - i) // 100..1, unsorted
	}
	p := Summarize(xs)
	if p.N != 100 || p.P50 != 50 || p.P95 != 95 || p.P99 != 99 || p.Max != 100 || p.Mean != 50.5 {
		t.Fatalf("got %+v", p)
	}
	if (Summarize(nil) != Percentiles{}) {
		t.Fatal("empty input must give zero percentiles")
	}
}
