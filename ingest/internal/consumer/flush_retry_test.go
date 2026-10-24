package consumer

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"slices"
	"strings"
	"testing"
	"time"
)

// loadTwo ingests two records (offsets 5 and 6, two spans) into partition 0.
func loadTwo(t *testing.T, c *Consumer) {
	t.Helper()
	ctx := context.Background()
	c.mu.Lock()
	defer c.mu.Unlock()
	for i, r := range []struct {
		off  int64
		b, s byte
	}{{5, 0xa1, 2}, {6, 0xa2, 1}} {
		if err := c.ingestRecordLocked(ctx, recWith(0, r.off, "k", oneSpanOTLP(t, r.b, r.s))); err != nil {
			t.Fatalf("ingest %d: %v", i, err)
		}
	}
}

// TestFlushRetry covers in-process insert/commit retries: identical rows and token on
// every insert attempt, exactly one successful commit, no re-insert while only the commit
// is retried, and an uncommitted, retained batch when the window is exhausted.
func TestFlushRetry(t *testing.T) {
	const token = "otlp.spans.raw:0:5:6"
	tests := []struct {
		name           string
		sink           *fakeSink
		commit         *fakeCommitter
		window         time.Duration
		wantErr        error // errors.Is target; nil = success
		wantAttempts   int   // insert attempts
		wantInserts    int   // successful inserts
		wantCommitTry  int   // commit attempts (-1 = at least one)
		wantCommitted  []int64
		wantBatchKept  bool
		wantInserted   bool // batch.inserted when kept
		wantReadyAfter bool
	}{
		{
			name: "first attempt succeeds", sink: &fakeSink{}, commit: &fakeCommitter{},
			wantAttempts: 1, wantInserts: 1, wantCommitTry: 1, wantCommitted: []int64{6},
			wantReadyAfter: true,
		},
		{
			name: "insert fails twice then succeeds", sink: &fakeSink{failN: 2}, commit: &fakeCommitter{},
			wantAttempts: 3, wantInserts: 1, wantCommitTry: 1, wantCommitted: []int64{6},
			wantReadyAfter: true,
		},
		{
			name: "commit fails twice: retried without re-insert", sink: &fakeSink{}, commit: &fakeCommitter{failN: 2},
			wantAttempts: 1, wantInserts: 1, wantCommitTry: 3, wantCommitted: []int64{6},
			wantReadyAfter: true,
		},
		{
			name: "insert retries exhausted", sink: &fakeSink{err: errTransient}, commit: &fakeCommitter{},
			window: 30 * time.Millisecond, wantErr: context.DeadlineExceeded,
			wantAttempts: -1, wantCommitTry: 0, wantBatchKept: true,
		},
		{
			name: "commit retries exhausted", sink: &fakeSink{}, commit: &fakeCommitter{always: true},
			window: 30 * time.Millisecond, wantErr: context.DeadlineExceeded,
			wantAttempts: 1, wantInserts: 1, wantCommitTry: -1, wantBatchKept: true, wantInserted: true,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			c := newTestConsumer(tt.sink, &fakeDLQ{}, tt.commit.commit)
			if tt.window > 0 {
				c.retry.window = tt.window
			}
			loadTwo(t, c)

			c.mu.Lock()
			err := c.flushLocked(context.Background(), 0)
			c.mu.Unlock()

			if tt.wantErr == nil && err != nil {
				t.Fatalf("flush: %v", err)
			}
			if tt.wantErr != nil && !errors.Is(err, tt.wantErr) {
				t.Fatalf("flush err = %v, want errors.Is %v", err, tt.wantErr)
			}
			if tt.wantErr != nil && !errors.Is(err, errTransient) && tt.sink.err != nil {
				t.Errorf("give-up error should wrap the last insert error, got %v", err)
			}
			if tt.wantAttempts >= 0 && len(tt.sink.attempts) != tt.wantAttempts {
				t.Errorf("insert attempts = %d, want %d", len(tt.sink.attempts), tt.wantAttempts)
			}
			if tt.wantAttempts < 0 && len(tt.sink.attempts) < 2 {
				t.Errorf("insert attempts = %d, want retries (>= 2)", len(tt.sink.attempts))
			}
			for i, a := range tt.sink.attempts {
				if a.token != token || !slices.Equal(a.spanIDs, tt.sink.attempts[0].spanIDs) || a.rows != 2 {
					t.Errorf("attempt %d = %+v, want token %q and the same 2 rows as attempt 1 %v",
						i+1, a, token, tt.sink.attempts[0].spanIDs)
				}
			}
			if len(tt.sink.inserts) != tt.wantInserts {
				t.Errorf("successful inserts = %d, want %d", len(tt.sink.inserts), tt.wantInserts)
			}
			if tt.wantCommitTry >= 0 && tt.commit.attempts != tt.wantCommitTry {
				t.Errorf("commit attempts = %d, want %d", tt.commit.attempts, tt.wantCommitTry)
			}
			if tt.wantCommitTry < 0 && tt.commit.attempts < 2 {
				t.Errorf("commit attempts = %d, want retries (>= 2)", tt.commit.attempts)
			}
			if !slices.Equal(tt.commit.committed, tt.wantCommitted) {
				t.Errorf("committed = %v, want %v", tt.commit.committed, tt.wantCommitted)
			}
			b, kept := c.batches[0]
			if kept != tt.wantBatchKept {
				t.Fatalf("batch kept = %v, want %v", kept, tt.wantBatchKept)
			}
			if kept && (!b.sealed || b.inserted != tt.wantInserted || b.flushToken != token) {
				t.Errorf("kept batch sealed=%v inserted=%v token=%q, want sealed, inserted=%v, %q",
					b.sealed, b.inserted, b.flushToken, tt.wantInserted, token)
			}
			if kept && c.inflight != b.byteSize {
				t.Errorf("inflight = %d, want %d (bytes still held by the kept batch)", c.inflight, b.byteSize)
			}
			if !kept && c.inflight != 0 {
				t.Errorf("inflight = %d, want 0 after a successful flush", c.inflight)
			}
			if ready := c.Ping(context.Background()) == nil; ready != tt.wantReadyAfter {
				t.Errorf("ready after flush = %v, want %v", ready, tt.wantReadyAfter)
			}
		})
	}
}

