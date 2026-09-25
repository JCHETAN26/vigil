// Package config parses ingest configuration from environment variables. Each binary
// reads only the sections it needs. Defaults target the local Docker stack defined in
// deploy/docker-compose.yml (all services bound to 127.0.0.1).
package config

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

// ClickHouse holds connection settings for the ClickHouse native protocol (port 9000).
type ClickHouse struct {
	Addr     string // host:port, native protocol
	Database string
	Username string
	Password string
}

// ClickHouseFromEnv reads ClickHouse settings. It reuses the CLICKHOUSE_* names from
// .env and adds VIGIL_CLICKHOUSE_ADDR for the native endpoint.
func ClickHouseFromEnv() ClickHouse {
	return ClickHouse{
		Addr:     envStr("VIGIL_CLICKHOUSE_ADDR", "127.0.0.1:9000"),
		Database: envStr("CLICKHOUSE_DB", "vigil"),
		Username: envStr("CLICKHOUSE_USER", "vigil"),
		Password: os.Getenv("CLICKHOUSE_PASSWORD"),
	}
}

// Receiver holds settings for the OTLP receiver binary.
type Receiver struct {
	GRPCAddr        string // OTLP/gRPC listener
	HTTPAddr        string // OTLP/HTTP listener (POST /v1/traces)
	HealthAddr      string // /healthz + /readyz listener
	MaxRequestBytes int    // reject larger request bodies / messages
	RawTopic        string
	DLQTopic        string
}

// ReceiverFromEnv reads the receiver settings. Listeners default to loopback, matching
// the rest of the local stack; override the *_ADDR vars for containerized use.
func ReceiverFromEnv() Receiver {
	return Receiver{
		GRPCAddr:        envStr("VIGIL_OTLP_GRPC_ADDR", "127.0.0.1:4317"),
		HTTPAddr:        envStr("VIGIL_OTLP_HTTP_ADDR", "127.0.0.1:4318"),
		HealthAddr:      envStr("VIGIL_HEALTH_ADDR", "127.0.0.1:8088"),
		MaxRequestBytes: envInt("VIGIL_MAX_REQUEST_BYTES", 4*1024*1024),
		RawTopic:        envStr("VIGIL_RAW_TOPIC", "otlp.spans.raw"),
		DLQTopic:        envStr("VIGIL_DLQ_TOPIC", "spans.dlq"),
	}
}

// Kafka holds the broker list shared by the producer, consumer, and admin client.
type Kafka struct {
	Brokers []string
}

// KafkaFromEnv reads the broker list from VIGIL_KAFKA_BROKERS (comma-separated).
// The default is the Redpanda external listener published on the host.
func KafkaFromEnv() Kafka {
	return Kafka{Brokers: envList("VIGIL_KAFKA_BROKERS", "127.0.0.1:19092")}
}

// Topic describes one Redpanda topic to create.
type Topic struct {
	Name        string
	Partitions  int32
	RetentionMS int64 // retention.ms (0 = broker default)
	// RetentionTotalBytes is a TOTAL disk budget for the topic across all partitions.
	// Kafka/Redpanda's retention.bytes is a PER-PARTITION limit, so the topic creator
	// divides this by Partitions before setting it. 0 = unset (time-based retention only).
	RetentionTotalBytes int64
	Compression         string
}

// RetentionBytesPerPartition converts the total disk budget into the per-partition
// retention.bytes value Kafka expects. Returns 0 when unset.
func (t Topic) RetentionBytesPerPartition() int64 {
	if t.RetentionTotalBytes <= 0 || t.Partitions <= 0 {
		return 0
	}
	return t.RetentionTotalBytes / int64(t.Partitions)
}

// Topics holds the topic layout from docs/design/data-model.md §4.
type Topics struct {
	Raw Topic
	DLQ Topic
}

// TopicsFromEnv builds the topic layout, allowing names/partitions to be overridden.
func TopicsFromEnv() Topics {
	return Topics{
		Raw: Topic{
			Name:                envStr("VIGIL_RAW_TOPIC", "otlp.spans.raw"),
			Partitions:          int32(envInt("VIGIL_RAW_PARTITIONS", 6)),
			RetentionMS:         envInt64("VIGIL_RAW_RETENTION_MS", int64(48*time.Hour/time.Millisecond)),
			RetentionTotalBytes: envInt64("VIGIL_RAW_RETENTION_BYTES", 4*1024*1024*1024), // ~4 GiB total across partitions
			Compression:         envStr("VIGIL_RAW_COMPRESSION", "zstd"),
		},
		DLQ: Topic{
			Name:        envStr("VIGIL_DLQ_TOPIC", "spans.dlq"),
			Partitions:  int32(envInt("VIGIL_DLQ_PARTITIONS", 1)),
			RetentionMS: envInt64("VIGIL_DLQ_RETENTION_MS", int64(14*24*time.Hour/time.Millisecond)),
		},
	}
}

// --- env helpers ---

func envStr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envList(key, def string) []string {
	raw := envStr(key, def)
	parts := strings.Split(raw, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func envInt64(key string, def int64) int64 {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.ParseInt(v, 10, 64); err == nil {
			return n
		}
	}
	return def
}

// MustNonEmpty returns an error if a required value is empty, for use at startup.
func MustNonEmpty(name, value string) error {
	if strings.TrimSpace(value) == "" {
		return fmt.Errorf("%s must be set", name)
	}
	return nil
}
