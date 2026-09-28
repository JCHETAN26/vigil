package loadgen

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/csv"
	"fmt"
	"io"
	"net/http"
	"runtime"
	"sort"
	"strconv"
	"sync"
	"syscall"
	"time"

	collectortracepb "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	tracepb "go.opentelemetry.io/proto/otlp/trace/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/proto"
)

// Config is one load run.
type Config struct {
	Protocol        string        `json:"protocol"` // "grpc" | "http"
	Endpoint        string        `json:"endpoint"` // host:port
	RunID           string        `json:"run_id"`
	Rate            float64       `json:"rate_spans_per_sec"`
	Duration        time.Duration `json:"duration_ns"`
	MaxSpans        int           `json:"max_spans_per_request"`
	MaxBytes        int           `json:"max_bytes_per_request"`
	Concurrency     int           `json:"concurrency"`
	Timeout         time.Duration `json:"attempt_timeout_ns"`
	MaxRetryElapsed time.Duration `json:"max_retry_elapsed_ns"`
	Seed            int64         `json:"seed"`
}

// Record is one request's fate, one CSV row in requests.csv.gz.
type Record struct {
	FirstSeq    uint64
	Traces      int
	Spans       int
	IntendedNs  int64
	FirstSendNs int64
	DoneNs      int64
	Attempts    int
	OK          bool
	Code        string
	Rejected    int64
}

var recordHeader = []string{"first_seq", "traces", "spans", "intended_ns", "first_send_ns",
	"done_ns", "attempts", "ok", "code", "rejected_spans"}

func (r Record) row() []string {
	return []string{
		strconv.FormatUint(r.FirstSeq, 10), strconv.Itoa(r.Traces), strconv.Itoa(r.Spans),
		strconv.FormatInt(r.IntendedNs, 10), strconv.FormatInt(r.FirstSendNs, 10),
		strconv.FormatInt(r.DoneNs, 10), strconv.Itoa(r.Attempts), strconv.FormatBool(r.OK),
		r.Code, strconv.FormatInt(r.Rejected, 10),
	}
}

// ReadRecords parses a requests.csv.gz written by Run.
func ReadRecords(r io.Reader) ([]Record, error) {
	zr, err := gzip.NewReader(r)
	if err != nil {
		return nil, err
	}
	rows, err := csv.NewReader(zr).ReadAll()
	if err != nil {
		return nil, err
	}
	out := make([]Record, 0, len(rows))
	for i, row := range rows {
		if i == 0 {
			continue // header
		}
		if len(row) != len(recordHeader) {
			return nil, fmt.Errorf("row %d: %d fields", i, len(row))
		}
		var rec Record
		var perr error
		parseU := func(s string) uint64 { v, e := strconv.ParseUint(s, 10, 64); perr = firstErr(perr, e); return v }
		parseI := func(s string) int64 { v, e := strconv.ParseInt(s, 10, 64); perr = firstErr(perr, e); return v }
		rec.FirstSeq = parseU(row[0])
		rec.Traces = int(parseI(row[1]))
		rec.Spans = int(parseI(row[2]))
		rec.IntendedNs, rec.FirstSendNs, rec.DoneNs = parseI(row[3]), parseI(row[4]), parseI(row[5])
		rec.Attempts = int(parseI(row[6]))
		rec.OK = row[7] == "true"
		rec.Code = row[8]
		rec.Rejected = parseI(row[9])
		if perr != nil {
			return nil, fmt.Errorf("row %d: %w", i, perr)
		}
		out = append(out, rec)
	}
	return out, nil
}

func firstErr(a, b error) error {
	if a != nil {
		return a
	}
	return b
}

