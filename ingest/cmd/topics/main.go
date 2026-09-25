// Command topics creates the Redpanda topics for the ingestion path (see §4):
// otlp.spans.raw (6 partitions, ~48h/size-capped, zstd) and spans.dlq (1 partition,
// 14d). It is idempotent — an already-existing topic is treated as success.
package main

import (
	"context"
	"errors"
	"os"
	"strconv"
	"time"

	"github.com/twmb/franz-go/pkg/kadm"
	"github.com/twmb/franz-go/pkg/kerr"
	"github.com/twmb/franz-go/pkg/kgo"

	"github.com/JCHETAN26/vigil/ingest/internal/config"
	"github.com/JCHETAN26/vigil/ingest/internal/logging"
)

const replicationFactor = 1 // single-node dev; production would use 3

func main() {
	log := logging.New("topics")

	kcfg := config.KafkaFromEnv()
	topics := config.TopicsFromEnv()

	cl, err := kgo.NewClient(kgo.SeedBrokers(kcfg.Brokers...))
	if err != nil {
		log.Error("kafka client", "brokers", kcfg.Brokers, "err", err)
		os.Exit(1)
	}
	defer cl.Close()

	adm := kadm.NewClient(cl)

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	for _, t := range []config.Topic{topics.Raw, topics.DLQ} {
		if err := ensureTopic(ctx, adm, t); err != nil {
			log.Error("ensure topic", "topic", t.Name, "err", err)
			os.Exit(1)
		}
		log.Info("topic ready", "topic", t.Name, "partitions", t.Partitions)
	}
}

// ensureTopic creates one topic, treating "already exists" as success. Existing topics
// are left as-is (partition counts and retention are not altered here).
func ensureTopic(ctx context.Context, adm *kadm.Client, t config.Topic) error {
	cfgs := map[string]*string{}
	if t.RetentionMS > 0 {
		cfgs["retention.ms"] = strptr(strconv.FormatInt(t.RetentionMS, 10))
	}
	if perPart := t.RetentionBytesPerPartition(); perPart > 0 {
		// retention.bytes is per-partition; RetentionTotalBytes is the total budget.
		cfgs["retention.bytes"] = strptr(strconv.FormatInt(perPart, 10))
	}
	if t.Compression != "" {
		cfgs["compression.type"] = strptr(t.Compression)
	}
	cfgs["cleanup.policy"] = strptr("delete")

	resp, err := adm.CreateTopics(ctx, t.Partitions, replicationFactor, cfgs, t.Name)
	if err != nil {
		return err
	}
	for _, r := range resp.Sorted() {
		if r.Err != nil && !errors.Is(r.Err, kerr.TopicAlreadyExists) {
			return r.Err
		}
	}
	return nil
}

func strptr(s string) *string { return &s }
