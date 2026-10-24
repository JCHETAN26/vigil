// Package consumer reads raw per-trace OTLP records from Redpanda, normalizes them, and
// inserts them into ClickHouse in deterministic contiguous per-partition offset-range
// batches. Offsets are committed only after a successful insert; batches are flushed and
// committed on partition revocation and on shutdown so ranges stay contiguous and
// idempotent across ownership changes and restarts.
//
// Failure handling: a failed insert or commit is retried in-process with the identical
// rows and dedup token (capped exponential backoff, bounded by a retry window below the
// rebalance timeout). Errors are not classified: a permanent error (say, a schema mismatch)
// is retried for the whole window like a transient one, which only delays the give-up;
// classification is intentionally deferred. If the window is exhausted the batch is kept
// uncommitted and Run returns the error; the range then replays from the last committed
// offset after restart. On revoke the batch is instead dropped uncommitted once the window
// is spent (no commit is attempted after that, and nothing waits past the window), and the
// new owner replays it. Exactly-once across a process crash or a failed handoff rests
// entirely on ClickHouse insert deduplication (insert_deduplication_token, kept for the
// table's last non_replicated_deduplication_window blocks) and only holds when the replay
// reforms the same offset range. Fetch boundaries can differ on replay; a different range
// gets a different token, so its rows are inserted again (spans converges on merge, but the
// materialized views double-count until rebuilt; data-model §3.5/§3.7). This package adds
// no guarantee beyond that.
//
// Readiness (Ping) tracks owned work: a partition's failure counts until that partition
// flushes successfully or is revoked/lost, never cleared by another partition.
package consumer

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"slices"
	"sync"
	"time"

	"github.com/twmb/franz-go/pkg/kgo"

	"github.com/JCHETAN26/vigil/ingest/internal/chsink"
	"github.com/JCHETAN26/vigil/ingest/internal/config"
	"github.com/JCHETAN26/vigil/ingest/internal/normalize"
	"github.com/JCHETAN26/vigil/ingest/internal/pricing"
	"github.com/JCHETAN26/vigil/ingest/internal/producer"
)

// spanSink is the ClickHouse write surface the consumer needs (satisfied by *chsink.Sink).
type spanSink interface {
	Insert(ctx context.Context, rows []chsink.Row, dedupToken string) error
	Ping(ctx context.Context) error
}

// dlqPublisher publishes failed records (satisfied by *producer.Producer).
type dlqPublisher interface {
	ProduceSync(ctx context.Context, recs ...producer.Record) error
}

// Consumer wires a franz-go consumer group to the ClickHouse sink and DLQ producer.
type Consumer struct {
	cl     *kgo.Client
	sink   spanSink
	prices *pricing.Table
	dlq    dlqPublisher
	log    *slog.Logger

	// commit commits offsets after a successful insert; a field so tests can inject a
	// fake without a live client. In production it wraps kgo.Client.CommitRecords.
	commit func(ctx context.Context, rec *kgo.Record) error

	topic       string
	dlqTopic    string
	maxRows     int
	maxBytes    int
	maxInflight int // global budget on batch bytes across partitions; 0 = unlimited
	interval    time.Duration
	retry       retryPolicy

	mu       sync.Mutex
	batches  map[int32]*batch
	inflight int // sum of byteSize over batches; guarded by mu
	// stalled holds, per partition, the offset of a record whose normalization failed and
	// whose DLQ publish also failed. That record is not in any batch, so no later record of
	// the partition may be added either (committing past it would lose it). Cleared when the
	// partition is revoked or lost: the next owner resumes from the last committed offset,
	// which is at or before the stalled record. Guarded by mu.
	stalled map[int32]int64

	// failures holds, per partition, the latest unresolved insert/commit/DLQ failure, so
	// readiness reflects the work this process still owns. An entry is cleared by a later
	// successful flush of that same partition (not while the partition is stalled), or when
	// that partition is revoked or lost (after the failed handoff is logged: the dropped range
	// becomes the next owner's work, replayed from the last committed offset). It is never
	// cleared by another partition's success or release. It has its own lock so readiness
	// never waits behind a retrying flush (which holds mu).
	failMu   sync.Mutex
	failures map[int32]error
}