// Summary is the generator-side result of a run (summary.json). Server-side truth —
// stored counts and end-to-end latency — comes from reconciliation.
type Summary struct {
	Config       Config         `json:"config"`
	StartNs      int64          `json:"start_ns"`
	EndNs        int64          `json:"end_ns"` // last request completed
	GOMAXPROCS   int            `json:"gomaxprocs"`
	Requests     int            `json:"requests"`
	RequestsOK   int            `json:"requests_ok"`
	RequestsFail int            `json:"requests_failed"`
	Retries      int            `json:"retries"`
	SpansSent    int64          `json:"spans_sent"`
	SpansAcked   int64          `json:"spans_acked"`
	SpansUnacked int64          `json:"spans_unacked"`
	SpansRejectd int64          `json:"spans_rejected_partial"`
	AckedRate    float64        `json:"acked_spans_per_sec"`
	Codes        map[string]int `json:"attempt_codes"`
	// Request latency from the INTENDED send time (coordinated-omission safe) and from
	// the actual first send (service time incl. retries); schedule lag = first send minus
	// intended — large values mean the generator could not keep up.
	LatencyMs     Percentiles `json:"request_latency_ms"`
	ServiceMs     Percentiles `json:"service_latency_ms"`
	ScheduleLagMs Percentiles `json:"schedule_lag_ms"`
	// Generator CPU, sampled each second as a fraction of one core (it is pinned to one).
	CPUSamples []float64   `json:"cpu_samples"`
	CPU        Percentiles `json:"cpu_fraction"`
}

// Percentiles summarizes a distribution.
type Percentiles struct {
	N    int     `json:"n"`
	Mean float64 `json:"mean"`
	P50  float64 `json:"p50"`
	P95  float64 `json:"p95"`
	P99  float64 `json:"p99"`
	Max  float64 `json:"max"`
}

// Summarize computes percentiles (nearest-rank) of xs.
func Summarize(xs []float64) Percentiles {
	if len(xs) == 0 {
		return Percentiles{}
	}
	s := append([]float64(nil), xs...)
	sort.Float64s(s)
	var sum float64
	for _, x := range s {
		sum += x
	}
	rank := func(q float64) float64 {
		i := int(q*float64(len(s))+0.999999) - 1
		if i < 0 {
			i = 0
		}
		if i >= len(s) {
			i = len(s) - 1
		}
		return s[i]
	}
	return Percentiles{N: len(s), Mean: sum / float64(len(s)), P50: rank(0.50), P95: rank(0.95),
		P99: rank(0.99), Max: s[len(s)-1]}
}

// sender sends one OTLP export attempt.
type sender interface {
	send(ctx context.Context, req *collectortracepb.ExportTraceServiceRequest, body []byte) Outcome
	needsBody() bool
	close()
}

type grpcSender struct {
	conn   *grpc.ClientConn
	client collectortracepb.TraceServiceClient
}

func newGRPCSender(endpoint string) (*grpcSender, error) {
	conn, err := grpc.NewClient(endpoint, grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithDefaultCallOptions(grpc.MaxCallSendMsgSize(16<<20)))
	if err != nil {
		return nil, err
	}
	return &grpcSender{conn: conn, client: collectortracepb.NewTraceServiceClient(conn)}, nil
}

func (s *grpcSender) send(ctx context.Context, req *collectortracepb.ExportTraceServiceRequest, _ []byte) Outcome {
	resp, err := s.client.Export(ctx, req)
	o := ClassifyGRPC(err)
	if o.OK {
		o.Rejected = resp.GetPartialSuccess().GetRejectedSpans()
	}
	return o
}
func (s *grpcSender) needsBody() bool { return false }
func (s *grpcSender) close()          { _ = s.conn.Close() }

type httpSender struct {
	url    string
	client *http.Client
}

func newHTTPSender(endpoint string, concurrency int) *httpSender {
	tr := http.DefaultTransport.(*http.Transport).Clone()
	tr.MaxIdleConns = concurrency
	tr.MaxIdleConnsPerHost = concurrency
	return &httpSender{url: "http://" + endpoint + "/v1/traces", client: &http.Client{Transport: tr}}
}

