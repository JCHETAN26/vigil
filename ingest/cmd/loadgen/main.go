// Command loadgen replays real recorded traces against the OTLP receiver.
//
//	loadgen export     read traces from the real `spans` table (read-only credentials when
//	                   CLICKHOUSE_RO_USER is set), verify every row round-trips exactly
//	                   through the production normalizer, and write the template file
//	loadgen run        send templates at a fixed open-loop rate over gRPC or HTTP, logging
//	                   every request (requests.csv.gz) and a summary (summary.json)
//	loadgen reconcile  wait for the writer to drain, then compare what was acked with what
//	                   ClickHouse stored (missing / duplicate / unexpected spans), and
//	                   compute end-to-end latency (reconcile.json)
//
// Load data goes to the dedicated load pipeline (database vigil_load, topic
// otlp.spans.load); bench/ingest_load.py orchestrates runs and generates the results.
package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"sort"
	"strings"
	"syscall"
	"time"

	"github.com/ClickHouse/clickhouse-go/v2"
	"github.com/ClickHouse/clickhouse-go/v2/lib/driver"
	"github.com/twmb/franz-go/pkg/kadm"
	"github.com/twmb/franz-go/pkg/kgo"
	tracepb "go.opentelemetry.io/proto/otlp/trace/v1"

	"github.com/JCHETAN26/vigil/ingest/internal/chsink"
	"github.com/JCHETAN26/vigil/ingest/internal/config"
	"github.com/JCHETAN26/vigil/ingest/internal/loadgen"
	"github.com/JCHETAN26/vigil/ingest/internal/pricing"
)

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: loadgen export|run|reconcile [flags]")
		os.Exit(2)
	}
	var err error
	switch os.Args[1] {
	case "export":
		err = cmdExport(os.Args[2:])
	case "run":
		err = cmdRun(os.Args[2:])
	case "reconcile":
		err = cmdReconcile(os.Args[2:])
	default:
		err = fmt.Errorf("unknown subcommand %q", os.Args[1])
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "loadgen:", err)
		os.Exit(1)
	}
}

// ---- export ----

// TemplateManifest describes a template file (written next to it as <file>.json).
type TemplateManifest struct {
	ExportedAt     string                 `json:"exported_at"`
	SourceDatabase string                 `json:"source_database"`
	SourceUser     string                 `json:"source_user"`
	Agents         []string               `json:"agents"`
	Traces         int                    `json:"traces"`
	Spans          int                    `json:"spans"`
	PerAgent       map[string][2]int      `json:"per_agent_traces_spans"`
	FileBytes      int64                  `json:"file_bytes"`
	SHA256         string                 `json:"sha256"`
	PricesAsOf     string                 `json:"prices_as_of"`
	Fidelity       loadgen.FidelityReport `json:"fidelity"`
}

