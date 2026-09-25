//go:build integration

package integration

import (
	"bytes"
	"context"
	"encoding/hex"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/JCHETAN26/vigil/ingest/internal/chsink"
	"github.com/JCHETAN26/vigil/ingest/internal/config"
	"github.com/JCHETAN26/vigil/ingest/internal/consumer"
	"github.com/JCHETAN26/vigil/ingest/internal/pricing"
	"github.com/JCHETAN26/vigil/ingest/internal/producer"
	"github.com/JCHETAN26/vigil/ingest/internal/receiver"
)

// TestReceiverToSpansRoundtrip drives the full pipeline in-process: an OTLP request posted
// to the receiver's HTTP handler is published to Redpanda, consumed by the writer, and
// lands in the spans table with its promoted columns.
func TestReceiverToSpansRoundtrip(t *testing.T) {
	conn := mustCH(t)
	log := discardLogger()

	suffix := uniqueSuffix()
	av := "itest-roundtrip-" + suffix
	rawTopic := "itest.raw." + suffix
	dlqTopic := "itest.dlq." + suffix
	group := "itest-group-" + suffix

	adm := newAdmin(t)
	createTopic(t, adm, rawTopic, 1)
	createTopic(t, adm, dlqTopic, 1)

	// Receiver side: HTTP handler → producer → rawTopic.
	prod, err := producer.New(brokers())
	if err != nil {
		t.Fatalf("producer: %v", err)
	}
	defer func() { _ = prod.Close(context.Background()) }()
	rHandler := receiver.NewHandler(receiver.NewKafkaSink(prod, rawTopic, dlqTopic), log)
	srv := httptest.NewServer(receiver.NewHTTPHandler(rHandler, 4<<20, log))
	defer srv.Close()

	// Writer side: consumer(rawTopic) → ClickHouse.
	sink, err := chsink.Open(context.Background(), config.ClickHouseFromEnv())
	if err != nil {
		t.Fatalf("sink: %v", err)
	}
	defer sink.Close()
	dlqProd, err := producer.New(brokers())
	if err != nil {
		t.Fatalf("dlq producer: %v", err)
	}
	defer func() { _ = dlqProd.Close(context.Background()) }()
	prices, err := pricing.Load()
	if err != nil {
		t.Fatalf("pricing: %v", err)
	}
	wcfg := config.Writer{
		Group: group, RawTopic: rawTopic, DLQTopic: dlqTopic,
		BatchMaxRows: 100, BatchMaxBytes: 1 << 20, FlushInterval: 300 * time.Millisecond,
	}
	cons, err := consumer.New(config.Kafka{Brokers: brokers()}, wcfg, sink, dlqProd, prices, log)
	if err != nil {
		t.Fatalf("consumer: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- cons.Run(ctx) }()

	// Send one trace through the receiver.
	traceID := traceIDBytes(0)
	body := otlpTraceBytes(t, av, traceID, spanIDBytes(0))
	resp, err := http.Post(srv.URL+"/v1/traces", "application/x-protobuf", bytes.NewReader(body))
	if err != nil {
		t.Fatalf("post: %v", err)
	}
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("receiver status = %d, want 200", resp.StatusCode)
	}
	_ = resp.Body.Close()

	tidHex := hex.EncodeToString(traceID)
	poll(t, 20*time.Second, "trace to land in spans", func() bool {
		return queryU64(t, conn, "SELECT count() FROM spans WHERE trace_id = ? AND agent_version = ?", tidHex, av) >= 1
	})

	// Verify the promoted columns came through the full path.
	var (
		agentID    string
		runKind    string
		respModel  string
		totalToken uint64
	)
	qctx, qcancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer qcancel()
	row := conn.QueryRow(qctx,
		"SELECT agent_id, toString(run_kind), gen_ai_response_model, toUInt64(gen_ai_usage_total_tokens) FROM spans WHERE trace_id = ? AND agent_version = ? LIMIT 1",
		tidHex, av)
	if err := row.Scan(&agentID, &runKind, &respModel, &totalToken); err != nil {
		t.Fatalf("scan span: %v", err)
	}
	if agentID != "itest" || runKind != "eval" || respModel != "claude-haiku-4-5" || totalToken != 300 {
		t.Errorf("promoted columns wrong: agent_id=%q run_kind=%q model=%q total_tokens=%d",
			agentID, runKind, respModel, totalToken)
	}

	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Errorf("consumer Run returned error: %v", err)
		}
	case <-time.After(15 * time.Second):
		t.Error("consumer did not stop after cancel")
	}
	cons.Close()
}
