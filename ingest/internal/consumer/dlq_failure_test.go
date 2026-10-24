package consumer

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/twmb/franz-go/pkg/kgo"
)

// fetchOf builds a single fetch of the given per-partition records (in partition order).
func fetchOf(parts map[int32][]*kgo.Record) kgo.Fetches {
	ft := kgo.FetchTopic{Topic: "otlp.spans.raw"}
	for _, p := range slices.Sorted(maps.Keys(parts)) {
		ft.Partitions = append(ft.Partitions, kgo.FetchPartition{Partition: p, Records: parts[p]})
	}
	return kgo.Fetches{{Topics: []kgo.FetchTopic{ft}}}
}

// partitionCommitter records committed offsets per partition.
type partitionCommitter struct{ committed map[int32][]int64 }

func (f *partitionCommitter) commit(_ context.Context, r *kgo.Record) error {
	if f.committed == nil {
		f.committed = map[int32][]int64{}
	}
	f.committed[r.Partition] = append(f.committed[r.Partition], r.Offset)
	return nil
}

var errDLQDown = errors.New("dlq broker unavailable")

// TestDLQFailureNeverCommitted drives a poll's records through processFetches where a
// record fails normalization, then runs the shutdown flush or the revoke callback (the two
// paths that flush after Run stops on an error or cancellation). A record whose DLQ publish
// failed or was canceled must never be committed (nor any record after it); the records
// before it on its partition, and other partitions, still commit. A successful DLQ publish
// still advances the commit over the bad record. Readiness tracks owned work: after the
// shutdown flush the stalled partition is still owned (not ready); the revoke releases every
// partition (ready).
func TestDLQFailureNeverCommitted(t *testing.T) {
	type outcome struct {
		committed map[int32][]int64
		tokens    []string
		ready     bool
	}
	tests := []struct {
		name     string
		dlq      *fakeDLQ
		cancel   bool // processFetches runs with an already-canceled context
		wantErr  error
		wantKept map[int32][2]int64 // batches left after processFetches: partition → [first,last]
		want     outcome
	}{
		{
			name: "dlq publish fails", dlq: &fakeDLQ{err: errDLQDown}, wantErr: errDLQDown,
			wantKept: map[int32][2]int64{0: {5, 5}},
			want: outcome{
				committed: map[int32][]int64{0: {5}},
				tokens:    []string{"otlp.spans.raw:0:5:5"},
			},
		},
		{
			name: "dlq publish canceled by shutdown", dlq: &fakeDLQ{block: true}, cancel: true,
			wantErr:  context.Canceled,
			wantKept: map[int32][2]int64{0: {5, 5}},
			want: outcome{
				committed: map[int32][]int64{0: {5}},
				tokens:    []string{"otlp.spans.raw:0:5:5"},
			},
		},
		{
			name: "dlq publish succeeds", dlq: &fakeDLQ{},
			wantKept: map[int32][2]int64{0: {5, 7}, 1: {3, 3}},
			want: outcome{
				committed: map[int32][]int64{0: {7}, 1: {3}},
				tokens:    []string{"otlp.spans.raw:0:5:7", "otlp.spans.raw:1:3:3"},
				ready:     true,
			},
		},
	}
	boundaries := []struct {
		name     string
		run      func(c *Consumer)
		releases bool // ownership of every partition is released
	}{
		{"shutdown flush", func(c *Consumer) {
			c.mu.Lock()
			defer c.mu.Unlock()
			_ = c.flushAllLocked(context.Background())
		}, false},
		{"revoke", func(c *Consumer) {
			c.onRevoked(context.Background(), nil, map[string][]int32{"otlp.spans.raw": {0, 1}})
		}, true},
	}
	for _, tt := range tests {
		for _, bd := range boundaries {
			t.Run(tt.name+"/"+bd.name, func(t *testing.T) {
				sink := &fakeSink{}
				com := &partitionCommitter{}
				dlq := *tt.dlq
				c := newTestConsumer(sink, &dlq, com.commit)
				c.maxInflight = 1 << 20

				good5 := recWith(0, 5, "a5", oneSpanOTLP(t, 0xa5, 1))
				bad6 := recWith(0, 6, "bad", []byte{0xff, 0xff, 0xff, 0xff})
				good7 := recWith(0, 7, "a7", oneSpanOTLP(t, 0xa7, 1))
				good3 := recWith(1, 3, "b3", oneSpanOTLP(t, 0xb3, 1))
				ctx, cancel := context.WithCancel(context.Background())
				if tt.cancel {
					cancel()
				}
				defer cancel()

				err := c.processFetches(ctx, fetchOf(map[int32][]*kgo.Record{
					0: {good5, bad6, good7},
					1: {good3},
				}))
				if tt.wantErr == nil && err != nil {
					t.Fatalf("processFetches: %v", err)
				}
				if tt.wantErr != nil && !errors.Is(err, tt.wantErr) {
					t.Fatalf("processFetches err = %v, want errors.Is %v", err, tt.wantErr)
				}

				c.mu.Lock()
				got := map[int32][2]int64{}
				sum := 0
				for p, b := range c.batches {
					got[p] = [2]int64{b.firstOffset, b.lastOffset}
					sum += b.byteSize
				}
				if !maps.Equal(got, tt.wantKept) {
					t.Errorf("batches = %v, want %v", got, tt.wantKept)
				}
				if c.inflight != sum {
					t.Errorf("inflight = %d, want %d (failed record's bytes not counted)", c.inflight, sum)
				}
				c.mu.Unlock()

				bd.run(c)

				if !maps.EqualFunc(com.committed, tt.want.committed, slices.Equal) {
					t.Errorf("committed = %v, want %v", com.committed, tt.want.committed)
				}
				var tokens []string
				for _, in := range sink.inserts {
					tokens = append(tokens, in.token)
				}
				if !slices.Equal(tokens, tt.want.tokens) {
					t.Errorf("inserted tokens = %v, want %v", tokens, tt.want.tokens)
				}
				wantReady := tt.want.ready || bd.releases
				if ready := c.Ping(context.Background()) == nil; ready != wantReady {
					t.Errorf("ready = %v, want %v", ready, wantReady)
				}
			})
		}
	}
}

