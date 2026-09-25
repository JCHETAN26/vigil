// Command smoke sends one OTLP trace through the running pipeline (receiver → Redpanda →
// writer) and verifies it lands in ClickHouse. It is a fast end-to-end health check for a
// running stack: start the infra, `make migrate && make topics`, run the receiver and
// writer, then `make smoke`.
package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"time"

	"github.com/ClickHouse/clickhouse-go/v2"
	collectortracepb "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	commonpb "go.opentelemetry.io/proto/otlp/common/v1"
	resourcepb "go.opentelemetry.io/proto/otlp/resource/v1"
	tracepb "go.opentelemetry.io/proto/otlp/trace/v1"
	"google.golang.org/protobuf/proto"

	"github.com/JCHETAN26/vigil/ingest/internal/config"
	"github.com/JCHETAN26/vigil/ingest/internal/logging"
)

func main() {
	log := logging.New("smoke")
	if err := run(log); err != nil {
		log.Error("smoke failed", "err", err)
		os.Exit(1)
	}
}

func run(log *slog.Logger) error {
	rcfg := config.ReceiverFromEnv()
	chcfg := config.ClickHouseFromEnv()
	if chcfg.Password == "" {
		return fmt.Errorf("CLICKHOUSE_PASSWORD not set (source ../.env)")
	}

	// Unique trace + agent version so repeated runs never collide.
	traceID := make([]byte, 16)
	if _, err := rand.Read(traceID); err != nil {
		return err
	}
	spanID := make([]byte, 8)
	if _, err := rand.Read(spanID); err != nil {
		return err
	}
	tidHex := hex.EncodeToString(traceID)
	av := "smoke-" + fmt.Sprintf("%d", time.Now().UnixNano())

	body, err := proto.Marshal(buildRequest(av, traceID, spanID))
	if err != nil {
		return err
	}

	url := "http://" + rcfg.HTTPAddr + "/v1/traces"
	resp, err := http.Post(url, "application/x-protobuf", bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("POST %s failed — is the receiver running? (make receiver): %w", url, err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("receiver returned status %d", resp.StatusCode)
	}
	log.Info("trace posted to receiver", "url", url, "trace_id", tidHex, "agent_version", av)

	conn, err := clickhouse.Open(&clickhouse.Options{
		Addr: []string{chcfg.Addr},
		Auth: clickhouse.Auth{Database: chcfg.Database, Username: chcfg.Username, Password: chcfg.Password},
	})
	if err != nil {
		return err
	}
	defer conn.Close()

	deadline := time.Now().Add(20 * time.Second)
	for time.Now().Before(deadline) {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		var n uint64
		err := conn.QueryRow(ctx, "SELECT count() FROM spans WHERE trace_id = ?", tidHex).Scan(&n)
		cancel()
		if err == nil && n >= 1 {
			log.Info("SUCCESS: trace landed in ClickHouse", "trace_id", tidHex, "spans", n)
			return nil
		}
		time.Sleep(500 * time.Millisecond)
	}
	return fmt.Errorf("trace %s did not appear in spans within 20s — is the writer running? (make writer)", tidHex)
}

func buildRequest(agentVersion string, traceID, spanID []byte) *collectortracepb.ExportTraceServiceRequest {
	now := uint64(time.Now().UnixNano())
	return &collectortracepb.ExportTraceServiceRequest{
		ResourceSpans: []*tracepb.ResourceSpans{{
			Resource: &resourcepb.Resource{Attributes: []*commonpb.KeyValue{
				kvStr("service.name", "smoke-agent"),
				kvStr("vigil.agent.id", "smoke"),
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
		}},
	}
}

func kvStr(k, v string) *commonpb.KeyValue {
	return &commonpb.KeyValue{Key: k, Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: v}}}
}
func kvInt(k string, v int64) *commonpb.KeyValue {
	return &commonpb.KeyValue{Key: k, Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_IntValue{IntValue: v}}}
}