func (s *httpSender) send(ctx context.Context, _ *collectortracepb.ExportTraceServiceRequest, body []byte) Outcome {
	hreq, err := http.NewRequestWithContext(ctx, http.MethodPost, s.url, bytes.NewReader(body))
	if err != nil {
		return Outcome{Code: "request_error"}
	}
	hreq.Header.Set("Content-Type", "application/x-protobuf")
	resp, err := s.client.Do(hreq)
	if err != nil {
		// Connection-level failure: the request may or may not have been processed.
		return Outcome{Retryable: true, Code: "transport_error"}
	}
	defer resp.Body.Close()
	respBody, _ := io.ReadAll(resp.Body)
	o := ClassifyHTTP(resp.StatusCode, resp.Header.Get("Retry-After"), time.Now())
	if o.OK {
		var er collectortracepb.ExportTraceServiceResponse
		if proto.Unmarshal(respBody, &er) == nil {
			o.Rejected = er.GetPartialSuccess().GetRejectedSpans()
		}
	}
	return o
}
func (s *httpSender) needsBody() bool { return true }
func (s *httpSender) close()          { s.client.CloseIdleConnections() }

// InstanceBytes estimates each template's encoded size per replay (for the request byte
// cap), by materializing one instance.
func InstanceBytes(runID string, tmpls []*Template) []int {
	out := make([]int, len(tmpls))
	for i, t := range tmpls {
		rss := t.Instance(runID, 0, uint64(time.Now().UnixNano()))
		StampSentAt(rss, time.Now().UnixNano())
		out[i] = proto.Size(&tracepb.TracesData{ResourceSpans: rss})
	}
	return out
}

// Run executes one open-loop load run: a scheduler emits requests on the plan's timeline
// to Concurrency workers, which materialize the traces, stamp the send time, and send with
// OTLP-spec retries. Every request's outcome is written to log (gzip CSV). Cancelling ctx
// stops scheduling; in-flight requests finish.
func Run(ctx context.Context, cfg Config, tmpls []*Template, log io.Writer) (Summary, error) {
	var snd sender
	switch cfg.Protocol {
	case "grpc":
		s, err := newGRPCSender(cfg.Endpoint)
		if err != nil {
			return Summary{}, err
		}
		snd = s
	case "http":
		snd = newHTTPSender(cfg.Endpoint, cfg.Concurrency)
	default:
		return Summary{}, fmt.Errorf("unknown protocol %q", cfg.Protocol)
	}
	defer snd.close()

	spans := make([]int, len(tmpls))
	for i, t := range tmpls {
		spans[i] = t.Spans()
	}
	plan := NewPlan(spans, InstanceBytes(cfg.RunID, tmpls), cfg.MaxSpans, cfg.MaxBytes, cfg.Seed)

	zw := gzip.NewWriter(log)
	cw := csv.NewWriter(zw)
	_ = cw.Write(recordHeader)
	var mu sync.Mutex // guards cw and the summary accumulators
	sum := Summary{Config: cfg, GOMAXPROCS: runtime.GOMAXPROCS(0), Codes: map[string]int{}}
	var lat, svc, lag []float64

	sampler := startCPUSampler()
	start := time.Now().Add(100 * time.Millisecond)
	sum.StartNs = start.UnixNano()
	jobs := make(chan Request, cfg.Concurrency)

	var wg sync.WaitGroup
	for w := 0; w < cfg.Concurrency; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			backoff := NewBackoff(cfg.MaxRetryElapsed, cfg.Seed+int64(w)+1)
			for job := range jobs {
				rec, codes := sendOne(snd, cfg, plan, tmpls, job, backoff)
				mu.Lock()
				_ = cw.Write(rec.row())
				sum.Requests++
				sum.Retries += rec.Attempts - 1
				sum.SpansSent += int64(rec.Spans)
				for _, c := range codes {
					sum.Codes[c]++
				}
				if rec.OK {
					sum.RequestsOK++
					sum.SpansAcked += int64(rec.Spans) - rec.Rejected
					sum.SpansRejectd += rec.Rejected
				} else {
					sum.RequestsFail++
					sum.SpansUnacked += int64(rec.Spans)
				}
				if rec.DoneNs > sum.EndNs {
					sum.EndNs = rec.DoneNs
				}
				lat = append(lat, float64(rec.DoneNs-rec.IntendedNs)/1e6)
				svc = append(svc, float64(rec.DoneNs-rec.FirstSendNs)/1e6)
				lag = append(lag, float64(rec.FirstSendNs-rec.IntendedNs)/1e6)
				mu.Unlock()
			}
		}(w)
	}

	end := start.Add(cfg.Duration)
