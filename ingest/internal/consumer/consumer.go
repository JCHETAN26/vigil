// Package consumer reads raw per-trace OTLP records from Redpanda, normalizes them, and
// inserts them into ClickHouse in deterministic contiguous per-partition offset-range
// batches. Offsets are committed only after a successful insert; batches are flushed and
// committed on partition revocation and on shutdown so ranges stay contiguous and
// idempotent across ownership changes and restarts.
package consumer

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
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

	topic    string
	dlqTopic string
	maxRows  int
	maxBytes int
	interval time.Duration

	mu      sync.Mutex
	batches map[int32]*batch
}

// New builds a Consumer. The sink and dlq producer are owned by the caller (closed after
// Run returns).
func New(kcfg config.Kafka, wcfg config.Writer, sink *chsink.Sink, dlq *producer.Producer, prices *pricing.Table, log *slog.Logger) (*Consumer, error) {
	c := &Consumer{
		sink:     sink,
		prices:   prices,
		dlq:      dlq,
		log:      log,
		topic:    wcfg.RawTopic,
		dlqTopic: wcfg.DLQTopic,
		maxRows:  wcfg.BatchMaxRows,
		maxBytes: wcfg.BatchMaxBytes,
		interval: wcfg.FlushInterval,
		batches:  map[int32]*batch{},
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

// Ping reports ClickHouse connectivity (readiness).
func (c *Consumer) Ping(ctx context.Context) error { return c.sink.Ping(ctx) }

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

		if err := c.processFetches(ctx, fetches); err != nil {
			// Allow the blocked rebalance to proceed before returning, so Close can run.
			c.cl.AllowRebalance()
			return err
		}
		// Required with BlockRebalanceOnPoll: let a pending rebalance (and its revoke
		// callback) proceed now that this poll's records are processed.
		c.cl.AllowRebalance()

		c.mu.Lock()
		err := c.flushStaleLocked(ctx)
		c.mu.Unlock()
		if err != nil {
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
			if err := c.ingestRecordLocked(ctx, rec); err != nil {
				procErr = err
				return
			}
			if b := c.batches[rec.Partition]; b != nil && b.shouldFlushSize(c.maxRows, c.maxBytes) {
				if err := c.flushLocked(ctx, rec.Partition); err != nil {
					procErr = err
					return
				}
			}
		}
	})
	return procErr
}

// ingestRecordLocked normalizes one record into its partition batch, or routes it to the
// DLQ on a normalization failure (still extending the offset range so the commit covers
// it). Caller holds c.mu.
func (c *Consumer) ingestRecordLocked(ctx context.Context, rec *kgo.Record) error {
	b := c.batches[rec.Partition]
	if b == nil {
		b = &batch{}
		c.batches[rec.Partition] = b
	}
	b.note(rec)

	rows, err := normalize.FromOTLP(rec.Value, c.prices)
	if err != nil {
		if derr := c.toDLQ(ctx, rec, err); derr != nil {
			return fmt.Errorf("dlq publish (partition %d offset %d): %w", rec.Partition, rec.Offset, derr)
		}
		c.log.Warn("normalization failed, routed to dlq",
			"partition", rec.Partition, "offset", rec.Offset, "err", err)
		return nil
	}
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
// Caller holds c.mu.
func (c *Consumer) flushLocked(ctx context.Context, partition int32) error {
	b := c.batches[partition]
	if b == nil || !b.started {
		return nil
	}
	rows := b.sortedRows()
	token := DedupToken(c.topic, partition, b.firstOffset, b.lastOffset)

	if err := c.sink.Insert(ctx, rows, token); err != nil {
		return fmt.Errorf("insert partition %d [%d,%d]: %w", partition, b.firstOffset, b.lastOffset, err)
	}
	if err := c.commit(ctx, b.lastRecord); err != nil {
		return fmt.Errorf("commit partition %d offset %d: %w", partition, b.lastOffset, err)
	}
	c.log.Info("flushed batch",
		"partition", partition, "first_offset", b.firstOffset, "last_offset", b.lastOffset,
		"rows", len(rows), "token", token)
	delete(c.batches, partition)
	return nil
}

// flushStaleLocked flushes any batch older than the flush interval. Caller holds c.mu.
func (c *Consumer) flushStaleLocked(ctx context.Context) error {
	for p, b := range c.batches {
		if b.shouldFlushTime(c.interval) {
			if err := c.flushLocked(ctx, p); err != nil {
				return err
			}
		}
	}
	return nil
}

// flushAllLocked flushes every open batch (used on shutdown). Caller holds c.mu.
func (c *Consumer) flushAllLocked(ctx context.Context) error {
	for p := range c.batches {
		if err := c.flushLocked(ctx, p); err != nil {
			return err
		}
	}
	return nil
}

// onRevoked flushes and commits the batches for partitions being taken away, before this
// consumer releases them, so their offset ranges stay contiguous and committed. It runs
// during a rebalance (management goroutine); c.mu serializes it against the poll loop.
func (c *Consumer) onRevoked(ctx context.Context, _ *kgo.Client, revoked map[string][]int32) {
	c.mu.Lock()
	defer c.mu.Unlock()
	for topic, parts := range revoked {
		if topic != c.topic {
			continue
		}
		for _, p := range parts {
			if err := c.flushLocked(ctx, p); err != nil {
				c.log.Error("flush on revoke failed", "partition", p, "err", err)
			}
		}
	}
}

// onLost drops batches for partitions whose ownership is already gone — offsets cannot be
// committed, so the ranges simply replay and insert-dedup covers the redelivery.
func (c *Consumer) onLost(_ context.Context, _ *kgo.Client, lost map[string][]int32) {
	c.mu.Lock()
	defer c.mu.Unlock()
	for topic, parts := range lost {
		if topic != c.topic {
			continue
		}
		for _, p := range parts {
			delete(c.batches, p)
		}
	}
}