func cmdExport(args []string) error {
	fs := flag.NewFlagSet("export", flag.ExitOnError)
	out := fs.String("out", "../data/loadtest/templates.v1.pb", "template file to write")
	agents := fs.String("agents", "hotpotqa-agent,tau2-retail-agent,hello-agent", "agent_ids to export (real agents only)")
	db := fs.String("db", "vigil", "source database (the real one; read-only access suffices)")
	_ = fs.Parse(args)

	user, pass := os.Getenv("CLICKHOUSE_RO_USER"), os.Getenv("CLICKHOUSE_RO_PASSWORD")
	if user == "" {
		user, pass = os.Getenv("CLICKHOUSE_USER"), os.Getenv("CLICKHOUSE_PASSWORD")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	conn, err := openCH(ctx, *db, user, pass)
	if err != nil {
		return err
	}
	defer conn.Close()

	agentList := strings.Split(*agents, ",")
	byTrace, err := readRows(ctx, conn, agentList)
	if err != nil {
		return err
	}
	prices, err := pricing.Load()
	if err != nil {
		return err
	}

	man := TemplateManifest{ExportedAt: time.Now().UTC().Format(time.RFC3339), SourceDatabase: *db,
		SourceUser: user, Agents: agentList, PerAgent: map[string][2]int{}, PricesAsOf: prices.PricesAsOf}
	traceIDs := make([]string, 0, len(byTrace))
	for tid := range byTrace {
		traceIDs = append(traceIDs, tid)
	}
	sort.Strings(traceIDs)
	var traces []*tracepb.TracesData
	for _, tid := range traceIDs {
		rows := byTrace[tid]
		if err := loadgen.CheckRoundTrip(rows, prices, &man.Fidelity); err != nil {
			return fmt.Errorf("round trip %s: %w", tid, err)
		}
		td, err := loadgen.Denormalize(rows)
		if err != nil {
			return err
		}
		traces = append(traces, td)
		pa := man.PerAgent[rows[0].AgentID]
		man.PerAgent[rows[0].AgentID] = [2]int{pa[0] + 1, pa[1] + len(rows)}
		man.Traces++
		man.Spans += len(rows)
	}
	if man.Fidelity.Mismatches > 0 {
		b, _ := json.MarshalIndent(man.Fidelity, "", "  ")
		return fmt.Errorf("fidelity check failed — templates would not reproduce the stored rows:\n%s", b)
	}
	if err := os.MkdirAll(filepath.Dir(*out), 0o755); err != nil {
		return err
	}
	if err := loadgen.WriteTemplates(*out, traces); err != nil {
		return err
	}
	if man.SHA256, man.FileBytes, err = fileDigest(*out); err != nil {
		return err
	}
	if err := writeJSON(*out+".json", man); err != nil {
		return err
	}
	fmt.Printf("exported %d traces / %d spans -> %s (fidelity: %d spans, %d mismatches, %d cost-only diffs)\n",
		man.Traces, man.Spans, *out, man.Fidelity.Spans, man.Fidelity.Mismatches, man.Fidelity.CostMismatches)
	return nil
}

// readRows reads every span of the given agents, grouped by trace id.
func readRows(ctx context.Context, conn driver.Conn, agents []string) (map[string][]chsink.Row, error) {
	rows, err := conn.Query(ctx, `
SELECT trace_id, span_id, parent_span_id, trace_state, start_time, end_time,
       toString(span_name), toString(span_kind), toString(status_code), status_message,
       toString(service_name), toString(service_version),
       toString(agent_id), toString(agent_version), toString(git_sha), run_id, toString(run_kind),
       eval_run_id, eval_case_id, eval_trial, session_id, toString(role),
       toString(gen_ai_system), toString(gen_ai_operation_name),
       toString(gen_ai_request_model), toString(gen_ai_response_model),
       gen_ai_usage_input_tokens, gen_ai_usage_output_tokens,
       gen_ai_usage_cache_creation_input_tokens, gen_ai_usage_cache_read_input_tokens,
       cost_usd, cache_hit,
       CAST(resource_attributes, 'Map(String, String)'), CAST(span_attributes, 'Map(String, String)'),
       events.timestamp, arrayMap(x -> toString(x), events.name), events.attributes
FROM spans FINAL
WHERE agent_id IN $1
ORDER BY trace_id, start_time, span_id`, agents)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string][]chsink.Row{}
	for rows.Next() {
		var r chsink.Row
		if err := rows.Scan(&r.TraceID, &r.SpanID, &r.ParentSpanID, &r.TraceState, &r.StartTime, &r.EndTime,
			&r.SpanName, &r.SpanKind, &r.StatusCode, &r.StatusMessage, &r.ServiceName, &r.ServiceVersion,
			&r.AgentID, &r.AgentVersion, &r.GitSHA, &r.RunID, &r.RunKind,
			&r.EvalRunID, &r.EvalCaseID, &r.EvalTrial, &r.SessionID, &r.Role,
			&r.GenAISystem, &r.GenAIOperationName, &r.GenAIRequestModel, &r.GenAIResponseModel,
			&r.GenAIUsageInputTokens, &r.GenAIUsageOutputTokens,
			&r.GenAIUsageCacheCreationInputTokens, &r.GenAIUsageCacheReadInputTokens,
			&r.CostUSD, &r.CacheHit, &r.ResourceAttributes, &r.SpanAttributes,
			&r.EventsTimestamp, &r.EventsName, &r.EventsAttributes); err != nil {
			return nil, err
		}
		out[r.TraceID] = append(out[r.TraceID], r)
	}
	return out, rows.Err()
}

// ---- run ----