// TestDLQFailureStallsPartition verifies that after a failed DLQ publish no later record of
// that partition can join a batch (committing it would commit past the failed record), even
// once the earlier records have been flushed, while other partitions are unaffected. The
// failure keeps readiness down while the partition is owned; a revoke unstalls it (the next
// owner resumes from the last commit) and clears its failure. When the partition comes back
// and the DLQ is still down, the failure is recorded again.
func TestDLQFailureStallsPartition(t *testing.T) {
	sink := &fakeSink{}
	com := &partitionCommitter{}
	dlq := &fakeDLQ{err: errDLQDown}
	c := newTestConsumer(sink, dlq, com.commit)
	ctx := context.Background()

	c.mu.Lock()
	if err := c.ingestRecordLocked(ctx, recWith(0, 5, "a5", oneSpanOTLP(t, 0xa5, 1))); err != nil {
		t.Fatalf("ingest 5: %v", err)
	}
	if err := c.ingestRecordLocked(ctx, recWith(0, 6, "bad", []byte{0xff})); !errors.Is(err, errDLQDown) {
		t.Fatalf("ingest 6 err = %v, want dlq failure", err)
	}
	if err := c.flushLocked(ctx, 0); err != nil {
		t.Fatalf("flush earlier records: %v", err)
	}
	if err := c.ingestRecordLocked(ctx, recWith(0, 7, "a7", oneSpanOTLP(t, 0xa7, 1))); err == nil ||
		!strings.Contains(err.Error(), "stalled at offset 6") {
		t.Fatalf("ingest 7 into stalled partition err = %v, want stalled error", err)
	}
	if _, ok := c.batches[0]; ok {
		t.Error("stalled partition should hold no batch")
	}
	if err := c.ingestRecordLocked(ctx, recWith(2, 1, "c1", oneSpanOTLP(t, 0xc1, 1))); err != nil {
		t.Fatalf("other partition ingest: %v", err)
	}
	c.mu.Unlock()

	if err := c.Ping(ctx); !errors.Is(err, errDLQDown) {
		t.Errorf("Ping = %v, want unresolved dlq failure after flushing the earlier records", err)
	}
	if !maps.EqualFunc(com.committed, map[int32][]int64{0: {5}}, slices.Equal) {
		t.Errorf("committed = %v, want only partition 0 offset 5", com.committed)
	}

	c.onRevoked(ctx, nil, map[string][]int32{"otlp.spans.raw": {0}})
	if err := c.Ping(ctx); err != nil {
		t.Errorf("Ping after revoke = %v, want nil (partition 0 is no longer owned)", err)
	}

	// Reassigned while the DLQ is still down: the replayed bad record fails again.
	c.mu.Lock()
	if err := c.ingestRecordLocked(ctx, recWith(0, 6, "bad", []byte{0xff})); !errors.Is(err, errDLQDown) {
		t.Fatalf("replay ingest 6 err = %v, want dlq failure", err)
	}
	c.mu.Unlock()
	if err := c.Ping(ctx); !errors.Is(err, errDLQDown) {
		t.Errorf("Ping after the failure recurred = %v, want the dlq failure", err)
	}
	c.onRevoked(ctx, nil, map[string][]int32{"otlp.spans.raw": {0}})

	// Reassigned again: the partition replays from offset 6 and the DLQ has recovered.
	dlq.err = nil
	c.mu.Lock()
	for _, r := range []*kgo.Record{recWith(0, 6, "bad", []byte{0xff}), recWith(0, 7, "a7", oneSpanOTLP(t, 0xa7, 1))} {
		if err := c.ingestRecordLocked(ctx, r); err != nil {
			t.Fatalf("replay ingest %d: %v", r.Offset, err)
		}
	}
	if err := c.flushAllLocked(ctx); err != nil {
		t.Fatalf("flush: %v", err)
	}
	c.mu.Unlock()
	if !maps.EqualFunc(com.committed, map[int32][]int64{0: {5, 7}, 2: {1}}, slices.Equal) {
		t.Errorf("committed = %v, want partition 0 [5 7] and partition 2 [1]", com.committed)
	}
	if err := c.Ping(ctx); err != nil {
		t.Errorf("Ping after the partition recovered = %v, want nil", err)
	}
}

