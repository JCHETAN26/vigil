package consumer

import (
	"bytes"
	"context"
	"fmt"
	"slices"
	"testing"
	"time"

	"github.com/twmb/franz-go/pkg/kgo"
)

// sizedRec builds a record of exactly n value bytes that fails normalization (and is routed
// to the DLQ), so batch byte sizes are exact and independent of OTLP encoding.
func sizedRec(partition int32, offset int64, n int) *kgo.Record {
	return recWith(partition, offset, "k", bytes.Repeat([]byte{0xff}, n))
}

// flushedPartitions extracts the partition of each successful insert from its dedup token.
func flushedPartitions(t *testing.T, s *fakeSink) []int32 {
	t.Helper()
	var out []int32
	for _, in := range s.inserts {
		var p int32
		var first, last int64
		if _, err := fmt.Sscanf(in.token, "otlp.spans.raw:%d:%d:%d", &p, &first, &last); err != nil {
			t.Fatalf("parse token %q: %v", in.token, err)
		}
		out = append(out, p)
	}
	return out
}

type sized struct {
	p int32
	n int
}

// TestInflightBudget covers global multi-partition accounting, largest-first flush order
// with a deterministic tie-break, the oversized-record path, and that the per-partition
// byte limit still applies under the budget.
func TestInflightBudget(t *testing.T) {
	tests := []struct {
		name         string
		maxInflight  int
		maxBytes     int
		recs         []sized
		wantFlushed  []int32
		wantInflight int
		wantBatches  []int32
	}{
		{
			name: "accounting across partitions under budget", maxInflight: 1000,
			recs:         []sized{{0, 40}, {1, 50}, {2, 10}, {0, 5}},
			wantFlushed:  nil,
			wantInflight: 105, wantBatches: []int32{0, 1, 2},
		},
		{
			name: "budget disabled", maxInflight: 0,
			recs:         []sized{{0, 400}, {1, 500}},
			wantInflight: 900, wantBatches: []int32{0, 1},
		},
		{
			name: "exactly at budget is admitted", maxInflight: 100,
			recs:         []sized{{0, 40}, {1, 50}, {2, 10}},
			wantInflight: 100, wantBatches: []int32{0, 1, 2},
		},
		{
			name: "largest partition flushed first", maxInflight: 100,
			// 40+50+10 = 100; +30 needs room: flush p1 (50) → 80.
			// +70 on p2 needs room: flush p0 (40) → 110 still over, flush p3 (30) → 80.
			recs:         []sized{{0, 40}, {1, 50}, {2, 10}, {3, 30}, {2, 70}},
			wantFlushed:  []int32{1, 0, 3},
			wantInflight: 80, wantBatches: []int32{2},
		},
		{
			name: "tie broken by lowest partition", maxInflight: 100,
			recs:         []sized{{3, 50}, {1, 50}, {2, 10}},
			wantFlushed:  []int32{1},
			wantInflight: 60, wantBatches: []int32{2, 3},
		},
		{
			name: "oversized record flushes others then itself", maxInflight: 100,
			recs:         []sized{{0, 30}, {1, 20}, {2, 150}},
			wantFlushed:  []int32{0, 1, 2},
			wantInflight: 0, wantBatches: []int32{},
		},
		{
			name: "per-partition byte limit still applies", maxInflight: 1000, maxBytes: 60,
			recs:         []sized{{0, 40}, {1, 40}, {0, 40}},
			wantFlushed:  []int32{0},
			wantInflight: 40, wantBatches: []int32{1},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			sink := &fakeSink{}
			c := newTestConsumer(sink, &fakeDLQ{}, (&fakeCommitter{}).commit)
			c.maxInflight = tt.maxInflight
			if tt.maxBytes > 0 {
				c.maxBytes = tt.maxBytes
			}
			c.mu.Lock()
			defer c.mu.Unlock()
			offsets := map[int32]int64{}
			for _, r := range tt.recs {
				if err := c.consumeRecordLocked(context.Background(), sizedRec(r.p, offsets[r.p], r.n)); err != nil {
					t.Fatalf("consume %+v: %v", r, err)
				}
				offsets[r.p]++
			}
			if got := flushedPartitions(t, sink); !slices.Equal(got, tt.wantFlushed) {
				t.Errorf("flush order = %v, want %v", got, tt.wantFlushed)
			}
			if c.inflight != tt.wantInflight {
				t.Errorf("inflight = %d, want %d", c.inflight, tt.wantInflight)
			}
			sum := 0
			for _, b := range c.batches {
				sum += b.byteSize
			}
			if sum != c.inflight {
				t.Errorf("inflight = %d but batches hold %d bytes", c.inflight, sum)
			}
			if got := c.sortedPartitionsLocked(); !slices.Equal(got, tt.wantBatches) {
				t.Errorf("open batches = %v, want %v", got, tt.wantBatches)
			}
		})
	}
}

// TestInflightBudgetFailedFlush verifies a failed budget flush keeps the batch and its bytes
// (nothing admitted, nothing committed), and that a later successful flush releases them.
func TestInflightBudgetFailedFlush(t *testing.T) {
	sink := &fakeSink{err: errTransient}
	com := &fakeCommitter{}
	c := newTestConsumer(sink, &fakeDLQ{}, com.commit)
	c.maxInflight = 100
	c.retry.window = 20 * time.Millisecond
	ctx := context.Background()

	c.mu.Lock()
	defer c.mu.Unlock()
	if err := c.consumeRecordLocked(ctx, sizedRec(0, 0, 60)); err != nil {
		t.Fatalf("consume: %v", err)
	}
	if err := c.consumeRecordLocked(ctx, sizedRec(1, 0, 60)); err == nil {
		t.Fatal("expected the budget flush to fail")
	}
	if c.inflight != 60 || len(c.batches) != 1 {
		t.Errorf("inflight=%d batches=%v, want 60 held by partition 0 only (new record not admitted)",
			c.inflight, c.sortedPartitionsLocked())
	}
	if com.attempts != 0 {
		t.Errorf("commit attempts = %d, want 0", com.attempts)
	}

	sink.err = nil
	if err := c.flushAllLocked(ctx); err != nil {
		t.Fatalf("recovery flush: %v", err)
	}
	if c.inflight != 0 || len(c.batches) != 0 {
		t.Errorf("inflight=%d batches=%d, want bytes released", c.inflight, len(c.batches))
	}
	if !slices.Equal(com.committed, []int64{0}) {
		t.Errorf("committed = %v, want [0]", com.committed)
	}
}

// TestOnLostReleasesBytes verifies dropping lost partitions also releases their budget.
func TestOnLostReleasesBytes(t *testing.T) {
	c := newTestConsumer(&fakeSink{}, &fakeDLQ{}, (&fakeCommitter{}).commit)
	c.maxInflight = 1000
	c.mu.Lock()
	for _, r := range []sized{{0, 10}, {1, 20}} {
		if err := c.consumeRecordLocked(context.Background(), sizedRec(r.p, 0, r.n)); err != nil {
			t.Fatalf("consume: %v", err)
		}
	}
	c.mu.Unlock()
	c.onLost(context.Background(), nil, map[string][]int32{"otlp.spans.raw": {1}})
	if c.inflight != 10 || len(c.batches) != 1 {
		t.Errorf("inflight=%d batches=%d, want 10 / 1", c.inflight, len(c.batches))
	}
}