// New builds a Consumer. The sink and dlq producer are owned by the caller (closed after
// Run returns).
func New(kcfg config.Kafka, wcfg config.Writer, sink *chsink.Sink, dlq *producer.Producer, prices *pricing.Table, log *slog.Logger) (*Consumer, error) {
	retry, err := newRetryPolicy(wcfg.RetryInitialBackoff, wcfg.RetryMaxBackoff, wcfg.RetryWindow)
	if err != nil {
		return nil, err
	}
	c := &Consumer{
		sink:        sink,
		prices:      prices,
		dlq:         dlq,
		log:         log,
		topic:       wcfg.RawTopic,
		dlqTopic:    wcfg.DLQTopic,
		maxRows:     wcfg.BatchMaxRows,
		maxBytes:    wcfg.BatchMaxBytes,
		maxInflight: wcfg.MaxInflightBytes,
		interval:    wcfg.FlushInterval,
		retry:       retry,
		batches:     map[int32]*batch{},
		stalled:     map[int32]int64{},
		failures:    map[int32]error{},
	}

	cl, err := kgo.NewClient(
		kgo.SeedBrokers(kcfg.Brokers...),
		kgo.ConsumerGroup(wcfg.Group),
		kgo.ConsumeTopics(wcfg.RawTopic),
		kgo.ConsumeResetOffset(kgo.NewOffset().AtStart()),
		kgo.DisableAutoCommit(),
		// Block rebalances from interleaving with in-flight poll processing; the revoke
		// callback (below) then flushes+commits owned partitions before they move.
		kgo.BlockRebalanceOnPoll(),
		kgo.RebalanceTimeout(rebalanceTimeout),
		kgo.OnPartitionsRevoked(c.onRevoked),
		kgo.OnPartitionsLost(c.onLost),
	)
	if err != nil {
		return nil, err
	}
	c.cl = cl
	c.commit = func(ctx context.Context, rec *kgo.Record) error {
		return cl.CommitRecords(ctx, rec)
	}
	return c, nil
}

// Ping reports readiness: not ready while any owned partition has an unresolved
// insert/commit/DLQ failure (retrying, or given up), otherwise ClickHouse connectivity.
func (c *Consumer) Ping(ctx context.Context) error {
	if err := c.writeFailure(); err != nil {
		return fmt.Errorf("unresolved write failure: %w", err)
	}
	return c.sink.Ping(ctx)
}

// writeFailure joins every partition's unresolved failure, in partition order.
func (c *Consumer) writeFailure() error {
	c.failMu.Lock()
	defer c.failMu.Unlock()
	ps := make([]int32, 0, len(c.failures))
	for p := range c.failures {
		ps = append(ps, p)
	}
	slices.Sort(ps)
	errs := make([]error, 0, len(ps))
	for _, p := range ps {
		errs = append(errs, c.failures[p])
	}
	return errors.Join(errs...)
}

func (c *Consumer) setWriteFailure(partition int32, err error) {
	c.failMu.Lock()
	if c.failures == nil {
		c.failures = map[int32]error{}
	}
	c.failures[partition] = err
	c.failMu.Unlock()
}

func (c *Consumer) clearWriteFailure(partition int32) {
	c.failMu.Lock()
	delete(c.failures, partition)
	c.failMu.Unlock()
}

// Run consumes until ctx is canceled, then flushes and commits all in-flight batches
// before returning. It does not close the client; call Close for that.
func (c *Consumer) Run(ctx context.Context) error {
	for ctx.Err() == nil {
		pollCtx, cancel := context.WithTimeout(ctx, c.interval)
		fetches := c.cl.PollFetches(pollCtx)
		cancel()

		if fetches.IsClientClosed() {
			break
		}
		fetches.EachError(func(t string, p int32, err error) {
			if !errors.Is(err, context.DeadlineExceeded) && !errors.Is(err, context.Canceled) {
				c.log.Error("fetch error", "topic", t, "partition", p, "err", err)
			}
		})

		// With BlockRebalanceOnPoll a pending rebalance waits for this iteration, so all of
		// its flush work shares one retry window (kept below half the rebalance timeout,
		// leaving the other half for the revoke callback's own window).
		iterCtx, cancelIter := context.WithTimeout(ctx, c.retry.window)
		err := c.processFetches(iterCtx, fetches)
		if err == nil {
			c.mu.Lock()
			err = c.flushStaleLocked(iterCtx)
			c.mu.Unlock()
		}
		cancelIter()
		// Required with BlockRebalanceOnPoll: let a pending rebalance (and its revoke
		// callback) proceed now that this poll's records are processed. Also lets Close run.
		c.cl.AllowRebalance()

		if err != nil {
			if ctx.Err() != nil {
				// Shutdown interrupted a flush or DLQ publish. An interrupted flush's batch is
				// kept sealed (same rows, same token) and retried by the shutdown flush below;
				// nothing was committed. A record whose DLQ publish was interrupted is in no
				// batch and its partition is stalled, so the shutdown flush commits only the
				// records before it, and it replays after restart.
				c.log.Warn("flush interrupted by shutdown", "err", err)
				break
			}
			return err
		}
	}

	// Graceful shutdown: flush and commit everything with a detached context.
	flushCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.flushAllLocked(flushCtx)
}