// TestReadinessPerPartition verifies readiness tracks owned work across the multi-partition
// flush paths (shutdown flushAll, revoke, loss): a partially failed flush keeps the failed
// partition's failure while it is still owned, one partition's successful flush or release
// never clears another's failure, releasing (revoke or loss) a failed partition clears only
// that partition's failure, and readiness recovers once every owned failed partition has
// flushed successfully.
func TestReadinessPerPartition(t *testing.T) {
	failP0 := func(tok string) bool { return strings.HasPrefix(tok, "otlp.spans.raw:0:") }
	failP1 := func(tok string) bool { return strings.HasPrefix(tok, "otlp.spans.raw:1:") }
	failAll := func(string) bool { return true }
	revoke := func(ctx context.Context, ps ...int32) func(c *Consumer) {
		return func(c *Consumer) { c.onRevoked(ctx, nil, map[string][]int32{"otlp.spans.raw": ps}) }
	}
	then := func(fs ...func(c *Consumer)) func(c *Consumer) {
		return func(c *Consumer) {
			for _, f := range fs {
				f(c)
			}
		}
	}
	canceled, cancel := context.WithCancel(context.Background())
	cancel()
	tests := []struct {
		name       string
		failToken  func(string) bool
		block      bool
		run        func(c *Consumer)
		wantFailed []int32 // partitions named in the readiness error; empty = ready
		recover    bool    // re-ingest the failed partitions and flush them successfully
	}{
		{
			name: "flushAll: failed partition before a successful one", failToken: failP0,
			run: flushAllNow, wantFailed: []int32{0}, recover: true,
		},
		{
			name: "flushAll: successful partition before a failed one", failToken: failP1,
			run: flushAllNow, wantFailed: []int32{1}, recover: true,
		},
		{
			name: "revoke of another partition keeps the owned failed one", failToken: failP0,
			run: then(flushAllNow, revoke(context.Background(), 1)), wantFailed: []int32{0}, recover: true,
		},
		{
			name: "revoke of a failed partition clears only its failure", failToken: failAll,
			run: then(flushAllNow, revoke(context.Background(), 0)), wantFailed: []int32{1}, recover: true,
		},
		{
			name: "loss of a failed partition clears only its failure", failToken: failAll,
			run: then(flushAllNow, func(c *Consumer) {
				c.onLost(context.Background(), nil, map[string][]int32{"otlp.spans.raw": {1}})
			}),
			wantFailed: []int32{0}, recover: true,
		},
		{
			name: "revoke: failed handoff of one partition, successful flush of the other", failToken: failP0,
			run: revoke(context.Background(), 0, 1),
		},
		{
			name: "revoke with a canceled context releases every revoked partition", block: true,
			run: revoke(canceled, 0, 1),
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			sink := &fakeSink{failToken: tt.failToken, block: tt.block}
			com := &partitionCommitter{}
			c := newTestConsumer(sink, &fakeDLQ{}, com.commit)
			c.retry.window = 30 * time.Millisecond
			ingestEach(t, c, 1)

			tt.run(c)

			err := c.Ping(context.Background())
			if len(tt.wantFailed) == 0 {
				if err != nil {
					t.Errorf("Ping = %v, want ready (no owned partition has a failure)", err)
				}
				return
			}
			if err == nil {
				t.Fatal("ready despite an unresolved failure on an owned partition")
			}
			for p := int32(0); p < 2; p++ {
				named := strings.Contains(err.Error(), fmt.Sprintf("partition %d:", p))
				if want := slices.Contains(tt.wantFailed, p); named != want {
					t.Errorf("readiness error %q names partition %d = %v, want %v", err, p, named, want)
				}
			}
			if !tt.recover {
				return
			}

			sink.failToken = nil
			c.mu.Lock()
			for _, p := range tt.wantFailed {
				if _, kept := c.batches[p]; !kept {
					if err := c.ingestRecordLocked(context.Background(), recWith(p, 1, "k", oneSpanOTLP(t, byte(p+1), 1))); err != nil {
						t.Fatalf("re-ingest: %v", err)
					}
				}
			}
			if err := c.flushAllLocked(context.Background()); err != nil {
				t.Fatalf("recovery flush: %v", err)
			}
			c.mu.Unlock()
			if err := c.Ping(context.Background()); err != nil {
				t.Errorf("Ping after every failed partition recovered = %v, want nil", err)
			}
		})
	}
}

func flushAllNow(c *Consumer) {
	c.mu.Lock()
	defer c.mu.Unlock()
	_ = c.flushAllLocked(context.Background())
}

// ingestEach adds one good record at the given offset to partitions 0 and 1.
func ingestEach(t *testing.T, c *Consumer, offset int64) {
	t.Helper()
	c.mu.Lock()
	defer c.mu.Unlock()
	for p := int32(0); p < 2; p++ {
		if err := c.ingestRecordLocked(context.Background(), recWith(p, offset, "k", oneSpanOTLP(t, byte(p+1), 1))); err != nil {
			t.Fatalf("ingest: %v", err)
		}
	}
}