schedule:
	for {
		req := plan.Next(start, cfg.Rate)
		if !req.Intended.Before(end) {
			break
		}
		if d := time.Until(req.Intended); d > 0 {
			select {
			case <-time.After(d):
			case <-ctx.Done():
				break schedule
			}
		}
		select {
		case jobs <- req: // blocks when all workers are busy: shows up as schedule lag
		case <-ctx.Done():
			break schedule
		}
	}
	close(jobs)
	wg.Wait()

	sum.CPUSamples = sampler.stop()
	sum.CPU = Summarize(sum.CPUSamples)
	sum.LatencyMs, sum.ServiceMs, sum.ScheduleLagMs = Summarize(lat), Summarize(svc), Summarize(lag)
	// Over the offered window (or longer, if the tail of requests completed after it): with
	// SDK-sized batches a short run's first request carries hundreds of spans at t=0, so
	// dividing by the send span alone would overstate the rate.
	if el := max(float64(sum.EndNs-sum.StartNs), float64(cfg.Duration)) / 1e9; el > 0 {
		sum.AckedRate = float64(sum.SpansAcked) / el
	}
	cw.Flush()
	if err := cw.Error(); err != nil {
		return sum, err
	}
	return sum, zw.Close()
}

// sendOne materializes a request, stamps its first-send time, and sends it with retries.
// It returns the request record and every attempt's code.
func sendOne(snd sender, cfg Config, plan *Plan, tmpls []*Template, job Request, bo *Backoff) (Record, []string) {
	rec := Record{FirstSeq: job.FirstSeq, Traces: job.Traces, Spans: job.Spans, IntendedNs: job.Intended.UnixNano()}
	now := time.Now()
	req := &collectortracepb.ExportTraceServiceRequest{}
	for i := 0; i < job.Traces; i++ {
		seq := job.FirstSeq + uint64(i)
		req.ResourceSpans = append(req.ResourceSpans,
			tmpls[plan.TemplateFor(seq)].Instance(cfg.RunID, seq, uint64(now.UnixNano()))...)
	}
	first := time.Now()
	rec.FirstSendNs = first.UnixNano()
	StampSentAt(req.ResourceSpans, rec.FirstSendNs)
	var body []byte
	if snd.needsBody() {
		body, _ = proto.Marshal(req)
	}

	var codes []string
	for {
		rec.Attempts++
		ctx, cancel := context.WithTimeout(context.Background(), cfg.Timeout)
		o := snd.send(ctx, req, body)
		cancel()
		codes = append(codes, o.Code)
		rec.Code = o.Code
		if o.OK {
			rec.OK, rec.Rejected = true, o.Rejected
			break
		}
		delay := bo.Delay(rec.Attempts, o.RetryAfter)
		if !o.Retryable || time.Since(first)+delay > bo.MaxElapsed {
			break
		}
		time.Sleep(delay)
	}
	rec.DoneNs = time.Now().UnixNano()
	return rec, codes
}

// cpuSampler records this process's CPU use (user+sys) each second, as a fraction of one
// core. The generator is pinned to one core, so a sample near 1.0 means it is saturated.
type cpuSampler struct {
	stopCh  chan struct{}
	done    chan struct{}
	samples []float64
}

func startCPUSampler() *cpuSampler {
	s := &cpuSampler{stopCh: make(chan struct{}), done: make(chan struct{})}
	go func() {
		defer close(s.done)
		prevCPU, prevWall := processCPU(), time.Now()
		t := time.NewTicker(time.Second)
		defer t.Stop()
		for {
			select {
			case <-s.stopCh:
				return
			case <-t.C:
				cpu, wall := processCPU(), time.Now()
				s.samples = append(s.samples, (cpu-prevCPU).Seconds()/wall.Sub(prevWall).Seconds())
				prevCPU, prevWall = cpu, wall
			}
		}
	}()
	return s
}

func (s *cpuSampler) stop() []float64 {
	close(s.stopCh)
	<-s.done
	return s.samples
}

func processCPU() time.Duration {
	var ru syscall.Rusage
	if err := syscall.Getrusage(syscall.RUSAGE_SELF, &ru); err != nil {
		return 0
	}
	return time.Duration(ru.Utime.Nano() + ru.Stime.Nano())
}