// TestRetryAfterCommitExhaustionDoesNotReinsert verifies that a later flush of a batch whose
// insert already succeeded (e.g. the shutdown flush) only retries the commit.
func TestRetryAfterCommitExhaustionDoesNotReinsert(t *testing.T) {
	sink := &fakeSink{}
	com := &fakeCommitter{always: true}
	c := newTestConsumer(sink, &fakeDLQ{}, com.commit)
	c.retry.window = 20 * time.Millisecond
	loadTwo(t, c)

	c.mu.Lock()
	defer c.mu.Unlock()
	if err := c.flushLocked(context.Background(), 0); err == nil {
		t.Fatal("expected commit exhaustion error")
	}
	com.always = false
	if err := c.flushAllLocked(context.Background()); err != nil {
		t.Fatalf("second flush: %v", err)
	}
	if len(sink.attempts) != 1 {
		t.Errorf("insert attempts = %d, want 1 (insert already succeeded)", len(sink.attempts))
	}
	if !slices.Equal(com.committed, []int64{6}) {
		t.Errorf("committed = %v, want [6]", com.committed)
	}
	if len(c.batches) != 0 || c.inflight != 0 {
		t.Errorf("batches=%d inflight=%d, want both 0", len(c.batches), c.inflight)
	}
}

// TestReadinessDuringRetry verifies readiness fails while a retry is in progress (Ping does
// not wait on the flush lock) and recovers after the flush succeeds.
func TestReadinessDuringRetry(t *testing.T) {
	sink := &fakeSink{failN: 1}
	com := &fakeCommitter{}
	c := newTestConsumer(sink, &fakeDLQ{}, com.commit)
	var readyOnRetry error
	sink.onAttempt = func(n int) {
		if n == 2 {
			readyOnRetry = c.Ping(context.Background())
		}
	}
	loadTwo(t, c)

	c.mu.Lock()
	err := c.flushLocked(context.Background(), 0)
	c.mu.Unlock()
	if err != nil {
		t.Fatalf("flush: %v", err)
	}
	if readyOnRetry == nil || !errors.Is(readyOnRetry, errTransient) {
		t.Errorf("Ping during retry = %v, want an unresolved-failure error wrapping the insert error", readyOnRetry)
	}
	if err := c.Ping(context.Background()); err != nil {
		t.Errorf("Ping after success = %v, want nil", err)
	}
}

// TestFlushCancellation verifies a canceled context stops a blocked insert promptly with no
// commit, keeps the sealed batch, and that the shutdown flush then sends the identical batch.
func TestFlushCancellation(t *testing.T) {
	sink := &fakeSink{block: true}
	com := &fakeCommitter{}
	c := newTestConsumer(sink, &fakeDLQ{}, com.commit)
	c.retry.window = 10 * time.Second
	loadTwo(t, c)

	ctx, cancel := context.WithCancel(context.Background())
	time.AfterFunc(20*time.Millisecond, cancel)
	start := time.Now()
	c.mu.Lock()
	err := c.flushLocked(ctx, 0)
	c.mu.Unlock()
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("flush err = %v, want context.Canceled", err)
	}
	if el := time.Since(start); el > 2*time.Second {
		t.Errorf("cancellation took %s, want prompt return (window is 10s)", el)
	}
	if com.attempts != 0 {
		t.Errorf("commit attempts = %d, want 0 after a canceled insert", com.attempts)
	}
	if c.Ping(context.Background()) == nil {
		t.Error("expected not-ready after a canceled flush")
	}

	// Shutdown flush with a fresh context: same batch, same token, one commit.
	sink.block = false
	c.mu.Lock()
	err = c.flushAllLocked(context.Background())
	c.mu.Unlock()
	if err != nil {
		t.Fatalf("shutdown flush: %v", err)
	}
	if len(sink.inserts) != 1 || sink.inserts[0].token != sink.attempts[0].token ||
		!slices.Equal(sink.inserts[0].spanIDs, sink.attempts[0].spanIDs) {
		t.Errorf("shutdown insert %+v, want identical to interrupted attempt %+v", sink.inserts, sink.attempts[0])
	}
	if !slices.Equal(com.committed, []int64{6}) {
		t.Errorf("committed = %v, want [6]", com.committed)
	}
}