func cmdRun(args []string) error {
	fs := flag.NewFlagSet("run", flag.ExitOnError)
	tpl := fs.String("templates", "../data/loadtest/templates.v1.pb", "template file")
	outDir := fs.String("out-dir", "", "directory for requests.csv.gz + summary.json (required)")
	cfg := loadgen.Config{}
	fs.StringVar(&cfg.Protocol, "protocol", "grpc", "grpc | http")
	fs.StringVar(&cfg.Endpoint, "endpoint", "", "receiver host:port (default: load receiver 14317 grpc / 14318 http)")
	fs.StringVar(&cfg.RunID, "run-id", "", "load run id (required; tags every span)")
	fs.Float64Var(&cfg.Rate, "rate", 500, "offered load, spans/sec")
	fs.DurationVar(&cfg.Duration, "duration", 30*time.Second, "how long to offer load")
	fs.IntVar(&cfg.MaxSpans, "max-spans", 512, "max spans per request (SDK default batch size)")
	fs.IntVar(&cfg.MaxBytes, "max-bytes", 3<<20, "max encoded bytes per request (receiver limit is 4 MiB)")
	fs.IntVar(&cfg.Concurrency, "concurrency", 32, "max in-flight requests")
	fs.DurationVar(&cfg.Timeout, "timeout", 10*time.Second, "per-attempt timeout")
	fs.DurationVar(&cfg.MaxRetryElapsed, "max-retry-elapsed", 5*time.Minute, "give up a request after this long")
	fs.Int64Var(&cfg.Seed, "seed", 1, "template order seed")
	_ = fs.Parse(args)
	if *outDir == "" || cfg.RunID == "" {
		return errors.New("--out-dir and --run-id are required")
	}
	if cfg.Endpoint == "" {
		cfg.Endpoint = map[string]string{"grpc": "127.0.0.1:14317", "http": "127.0.0.1:14318"}[cfg.Protocol]
	}

	tds, err := loadgen.ReadTemplates(*tpl)
	if err != nil {
		return err
	}
	tmpls := make([]*loadgen.Template, len(tds))
	for i, td := range tds {
		tmpls[i] = loadgen.Prepare(td, cfg.RunID)
	}
	if err := os.MkdirAll(*outDir, 0o755); err != nil {
		return err
	}
	logf, err := os.Create(filepath.Join(*outDir, "requests.csv.gz"))
	if err != nil {
		return err
	}
	defer logf.Close()

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	sum, err := loadgen.Run(ctx, cfg, tmpls, logf)
	if err != nil {
		return err
	}
	digest, _, err := fileDigest(*tpl)
	if err != nil {
		return err
	}
	out := struct {
		loadgen.Summary
		Templates       string `json:"templates"`
		TemplatesSHA256 string `json:"templates_sha256"`
	}{sum, *tpl, digest}
	if err := writeJSON(filepath.Join(*outDir, "summary.json"), out); err != nil {
		return err
	}
	fmt.Printf("run %s (%s @ %.0f spans/s for %s): %d requests, %d acked spans (%.0f/s), %d unacked, "+
		"request p99 %.0f ms, schedule lag p99 %.0f ms, generator CPU p95 %.2f core\n",
		cfg.RunID, cfg.Protocol, cfg.Rate, cfg.Duration, sum.Requests, sum.SpansAcked, sum.AckedRate,
		sum.SpansUnacked, sum.LatencyMs.P99, sum.ScheduleLagMs.P99, sum.CPU.P95)
	return nil
}

// ---- reconcile ----

// ReconcileReport is reconcile.json.
type ReconcileReport struct {
	RunID               string                 `json:"run_id"`
	Database            string                 `json:"database"`
	DrainedIn           string                 `json:"drained_in"`
	Reconciliation      loadgen.Reconciliation `json:"reconciliation"`
	Clean               bool                   `json:"clean"`
	TraceIndexSpanCount int64                  `json:"trace_index_span_count"` // must equal stored rows
	DLQRecords          int64                  `json:"dlq_records"`            // total in the load DLQ topic
	RawTopicBytes       int64                  `json:"raw_topic_bytes"`        // on-disk size of the raw topic
	E2ELatencyMs        map[string]float64     `json:"e2e_latency_ms"`         // ingested_at - sent_at, per span
}

type runSummary struct {
	Config          loadgen.Config `json:"config"`
	Templates       string         `json:"templates"`
	TemplatesSHA256 string         `json:"templates_sha256"`
}

