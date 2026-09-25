// Package producer wraps a franz-go client for the receiver's write path: keyed,
// zstd-compressed, idempotent, acks=all synchronous produces to the raw and DLQ topics.
package producer

import (
	"context"

	"github.com/twmb/franz-go/pkg/kgo"
)

// Producer is a thin synchronous producer. It is safe for concurrent use.
type Producer struct {
	cl *kgo.Client
}

// New builds a producer against the given brokers. It enables the idempotent producer
// (acks=all by default in franz-go) and zstd compression, and keys records by trace id
// via the default sticky-key (murmur2) partitioner so a trace's records share a partition.
func New(brokers []string) (*Producer, error) {
	cl, err := kgo.NewClient(
		kgo.SeedBrokers(brokers...),
		kgo.RequiredAcks(kgo.AllISRAcks()),
		kgo.ProducerBatchCompression(kgo.ZstdCompression()),
		kgo.RecordPartitioner(kgo.StickyKeyPartitioner(nil)),
	)
	if err != nil {
		return nil, err
	}
	return &Producer{cl: cl}, nil
}

// Record is a single message to produce.
type Record struct {
	Topic   string
	Key     string
	Value   []byte
	Headers map[string]string
}

// ProduceSync produces all records and blocks until they are acknowledged (or one
// fails). Returning only after acks gives the caller at-least-once durability before it
// acknowledges the upstream OTLP request.
func (p *Producer) ProduceSync(ctx context.Context, recs ...Record) error {
	krecs := make([]*kgo.Record, 0, len(recs))
	for _, r := range recs {
		kr := &kgo.Record{Topic: r.Topic, Key: []byte(r.Key), Value: r.Value}
		for k, v := range r.Headers {
			kr.Headers = append(kr.Headers, kgo.RecordHeader{Key: k, Value: []byte(v)})
		}
		krecs = append(krecs, kr)
	}
	return p.cl.ProduceSync(ctx, krecs...).FirstErr()
}

// Ping verifies broker connectivity, for readiness checks.
func (p *Producer) Ping(ctx context.Context) error { return p.cl.Ping(ctx) }

// Close flushes buffered records and closes the client.
func (p *Producer) Close(ctx context.Context) error {
	if err := p.cl.Flush(ctx); err != nil {
		return err
	}
	p.cl.Close()
	return nil
}
