//go:build integration

// Package integration holds tests that run against the live Docker stack (ClickHouse and
// Redpanda). Run with: make test-integration (which sources ../.env). All test data uses
// current timestamps so it is not immediately expired by the spans/trace_index TTL, and
// each test isolates itself with a unique agent_version and unique topic/consumer-group
// names.
package integration

import (
	"context"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"testing"
	"time"

	"github.com/ClickHouse/clickhouse-go/v2"
	"github.com/ClickHouse/clickhouse-go/v2/lib/driver"
	"github.com/twmb/franz-go/pkg/kadm"
	"github.com/twmb/franz-go/pkg/kerr"
	"github.com/twmb/franz-go/pkg/kgo"
	commonpb "go.opentelemetry.io/proto/otlp/common/v1"
	resourcepb "go.opentelemetry.io/proto/otlp/resource/v1"
	tracepb "go.opentelemetry.io/proto/otlp/trace/v1"
	"google.golang.org/protobuf/proto"

	"github.com/JCHETAN26/vigil/ingest/internal/chsink"
	"github.com/JCHETAN26/vigil/ingest/internal/config"
	"github.com/JCHETAN26/vigil/ingest/internal/schema"
)

// TestMain applies the schema (idempotent) before running the integration tests.
func TestMain(m *testing.M) {
	if os.Getenv("CLICKHOUSE_PASSWORD") == "" {
		fmt.Fprintln(os.Stderr, "integration tests need env (source ../.env): CLICKHOUSE_PASSWORD is unset")
		os.Exit(1)
	}
	conn, err := openCHConn()
	if err != nil {
		fmt.Fprintln(os.Stderr, "open clickhouse:", err)
		os.Exit(1)
	}
	if err := applySchema(conn); err != nil {
		fmt.Fprintln(os.Stderr, "apply schema:", err)
		os.Exit(1)
	}
	_ = conn.Close()
	os.Exit(m.Run())
}

func discardLogger() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

func openCHConn() (driver.Conn, error) {
	cfg := config.ClickHouseFromEnv()
	conn, err := clickhouse.Open(&clickhouse.Options{
		Addr: []string{cfg.Addr},
		Auth: clickhouse.Auth{Database: cfg.Database, Username: cfg.Username, Password: cfg.Password},
	})
	if err != nil {
		return nil, err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := conn.Ping(ctx); err != nil {
		return nil, err
	}
	return conn, nil
}

func mustCH(t *testing.T) driver.Conn {
	t.Helper()
	conn, err := openCHConn()
	if err != nil {
		t.Fatalf("open clickhouse: %v", err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	return conn
}

func applySchema(conn driver.Conn) error {
	stmts, err := schema.Statements()
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	for _, s := range stmts {
		if err := conn.Exec(ctx, s.SQL); err != nil {
			return fmt.Errorf("%s: %w", s.File, err)
		}
	}
	return nil
}

func queryU64(t *testing.T, conn driver.Conn, query string, args ...any) uint64 {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	var v uint64
	if err := conn.QueryRow(ctx, query, args...).Scan(&v); err != nil {
		t.Fatalf("query %q: %v", query, err)
	}
	return v
}

// poll retries cond until it is true or the timeout elapses.
func poll(t *testing.T, timeout time.Duration, desc string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(200 * time.Millisecond)
	}
	t.Fatalf("timed out after %s waiting for %s", timeout, desc)
}

func brokers() []string { return config.KafkaFromEnv().Brokers }

func uniqueSuffix() string { return fmt.Sprintf("%d", time.Now().UnixNano()) }

// --- Redpanda topic admin ---

func newAdmin(t *testing.T) *kadm.Client {
	t.Helper()
	cl, err := kgo.NewClient(kgo.SeedBrokers(brokers()...))
	if err != nil {
		t.Fatalf("kafka client: %v", err)
	}
	t.Cleanup(cl.Close)
	return kadm.NewClient(cl)
}

func createTopic(t *testing.T, adm *kadm.Client, name string, partitions int32) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	resp, err := adm.CreateTopics(ctx, partitions, 1, nil, name)
	if err != nil {
		t.Fatalf("create topic %s: %v", name, err)
	}
	for _, r := range resp.Sorted() {
		if r.Err != nil && !errors.Is(r.Err, kerr.TopicAlreadyExists) {
			t.Fatalf("create topic %s: %v", name, r.Err)
		}
	}
	t.Cleanup(func() {
		dctx, dcancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer dcancel()
		_, _ = adm.DeleteTopics(dctx, name)
	})
}

// --- test data ---

func traceIDBytes(i int) []byte {
	b := make([]byte, 16)
	binary.BigEndian.PutUint64(b[8:], uint64(i+1))
	return b
}

func spanIDBytes(i int) []byte {
	b := make([]byte, 8)
	binary.BigEndian.PutUint64(b, uint64(i+1))
	return b
}

func kvStr(k, v string) *commonpb.KeyValue {
	return &commonpb.KeyValue{Key: k, Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: v}}}
}
func kvInt(k string, v int64) *commonpb.KeyValue {
	return &commonpb.KeyValue{Key: k, Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_IntValue{IntValue: v}}}
}

