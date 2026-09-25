package receiver

import (
	"context"
	"time"

	"github.com/JCHETAN26/vigil/ingest/internal/producer"
)

// kafkaSink is the production Sink: it publishes to the raw and DLQ topics via the
// producer. The DLQ record keeps the original bytes as its value (replayable) and puts
// the failure metadata in headers.
type kafkaSink struct {
	prod     *producer.Producer
	rawTopic string
	dlqTopic string
}

// NewKafkaSink builds a Sink backed by prod, writing to rawTopic and dlqTopic.
func NewKafkaSink(prod *producer.Producer, rawTopic, dlqTopic string) Sink {
	return &kafkaSink{prod: prod, rawTopic: rawTopic, dlqTopic: dlqTopic}
}

func (s *kafkaSink) Raw(ctx context.Context, traceID string, payload []byte) error {
	return s.prod.ProduceSync(ctx, producer.Record{
		Topic: s.rawTopic,
		Key:   traceID,
		Value: payload,
	})
}

func (s *kafkaSink) DLQ(ctx context.Context, traceID string, payload []byte, stage, reason string) error {
	return s.prod.ProduceSync(ctx, producer.Record{
		Topic: s.dlqTopic,
		Key:   traceID,
		Value: payload,
		Headers: map[string]string{
			"error":       reason,
			"stage":       stage,
			"trace_id":    traceID,
			"received_at": time.Now().UTC().Format(time.RFC3339Nano),
		},
	})
}