func cmdReconcile(args []string) error {
	fs := flag.NewFlagSet("reconcile", flag.ExitOnError)
	runDir := fs.String("run-dir", "", "directory written by loadgen run (required)")
	drainTimeout := fs.Duration("drain-timeout", 5*time.Minute, "max wait for the writer to catch up")
	_ = fs.Parse(args)
	if *runDir == "" {
		return errors.New("--run-dir is required")
	}
	var rs runSummary
	if err := readJSON(filepath.Join(*runDir, "summary.json"), &rs); err != nil {
		return err
	}
	digest, _, err := fileDigest(rs.Templates)
	if err != nil {
		return err
	}
	if digest != rs.TemplatesSHA256 {
		return fmt.Errorf("template file %s changed since the run (sha256 mismatch)", rs.Templates)
	}
	tds, err := loadgen.ReadTemplates(rs.Templates)
	if err != nil {
		return err
	}
	f, err := os.Open(filepath.Join(*runDir, "requests.csv.gz"))
	if err != nil {
		return err
	}
	recs, err := loadgen.ReadRecords(f)
	f.Close()
	if err != nil {
		return err
	}

	// Rebuild the plan's seq → template mapping to know each trace's expected span count.
	spans := make([]int, len(tds))
	for i, td := range tds {
		spans[i] = loadgen.Prepare(td, rs.Config.RunID).Spans()
	}
	plan := loadgen.NewPlan(spans, nil, 0, 0, rs.Config.Seed) // only TemplateFor is used
	acked, unacked := map[string]int{}, map[string]int{}
	for _, r := range recs {
		dst := acked
		if !r.OK || r.Rejected > 0 {
			dst = unacked // partial rejection: cannot tell which spans, so not counted as acked
		}
		for i := 0; i < r.Traces; i++ {
			seq := r.FirstSeq + uint64(i)
			dst[loadgen.TraceIDHex(rs.Config.RunID, seq)] = spans[plan.TemplateFor(seq)]
		}
	}

	ch := config.ClickHouseFromEnv()
	ctx, cancel := context.WithTimeout(context.Background(), *drainTimeout+2*time.Minute)
	defer cancel()
	conn, err := openCH(ctx, ch.Database, ch.Username, ch.Password)
	if err != nil {
		return err
	}
	defer conn.Close()

	rep := ReconcileReport{RunID: rs.Config.RunID, Database: ch.Database}
	t0 := time.Now()
	if err := waitDrained(ctx, conn, rs.Config.RunID, *drainTimeout); err != nil {
		return err
	}
	rep.DrainedIn = time.Since(t0).Round(time.Millisecond).String()

	stored := map[string]loadgen.StoredTrace{}
	rows, err := conn.Query(ctx,
		`SELECT trace_id, count(), uniqExact(span_id) FROM spans WHERE run_id = $1 GROUP BY trace_id`, rs.Config.RunID)
	if err != nil {
		return err
	}
	for rows.Next() {
		var tid string
		var n, u uint64
		if err := rows.Scan(&tid, &n, &u); err != nil {
			return err
		}
		stored[tid] = loadgen.StoredTrace{Rows: int64(n), Unique: int64(u)}
	}
	rows.Close()
	rep.Reconciliation = loadgen.Reconcile(acked, unacked, stored)
	rep.Clean = rep.Reconciliation.Clean()

	var ti uint64
	if err := conn.QueryRow(ctx, `SELECT sum(span_count) FROM trace_index WHERE run_id = $1`,
		rs.Config.RunID).Scan(&ti); err != nil {
		return err
	}
	rep.TraceIndexSpanCount = int64(ti)
	if rep.TraceIndexSpanCount != rep.Reconciliation.StoredRows {
		rep.Clean = false
	}

	var q []float64
	var mx float64
	if err := conn.QueryRow(ctx, `
SELECT quantilesExact(0.5, 0.95, 0.99)(lat), max(lat) FROM (
  SELECT toFloat64(toUnixTimestamp64Milli(ingested_at))
         - toInt64(resource_attributes['`+loadgen.KeySentAtNano+`']) / 1e6 AS lat
  FROM spans WHERE run_id = $1)`, rs.Config.RunID).Scan(&q, &mx); err != nil {
		return err
	}
	rep.E2ELatencyMs = map[string]float64{"p50": q[0], "p95": q[1], "p99": q[2], "max": mx}

	if rep.DLQRecords, err = dlqRecords(ctx); err != nil {
		return err
	}
	if rep.RawTopicBytes, err = topicBytes(ctx, config.WriterFromEnv().RawTopic); err != nil {
		return err
	}
	if rep.DLQRecords > 0 {
		rep.Clean = false
	}
	if err := writeJSON(filepath.Join(*runDir, "reconcile.json"), rep); err != nil {
		return err
	}
	r := rep.Reconciliation
	fmt.Printf("reconcile %s: acked %d, stored %d/%d, missing %d, duplicates %d, unexpected %d, "+
		"unacked %d (%d stored), trace_index %d, dlq %d, e2e p50/p99 %.0f/%.0f ms -> clean=%v\n",
		rep.RunID, r.AckedSpans, r.StoredAckedUnique, r.AckedSpans, r.MissingSpans, r.DuplicateRows,
		r.UnexpectedTraces, r.UnackedSpans, r.UnackedStored, rep.TraceIndexSpanCount, rep.DLQRecords,
		q[0], q[2], rep.Clean)
	if !rep.Clean {
		return errors.New("reconciliation NOT clean")
	}
	return nil
}