// Close closes the underlying client (after Run has returned).
func (c *Consumer) Close() { c.cl.Close() }

// processFetches ingests each partition's records in offset order, flushing on size.
func (c *Consumer) processFetches(ctx context.Context, fetches kgo.Fetches) error {
	var procErr error
	fetches.EachPartition(func(ftp kgo.FetchTopicPartition) {
		if procErr != nil {
			return
		}
		c.mu.Lock()
		defer c.mu.Unlock()
		for _, rec := range ftp.Records {
			if err := c.consumeRecordLocked(ctx, rec); err != nil {
				procErr = err
				return
			}
		}
	})
	return procErr
}

// consumeRecordLocked admits one record under the global inflight budget, adds it to its
// partition batch, and flushes that batch if it hit a per-partition limit. Caller holds c.mu.
func (c *Consumer) consumeRecordLocked(ctx context.Context, rec *kgo.Record) error {
	if err := c.admitLocked(ctx, len(rec.Value)); err != nil {
		return err
	}
	if err := c.ingestRecordLocked(ctx, rec); err != nil {
		return err
	}
	b := c.batches[rec.Partition]
	if c.overBudget() {
		// Only reachable when this record alone exceeds the budget: admitLocked already
		// flushed everything else. It is already in memory (fetched), so it is admitted and
		// flushed on its own immediately rather than rejected.
		c.log.Warn("record exceeds the inflight byte budget; flushing it alone",
			"partition", rec.Partition, "offset", rec.Offset, "bytes", len(rec.Value),
			"max_inflight_bytes", c.maxInflight)
		return c.flushLocked(ctx, rec.Partition)
	}
	if b.shouldFlushSize(c.maxRows, c.maxBytes) {
		return c.flushLocked(ctx, rec.Partition)
	}
	return nil
}

// admitLocked makes room for n more bytes under the global inflight budget by flushing the
// largest batch (ties: lowest partition) until the record fits or nothing is buffered.
// Caller holds c.mu.
func (c *Consumer) admitLocked(ctx context.Context, n int) error {
	if c.maxInflight <= 0 {
		return nil
	}
	for c.inflight > 0 && c.inflight+n > c.maxInflight {
		p := c.largestBatchLocked()
		if err := c.flushLocked(ctx, p); err != nil {
			return fmt.Errorf("flush for inflight byte budget: %w", err)
		}
	}
	return nil
}

func (c *Consumer) overBudget() bool {
	return c.maxInflight > 0 && c.inflight > c.maxInflight
}

// largestBatchLocked returns the partition with the most buffered bytes, breaking ties by
// the lowest partition id so the flush order is deterministic. Caller holds c.mu.
func (c *Consumer) largestBatchLocked() int32 {
	best, bestSize := int32(-1), -1
	for _, p := range c.sortedPartitionsLocked() {
		if s := c.batches[p].byteSize; s > bestSize {
			best, bestSize = p, s
		}
	}
	return best
}

func (c *Consumer) sortedPartitionsLocked() []int32 {
	ps := make([]int32, 0, len(c.batches))
	for p := range c.batches {
		ps = append(ps, p)
	}
	slices.Sort(ps)
	return ps
}