// TestSealedBatchRejectsNewRecords verifies a batch with an unresolved flush is not extended
// (which would change its rows and dedup token).
func TestSealedBatchRejectsNewRecords(t *testing.T) {
	c := newTestConsumer(&fakeSink{err: errTransient}, &fakeDLQ{}, (&fakeCommitter{}).commit)
	c.retry.window = 10 * time.Millisecond
	loadTwo(t, c)

	c.mu.Lock()
	defer c.mu.Unlock()
	_ = c.flushLocked(context.Background(), 0)
	if err := c.ingestRecordLocked(context.Background(), recWith(0, 7, "k", oneSpanOTLP(t, 0xa3, 1))); err == nil {
		t.Fatal("expected ingest into a sealed batch to fail")
	}
	if b := c.batches[0]; b.lastOffset != 6 || b.flushToken != "otlp.spans.raw:0:5:6" {
		t.Errorf("sealed batch changed: last=%d token=%q", b.lastOffset, b.flushToken)
	}
}

// TestRevokeFailure verifies a revoke whose flush fails commits nothing, drops the batches
// (ownership is moving; the new owner replays), releases their bytes, and bounds all revoked
// partitions by one shared retry window. The failed handoff is logged and the revoked
// partitions' failures are cleared, while a still-owned partition's failure is kept.
func TestRevokeFailure(t *testing.T) {
	tests := []struct {
		name   string
		sink   *fakeSink
		commit *fakeCommitter
	}{
		{"insert keeps failing", &fakeSink{err: errTransient}, &fakeCommitter{}},
		{"commit keeps failing", &fakeSink{}, &fakeCommitter{always: true}},
		{"insert hangs", &fakeSink{block: true}, &fakeCommitter{}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			const window = 200 * time.Millisecond
			c := newTestConsumer(tt.sink, &fakeDLQ{}, tt.commit.commit)
			c.retry.window = window
			var logs bytes.Buffer
			c.log = slog.New(slog.NewTextHandler(&logs, nil))
			c.mu.Lock()
			for p := int32(0); p < 3; p++ {
				if err := c.ingestRecordLocked(context.Background(), recWith(p, 1, "k", oneSpanOTLP(t, byte(p+1), 1))); err != nil {
					t.Fatalf("ingest: %v", err)
				}
			}
			c.mu.Unlock()
			// Partition 1 stays owned with an unresolved failure of its own.
			errOwned := errors.New("owned partition failure")
			c.setWriteFailure(1, errOwned)

			start := time.Now()
			c.onRevoked(context.Background(), nil, map[string][]int32{"otlp.spans.raw": {2, 0}, "other": {1}})
			if el := time.Since(start); el > 2*window {
				t.Errorf("revoke took %s, want about one shared window (%s)", el, window)
			}
			if len(tt.commit.committed) != 0 {
				t.Errorf("committed = %v, want none", tt.commit.committed)
			}
			if _, ok := c.batches[1]; !ok || len(c.batches) != 1 {
				t.Fatalf("batches = %v, want only the unrevoked partition 1", c.sortedPartitionsLocked())
			}
			if c.inflight != c.batches[1].byteSize {
				t.Errorf("inflight = %d, want %d (only partition 1)", c.inflight, c.batches[1].byteSize)
			}
			err := c.Ping(context.Background())
			if !errors.Is(err, errOwned) {
				t.Errorf("Ping = %v, want the still-owned partition 1 failure", err)
			}
			for _, p := range []int32{0, 2} {
				if err != nil && strings.Contains(err.Error(), fmt.Sprintf("partition %d:", p)) {
					t.Errorf("readiness error %q still names revoked partition %d", err, p)
				}
				for _, msg := range []string{
					"flush on revoke failed; range left uncommitted for the new owner to replay",
					"partition released with an unresolved write failure; clearing it from readiness",
				} {
					if !strings.Contains(logs.String(), fmt.Sprintf("msg=%q partition=%d", msg, p)) {
						t.Errorf("no %q log for partition %d; logs:\n%s", msg, p, logs.String())
					}
				}
			}
		})
	}
}

// TestRevokeCanceledContext verifies an already-canceled revoke context gives no false
// success: no commit and no successful insert is reported.
func TestRevokeCanceledContext(t *testing.T) {
	sink := &fakeSink{block: true}
	com := &fakeCommitter{}
	c := newTestConsumer(sink, &fakeDLQ{}, com.commit)
	loadTwo(t, c)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	c.onRevoked(ctx, nil, map[string][]int32{"otlp.spans.raw": {0}})
	if com.attempts != 0 || len(sink.inserts) != 0 {
		t.Errorf("commit attempts=%d inserts=%d, want 0/0", com.attempts, len(sink.inserts))
	}
	if _, ok := c.batches[0]; ok {
		t.Error("revoked batch should be dropped")
	}
}
