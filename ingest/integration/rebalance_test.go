//go:build integration

package integration

import (
	"context"
	"encoding/hex"
	"testing"
	"time"

	"github.com/JCHETAN26/vigil/ingest/internal/chsink"
	"github.com/JCHETAN26/vigil/ingest/internal/config"
	"github.com/JCHETAN26/vigil/ingest/internal/consumer"
	"github.com/JCHETAN26/vigil/ingest/internal/pricing"
	"github.com/JCHETAN26/vigil/ingest/internal/producer"
)

type runningConsumer struct {
	cons   *consumer.Consumer
	done   chan error
	cancel context.CancelFunc
}

func startConsumer(t *testing.T, rawTopic, dlqTopic, group string) *runningConsumer {
	t.Helper()
	sink, err := chsink.Open(context.Background(), config.ClickHouseFromEnv())
	if err != nil {
		t.Fatalf("sink: %v", err)
	}
	t.Cleanup(func() { _ = sink.Close() })
	dlqProd, err := producer.New(brokers())
	if err != nil {
		t.Fatalf("dlq producer: %v", err)
	}
	t.Cleanup(func() { _ = dlqProd.Close(context.Background()) })
	prices, err := pricing.Load()
	if err != nil {
		t.Fatalf("pricing: %v", err)
	}
	wcfg := config.Writer{
		Group: group, RawTopic: rawTopic, DLQTopic: dlqTopic,
		BatchMaxRows: 4, BatchMaxBytes: 1 << 20, FlushInterval: 250 * time.Millisecond,
	}
	cons, err := consumer.New(config.Kafka{Brokers: brokers()}, wcfg, sink, dlqProd, prices, discardLogger())
	if err != nil {
		t.Fatalf("consumer: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- cons.Run(ctx) }()
	return &runningConsumer{cons: cons, done: done, cancel: cancel}
}

func (r *runningConsumer) stop(t *testing.T) {
	t.Helper()
	r.cancel()
	select {
	case err := <-r.done:
		if err != nil {
			t.Errorf("consumer Run error: %v", err)
		}
	case <-time.After(15 * time.Second):
		t.Error("consumer did not stop after cancel")
	}
	r.cons.Close()
}

// TestRebalanceNoLossNoDup runs two consumers in one group against a 2-partition topic
// while data flows, forcing a rebalance. Because revocation flushes and commits in-flight
// batches before releasing a partition (and offsets commit only after inserts), every
// record must land exactly once: no loss and no double-count in the rollup.
func TestRebalanceNoLossNoDup(t *testing.T) {
	conn := mustCH(t)

	const total = 20
	suffix := uniqueSuffix()
	av := "itest-rebalance-" + suffix
	rawTopic := "itest.raw." + suffix
	dlqTopic := "itest.dlq." + suffix
	group := "itest-group-" + suffix

	adm := newAdmin(t)
	createTopic(t, adm, rawTopic, 2)
	createTopic(t, adm, dlqTopic, 1)

	prod, err := producer.New(brokers())
	if err != nil {
		t.Fatalf("producer: %v", err)
	}
	defer func() { _ = prod.Close(context.Background()) }()

	produce := func(from, to int) {
		for i := from; i < to; i++ {
			tid := traceIDBytes(i)
			rec := producer.Record{
				Topic: rawTopic,
				Key:   hex.EncodeToString(tid),
				Value: otlpTraceBytes(t, av, tid, spanIDBytes(i)),
			}
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			if err := prod.ProduceSync(ctx, rec); err != nil {
				cancel()
				t.Fatalf("produce %d: %v", i, err)
			}
			cancel()
		}
	}

	// Wave 1, then consumer A, then consumer B (forces a rebalance), then wave 2.
	produce(0, total/2)
	a := startConsumer(t, rawTopic, dlqTopic, group)
	time.Sleep(600 * time.Millisecond)
	b := startConsumer(t, rawTopic, dlqTopic, group)
	produce(total/2, total)

	// All records must land exactly once.
	poll(t, 40*time.Second, "all records to land in spans", func() bool {
		return dedupedSpanCount(t, conn, av) == total
	})

	a.stop(t)
	b.stop(t)

	if got := dedupedSpanCount(t, conn, av); got != total {
		t.Errorf("deduped span count = %d, want %d (no loss / no dup)", got, total)
	}
	if got := rollupRuns(t, conn, av); got != total {
		t.Errorf("rollup uniq(runs) = %d, want %d (no loss)", got, total)
	}
	if got := rollupSpanCount(t, conn, av); got != total {
		t.Errorf("rollup span_count = %d, want %d (no double-count across the rebalance)", got, total)
	}
}