// ingestRecordLocked normalizes one record into its partition batch, or routes it to the
// DLQ on a normalization failure (still extending the offset range so the commit covers
// it). A record joins the offset range (and the inflight budget) only once it is fully
// handled — normalized, or published to the DLQ. If the DLQ publish fails, the record is
// left out of every batch, earlier records stay in place, and the partition is stalled so
// no later record can extend the range past it. Caller holds c.mu.
func (c *Consumer) ingestRecordLocked(ctx context.Context, rec *kgo.Record) error {
	if off, ok := c.stalled[rec.Partition]; ok {
		return fmt.Errorf("partition %d is stalled at offset %d (dlq publish failed); not adding offset %d",
			rec.Partition, off, rec.Offset)
	}
	b := c.batches[rec.Partition]
	if b != nil && b.sealed {
		// Extending a batch whose flush is unresolved would change its rows and token.
		return fmt.Errorf("partition %d batch [%d,%d] has an unresolved flush; not adding offset %d",
			rec.Partition, b.firstOffset, b.lastOffset, rec.Offset)
	}

	rows, err := normalize.FromOTLP(rec.Value, c.prices)
	if err != nil {
		if derr := c.toDLQ(ctx, rec, err); derr != nil {
			if c.stalled == nil {
				c.stalled = map[int32]int64{}
			}
			c.stalled[rec.Partition] = rec.Offset
			derr = fmt.Errorf("dlq publish (partition %d offset %d): %w", rec.Partition, rec.Offset, derr)
			c.setWriteFailure(rec.Partition, derr)
			return derr
		}
		c.log.Warn("normalization failed, routed to dlq",
			"partition", rec.Partition, "offset", rec.Offset, "err", err)
		rows = nil
	}

	if b == nil {
		b = &batch{}
		c.batches[rec.Partition] = b
	}
	b.note(rec)
	c.inflight += len(rec.Value)
	b.addRows(rec, rows)
	return nil
}

// toDLQ publishes the original record bytes to the DLQ with failure metadata in headers.
func (c *Consumer) toDLQ(ctx context.Context, rec *kgo.Record, cause error) error {
	return c.dlq.ProduceSync(ctx, producer.Record{
		Topic: c.dlqTopic,
		Key:   string(rec.Key),
		Value: rec.Value,
		Headers: map[string]string{
			"error":       cause.Error(),
			"stage":       "normalize",
			"trace_id":    string(rec.Key),
			"received_at": time.Now().UTC().Format(time.RFC3339Nano),
		},
	})
}

// flushLocked inserts a partition's batch to ClickHouse and, on success, commits its last
// offset. Committing only after a successful insert makes redelivery safe: the same range
// reforms with the same dedup token and is dropped before any rows or MV rows are written.
//
// The batch is sealed on the first attempt; insert retries resend the identical rows and
// token (so an insert that landed but reported an error is deduplicated), and once the
// insert succeeds only the commit is retried. All attempts share one retry window (and
// any tighter deadline on ctx). On give-up the batch stays in place, uncommitted, and the
// partition's failure stays visible to readiness until this partition flushes successfully
// or its ownership is released (revoke or loss). Caller holds c.mu.
func (c *Consumer) flushLocked(ctx context.Context, partition int32) error {
	b := c.batches[partition]
	if b == nil || !b.started {
		return nil
	}
	b.seal(c.topic, partition)
	ctx, cancel := context.WithTimeout(ctx, c.retry.window)
	defer cancel()

	if !b.inserted {
		err := c.retry.do(ctx, func(ctx context.Context) error {
			return c.sink.Insert(ctx, b.flushRows, b.flushToken)
		}, func(attempt int, err error) {
			c.setWriteFailure(partition, fmt.Errorf("insert partition %d: %w", partition, err))
			c.log.Warn("insert attempt failed", "partition", partition, "attempt", attempt,
				"token", b.flushToken, "err", err)
		})
		if err != nil {
			return fmt.Errorf("insert partition %d [%d,%d]: %w", partition, b.firstOffset, b.lastOffset, err)
		}
		b.inserted = true
	}
	err := c.retry.do(ctx, func(ctx context.Context) error {
		return c.commit(ctx, b.lastRecord)
	}, func(attempt int, err error) {
		c.setWriteFailure(partition, fmt.Errorf("commit partition %d: %w", partition, err))
		c.log.Warn("commit attempt failed", "partition", partition, "attempt", attempt,
			"offset", b.lastOffset, "err", err)
	})
	if err != nil {
		return fmt.Errorf("commit partition %d offset %d: %w", partition, b.lastOffset, err)
	}

	if _, stalled := c.stalled[partition]; !stalled {
		// A stalled partition keeps its DLQ failure: committing the records before the
		// stalled one does not resolve it.
		c.clearWriteFailure(partition)
	}
	c.log.Info("flushed batch",
		"partition", partition, "first_offset", b.firstOffset, "last_offset", b.lastOffset,
		"rows", len(b.flushRows), "token", b.flushToken)
	c.dropBatchLocked(partition)
	return nil
}