// waitDrained waits until the load writer's consumer group has no lag on the raw topic and
// the run's stored row count has stopped changing across two flush intervals.
func waitDrained(ctx context.Context, conn driver.Conn, runID string, timeout time.Duration) error {
	kcfg, wcfg := config.KafkaFromEnv(), config.WriterFromEnv()
	cl, err := kgo.NewClient(kgo.SeedBrokers(kcfg.Brokers...))
	if err != nil {
		return err
	}
	defer cl.Close()
	adm := kadm.NewClient(cl)
	deadline := time.Now().Add(timeout)
	var prev uint64 = ^uint64(0)
	for time.Now().Before(deadline) {
		lags, err := adm.Lag(ctx, wcfg.Group)
		if err != nil {
			return err
		}
		total := int64(0)
		lags.Each(func(l kadm.DescribedGroupLag) {
			for _, pl := range l.Lag[wcfg.RawTopic] {
				total += pl.Lag
			}
		})
		var n uint64
		if err := conn.QueryRow(ctx, `SELECT count() FROM spans WHERE run_id = $1`, runID).Scan(&n); err != nil {
			return err
		}
		if total == 0 && n == prev {
			return nil
		}
		prev = n
		time.Sleep(wcfg.FlushInterval + 500*time.Millisecond)
	}
	return fmt.Errorf("writer did not drain within %s", timeout)
}

func dlqRecords(ctx context.Context) (int64, error) {
	kcfg, wcfg := config.KafkaFromEnv(), config.WriterFromEnv()
	cl, err := kgo.NewClient(kgo.SeedBrokers(kcfg.Brokers...))
	if err != nil {
		return 0, err
	}
	defer cl.Close()
	adm := kadm.NewClient(cl)
	ends, err := adm.ListEndOffsets(ctx, wcfg.DLQTopic)
	if err != nil {
		return 0, err
	}
	starts, err := adm.ListStartOffsets(ctx, wcfg.DLQTopic)
	if err != nil {
		return 0, err
	}
	var n int64
	ends.Each(func(o kadm.ListedOffset) {
		if s, ok := starts.Lookup(o.Topic, o.Partition); ok {
			n += o.Offset - s.Offset
		}
	})
	return n, nil
}

// topicBytes sums the on-disk log size of every partition of topic (DescribeLogDirs), for
// disk calibration.
func topicBytes(ctx context.Context, topic string) (int64, error) {
	cl, err := kgo.NewClient(kgo.SeedBrokers(config.KafkaFromEnv().Brokers...))
	if err != nil {
		return 0, err
	}
	defer cl.Close()
	dirs, err := kadm.NewClient(cl).DescribeAllLogDirs(ctx, nil)
	if err != nil {
		return 0, err
	}
	var n int64
	dirs.Each(func(d kadm.DescribedLogDir) {
		d.Topics.Each(func(p kadm.DescribedLogDirPartition) {
			if p.Topic == topic {
				n += p.Size
			}
		})
	})
	return n, nil
}

// ---- helpers ----

func openCH(ctx context.Context, db, user, pass string) (driver.Conn, error) {
	conn, err := clickhouse.Open(&clickhouse.Options{
		Addr: []string{config.ClickHouseFromEnv().Addr},
		Auth: clickhouse.Auth{Database: db, Username: user, Password: pass},
	})
	if err != nil {
		return nil, err
	}
	if err := conn.Ping(ctx); err != nil {
		return nil, fmt.Errorf("ping clickhouse (db %s): %w", db, err)
	}
	return conn, nil
}

func fileDigest(path string) (string, int64, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", 0, err
	}
	defer f.Close()
	h := sha256.New()
	n, err := io.Copy(h, f)
	if err != nil {
		return "", 0, err
	}
	return hex.EncodeToString(h.Sum(nil)), n, nil
}

func writeJSON(path string, v any) error {
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, append(b, '\n'), 0o644)
}

func readJSON(path string, v any) error {
	b, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	return json.Unmarshal(b, v)
}