// otlpTraceBytes marshals a single-span per-trace OTLP payload with current timestamps.
func otlpTraceBytes(t *testing.T, agentVersion string, traceID, spanID []byte) []byte {
	t.Helper()
	now := uint64(time.Now().UnixNano())
	td := &tracepb.TracesData{ResourceSpans: []*tracepb.ResourceSpans{{
		Resource: &resourcepb.Resource{Attributes: []*commonpb.KeyValue{
			kvStr("service.name", "itest"),
			kvStr("vigil.agent.id", "itest"),
			kvStr("vigil.agent.version", agentVersion),
		}},
		ScopeSpans: []*tracepb.ScopeSpans{{
			Spans: []*tracepb.Span{{
				TraceId:           traceID,
				SpanId:            spanID,
				Name:              "chat",
				Kind:              tracepb.Span_SPAN_KIND_CLIENT,
				StartTimeUnixNano: now,
				EndTimeUnixNano:   now + 1_000_000,
				Status:            &tracepb.Status{Code: tracepb.Status_STATUS_CODE_OK},
				Attributes: []*commonpb.KeyValue{
					kvStr("vigil.run.kind", "eval"),
					kvStr("gen_ai.response.model", "claude-haiku-4-5"),
					kvInt("gen_ai.usage.input_tokens", 100),
					kvInt("gen_ai.usage.output_tokens", 200),
				},
			}},
		}},
	}}}
	b, err := proto.Marshal(td)
	if err != nil {
		t.Fatalf("marshal OTLP: %v", err)
	}
	return b
}

// makeSpanRows builds n chsink.Row values for a direct insert, current-timestamped and
// tagged with agentVersion. Each row has a distinct run_id so uniq(runs) == n.
func makeSpanRows(agentVersion string, n int) []chsink.Row {
	now := time.Now().UTC()
	rows := make([]chsink.Row, 0, n)
	for i := 0; i < n; i++ {
		tid := hex.EncodeToString(traceIDBytes(i))
		rows = append(rows, chsink.Row{
			TraceID:                tid,
			SpanID:                 hex.EncodeToString(spanIDBytes(i)),
			StartTime:              now,
			EndTime:                now.Add(time.Millisecond),
			SpanName:               "op",
			SpanKind:               "CLIENT",
			StatusCode:             "OK",
			ServiceName:            "itest",
			AgentID:                "itest",
			AgentVersion:           agentVersion,
			RunID:                  tid,
			RunKind:                "eval",
			GenAIOperationName:     "chat",
			GenAIResponseModel:     "claude-haiku-4-5",
			GenAIUsageInputTokens:  100,
			GenAIUsageOutputTokens: 200,
			CostUSD:                0.0011,
			ResourceAttributes:     map[string]string{},
			SpanAttributes:         map[string]string{},
		})
	}
	return rows
}

// rollup counts for an agent_version.
func rollupSpanCount(t *testing.T, conn driver.Conn, agentVersion string) uint64 {
	return queryU64(t, conn, "SELECT toUInt64(sum(span_count)) FROM agent_version_stats_daily WHERE agent_version = ?", agentVersion)
}
func rollupRuns(t *testing.T, conn driver.Conn, agentVersion string) uint64 {
	return queryU64(t, conn, "SELECT uniqMerge(runs) FROM agent_version_stats_daily WHERE agent_version = ?", agentVersion)
}
func rawSpanCount(t *testing.T, conn driver.Conn, agentVersion string) uint64 {
	return queryU64(t, conn, "SELECT count() FROM spans WHERE agent_version = ?", agentVersion)
}
func dedupedSpanCount(t *testing.T, conn driver.Conn, agentVersion string) uint64 {
	return queryU64(t, conn, "SELECT uniqExact((trace_id, span_id)) FROM spans WHERE agent_version = ?", agentVersion)
}
