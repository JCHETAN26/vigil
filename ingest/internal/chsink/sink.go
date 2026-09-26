package chsink

import (
	"context"
	"fmt"

	"github.com/ClickHouse/clickhouse-go/v2"
	"github.com/ClickHouse/clickhouse-go/v2/lib/driver"

	"github.com/JCHETAN26/vigil/ingest/internal/config"
)

// Sink inserts span rows into ClickHouse over the native protocol.
type Sink struct {
	conn driver.Conn
}

// Open connects to ClickHouse using the given config and verifies the connection.
func Open(ctx context.Context, cfg config.ClickHouse) (*Sink, error) {
	conn, err := clickhouse.Open(&clickhouse.Options{
		Addr: []string{cfg.Addr},
		Auth: clickhouse.Auth{Database: cfg.Database, Username: cfg.Username, Password: cfg.Password},
	})
	if err != nil {
		return nil, err
	}
	if err := conn.Ping(ctx); err != nil {
		return nil, fmt.Errorf("ping clickhouse %s: %w", cfg.Addr, err)
	}
	return &Sink{conn: conn}, nil
}

// Ping reports connectivity, for readiness checks.
func (s *Sink) Ping(ctx context.Context) error { return s.conn.Ping(ctx) }

// Close closes the connection.
func (s *Sink) Close() error { return s.conn.Close() }

// Insert writes rows as a single batch, tagged with dedupToken. Two settings are sent on
// every insert:
//   - insert_deduplication_token: the deterministic offset-range token; a redelivered
//     batch with the same token is dropped in full before any rows are written.
//   - deduplicate_blocks_in_dependent_materialized_views: propagates that dedup decision
//     into the trace_index and agent_version_stats_daily MVs, so the rollups are not
//     double-counted (they fire on insert, before ReplacingMergeTree can dedup on merge).
//
// Callers must pass rows in a deterministic order so wire-block splitting stays
// reproducible across replays (the writer sorts by source offset, then span id).
func (s *Sink) Insert(ctx context.Context, rows []Row, dedupToken string) error {
	if len(rows) == 0 {
		return nil
	}
	ctx = clickhouse.Context(ctx, clickhouse.WithSettings(clickhouse.Settings{
		"insert_deduplication_token":                         dedupToken,
		"deduplicate_blocks_in_dependent_materialized_views": 1,
	}))

	batch, err := s.conn.PrepareBatch(ctx, "INSERT INTO spans ("+insertColumns+")")
	if err != nil {
		return fmt.Errorf("prepare batch: %w", err)
	}
	for i := range rows {
		r := &rows[i]
		if err := batch.Append(
			r.TraceID, r.SpanID, r.ParentSpanID, r.TraceState,
			r.StartTime, r.EndTime,
			r.SpanName, r.SpanKind, r.StatusCode, r.StatusMessage,
			r.ServiceName, r.ServiceVersion,
			r.AgentID, r.AgentVersion, r.GitSHA, r.RunID, r.RunKind, r.EvalRunID, r.EvalCaseID, r.EvalTrial, r.SessionID, r.Role,
			r.GenAISystem, r.GenAIOperationName, r.GenAIRequestModel, r.GenAIResponseModel,
			r.GenAIUsageInputTokens, r.GenAIUsageOutputTokens, r.CostUSD, r.CacheHit,
			r.ResourceAttributes, r.SpanAttributes,
			r.EventsTimestamp, r.EventsName, r.EventsAttributes,
		); err != nil {
			return fmt.Errorf("append row %d: %w", i, err)
		}
	}
	return batch.Send()
}
