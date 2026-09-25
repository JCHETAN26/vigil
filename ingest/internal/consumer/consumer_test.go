package consumer

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"testing"
	"time"

	tracepb "go.opentelemetry.io/proto/otlp/trace/v1"
	"google.golang.org/protobuf/proto"

	"github.com/twmb/franz-go/pkg/kgo"

	"github.com/JCHETAN26/vigil/ingest/internal/chsink"
	"github.com/JCHETAN26/vigil/ingest/internal/pricing"
	"github.com/JCHETAN26/vigil/ingest/internal/producer"
)

// --- fakes ---

type fakeSink struct {
	inserts []insertCall
	err     error
}

type insertCall struct {
	token string
	rows  int
}

func (f *fakeSink) Insert(_ context.Context, rows []chsink.Row, token string) error {
	if f.err != nil {
		return f.err
	}
	f.inserts = append(f.inserts, insertCall{token: token, rows: len(rows)})
	return nil
}
func (f *fakeSink) Ping(context.Context) error { return nil }

type fakeDLQ struct{ count int }

func (f *fakeDLQ) ProduceSync(_ context.Context, recs ...producer.Record) error {
	f.count += len(recs)
	return nil
}

func newTestConsumer(sink spanSink, dlq dlqPublisher, commit func(context.Context, *kgo.Record) error) *Consumer {
	prices, _ := pricing.Load()
	return &Consumer{
		sink:     sink,
		dlq:      dlq,
		prices:   prices,
		commit:   commit,
		log:      slog.New(slog.NewTextHandler(io.Discard, nil)),
		topic:    "otlp.spans.raw",
		dlqTopic: "spans.dlq",
		maxRows:  10000,
		maxBytes: 32 << 20,
		interval: time.Second,
		batches:  map[int32]*batch{},
	}
}

// oneSpanOTLP marshals a valid single-span TracesData for a given trace/span id byte.
func oneSpanOTLP(t *testing.T, traceByte, spanByte byte) []byte {
	t.Helper()
	td := &tracepb.TracesData{ResourceSpans: []*tracepb.ResourceSpans{{
		ScopeSpans: []*tracepb.ScopeSpans{{
			Spans: []*tracepb.Span{{
				TraceId:           append([]byte{traceByte}, make([]byte, 15)...),
				SpanId:            append([]byte{spanByte}, make([]byte, 7)...),
				StartTimeUnixNano: 100,
				EndTimeUnixNano:   200,
			}},
		}},
	}}}
	b, err := proto.Marshal(td)
	if err != nil {
		t.Fatalf("marshal OTLP: %v", err)
	}
	return b
}

func recWith(partition int32, offset int64, key string, value []byte) *kgo.Record {
	return &kgo.Record{Partition: partition, Offset: offset, Key: []byte(key), Value: value}
}

// TestFlushOnRevoke verifies that revoking a partition flushes its in-flight batch to the
// sink (with the offset-range token) and commits the last offset before releasing it.
func TestFlushOnRevoke(t *testing.T) {
	sink := &fakeSink{}
	dlq := &fakeDLQ{}
	var committed []int64
	commit := func(_ context.Context, r *kgo.Record) error {
		committed = append(committed, r.Offset)
		return nil
	}
	c := newTestConsumer(sink, dlq, commit)
	ctx := context.Background()

	c.mu.Lock()
	if err := c.ingestRecordLocked(ctx, recWith(0, 5, "a1", oneSpanOTLP(t, 0xa1, 1))); err != nil {
		t.Fatalf("ingest: %v", err)
	}
	if err := c.ingestRecordLocked(ctx, recWith(0, 6, "a2", oneSpanOTLP(t, 0xa2, 1))); err != nil {
		t.Fatalf("ingest: %v", err)
	}
	c.mu.Unlock()

	// No flush yet (below thresholds, no revoke).
	if len(sink.inserts) != 0 {
		t.Fatalf("expected no insert before revoke, got %d", len(sink.inserts))
	}

	c.onRevoked(ctx, nil, map[string][]int32{"otlp.spans.raw": {0}})

	if len(sink.inserts) != 1 {
		t.Fatalf("expected 1 insert on revoke, got %d", len(sink.inserts))
	}
	if want := "otlp.spans.raw:0:5:6"; sink.inserts[0].token != want {
		t.Errorf("token = %q, want %q", sink.inserts[0].token, want)
	}
	if sink.inserts[0].rows != 2 {
		t.Errorf("inserted rows = %d, want 2", sink.inserts[0].rows)
	}
	if len(committed) != 1 || committed[0] != 6 {
		t.Errorf("committed = %v, want [6] (last offset in range)", committed)
	}
	if _, ok := c.batches[0]; ok {
		t.Error("batch should be removed after flush")
	}
}

// TestCommitOnlyAfterSuccessfulInsert verifies that a failed insert does not commit and
// keeps the batch, so at-least-once replay can retry the same range.
func TestCommitOnlyAfterSuccessfulInsert(t *testing.T) {
	sink := &fakeSink{err: errors.New("clickhouse down")}
	dlq := &fakeDLQ{}
	commits := 0
	commit := func(context.Context, *kgo.Record) error { commits++; return nil }
	c := newTestConsumer(sink, dlq, commit)
	ctx := context.Background()

	c.mu.Lock()
	_ = c.ingestRecordLocked(ctx, recWith(0, 5, "a1", oneSpanOTLP(t, 0xa1, 1)))
	err := c.flushLocked(ctx, 0)
	c.mu.Unlock()

	if err == nil {
		t.Fatal("expected flush error when insert fails")
	}
	if commits != 0 {
		t.Errorf("commits = %d, want 0 (never commit on insert failure)", commits)
	}
	if _, ok := c.batches[0]; !ok {
		t.Error("batch should be retained after a failed insert for replay")
	}
}

// TestNormalizationFailureRoutesToDLQ verifies a record that cannot be normalized is sent
// to the DLQ, contributes no rows, but still extends the committed offset range.
func TestNormalizationFailureRoutesToDLQ(t *testing.T) {
	sink := &fakeSink{}
	dlq := &fakeDLQ{}
	var committed []int64
	commit := func(_ context.Context, r *kgo.Record) error {
		committed = append(committed, r.Offset)
		return nil
	}
	c := newTestConsumer(sink, dlq, commit)
	ctx := context.Background()

	c.mu.Lock()
	// Good record, then a garbage (unparseable) record, then another good one.
	_ = c.ingestRecordLocked(ctx, recWith(0, 5, "a1", oneSpanOTLP(t, 0xa1, 1)))
	_ = c.ingestRecordLocked(ctx, recWith(0, 6, "bad", []byte{0xff, 0xff, 0xff, 0xff}))
	_ = c.ingestRecordLocked(ctx, recWith(0, 7, "a3", oneSpanOTLP(t, 0xa3, 1)))
	err := c.flushLocked(ctx, 0)
	c.mu.Unlock()

	if err != nil {
		t.Fatalf("flush: %v", err)
	}
	if dlq.count != 1 {
		t.Errorf("dlq publishes = %d, want 1", dlq.count)
	}
	if len(sink.inserts) != 1 || sink.inserts[0].rows != 2 {
		t.Fatalf("expected 1 insert with 2 rows (bad record excluded), got %+v", sink.inserts)
	}
	if want := "otlp.spans.raw:0:5:7"; sink.inserts[0].token != want {
		t.Errorf("token = %q, want %q (range covers the DLQ'd offset)", sink.inserts[0].token, want)
	}
	if len(committed) != 1 || committed[0] != 7 {
		t.Errorf("committed = %v, want [7]", committed)
	}
}