// dropBatchLocked removes a partition's batch and releases its bytes from the inflight
// budget. Caller holds c.mu.
func (c *Consumer) dropBatchLocked(partition int32) {
	if b := c.batches[partition]; b != nil {
		c.inflight -= b.byteSize
		delete(c.batches, partition)
	}
}

// flushStaleLocked flushes any batch older than the flush interval. Caller holds c.mu.
func (c *Consumer) flushStaleLocked(ctx context.Context) error {
	for _, p := range c.sortedPartitionsLocked() {
		if c.batches[p].shouldFlushTime(c.interval) {
			if err := c.flushLocked(ctx, p); err != nil {
				return err
			}
		}
	}
	return nil
}

// flushAllLocked flushes every open batch (used on shutdown), attempting each partition
// even if an earlier one fails. Caller holds c.mu.
func (c *Consumer) flushAllLocked(ctx context.Context) error {
	var errs []error
	for _, p := range c.sortedPartitionsLocked() {
		if err := c.flushLocked(ctx, p); err != nil {
			errs = append(errs, err)
		}
	}
	return errors.Join(errs...)
}

// onRevoked flushes and commits the batches for partitions being taken away, before this
// consumer releases them, so their offset ranges stay contiguous and committed. It runs
// during a rebalance (management goroutine); c.mu serializes it against the poll loop.
// All revoked partitions share one retry window so the callback finishes inside the
// rebalance timeout. A partition whose flush fails is dropped uncommitted (ownership is
// moving, and nothing is committed once the window is spent): the new owner replays the
// range from the last committed offset. The replay may form a different offset range, and
// so a different dedup token, than the dropped batch; see the package comment. A stalled
// partition flushes only the records before its stalled offset. Each revoked partition is
// then released (releaseLocked), which clears its stall and readiness failure but never
// another partition's.
func (c *Consumer) onRevoked(ctx context.Context, _ *kgo.Client, revoked map[string][]int32) {
	c.mu.Lock()
	defer c.mu.Unlock()
	ctx, cancel := context.WithTimeout(ctx, c.retry.window)
	defer cancel()
	parts := slices.Clone(revoked[c.topic])
	slices.Sort(parts)
	for _, p := range parts {
		if err := c.flushLocked(ctx, p); err != nil {
			c.log.Error("flush on revoke failed; range left uncommitted for the new owner to replay",
				"partition", p, "err", err)
			c.dropBatchLocked(p)
		}
		c.releaseLocked(p, "revoked")
	}
}

// onLost drops batches for partitions whose ownership is already gone — offsets cannot be
// committed, so the ranges simply replay from the last committed offset (insert dedup
// covers it only if the replay reforms the same range). Each lost partition is released.
func (c *Consumer) onLost(_ context.Context, _ *kgo.Client, lost map[string][]int32) {
	c.mu.Lock()
	defer c.mu.Unlock()
	for _, p := range lost[c.topic] {
		if b := c.batches[p]; b != nil && b.started {
			c.log.Error("partition lost with an unflushed batch; range left uncommitted for the new owner to replay",
				"partition", p, "first_offset", b.firstOffset, "last_offset", b.lastOffset)
		}
		c.dropBatchLocked(p)
		c.releaseLocked(p, "lost")
	}
}

// releaseLocked forgets a partition this consumer no longer owns: its stall (the next owner
// resumes from the last committed offset, at or before the stalled record) and its
// readiness failure (the unflushed range is now the next owner's work). Readiness tracks
// owned work, so an idle consumer that handed off a failed partition becomes ready again;
// the failure itself was logged when it happened and is logged once more here. Caller
// holds c.mu.
func (c *Consumer) releaseLocked(partition int32, reason string) {
	delete(c.stalled, partition)
	c.failMu.Lock()
	err, failed := c.failures[partition]
	delete(c.failures, partition)
	c.failMu.Unlock()
	if failed {
		c.log.Warn("partition released with an unresolved write failure; clearing it from readiness",
			"partition", partition, "reason", reason, "err", err)
	}
}
