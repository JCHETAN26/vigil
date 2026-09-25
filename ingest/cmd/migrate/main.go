// Command migrate applies the ClickHouse schema (tables + materialized views) for the
// ingestion path. It is idempotent — every statement uses IF NOT EXISTS — so it is safe
// to run repeatedly, including from integration-test setup.
package main

import (
	"context"
	"os"
	"time"

	"github.com/ClickHouse/clickhouse-go/v2"

	"github.com/JCHETAN26/vigil/ingest/internal/config"
	"github.com/JCHETAN26/vigil/ingest/internal/logging"
	"github.com/JCHETAN26/vigil/ingest/internal/schema"
)

func main() {
	log := logging.New("migrate")

	ch := config.ClickHouseFromEnv()
	if err := config.MustNonEmpty("CLICKHOUSE_PASSWORD", ch.Password); err != nil {
		log.Error("invalid config", "err", err)
		os.Exit(1)
	}

	stmts, err := schema.Statements()
	if err != nil {
		log.Error("load schema", "err", err)
		os.Exit(1)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	conn, err := clickhouse.Open(&clickhouse.Options{
		Addr: []string{ch.Addr},
		Auth: clickhouse.Auth{Database: ch.Database, Username: ch.Username, Password: ch.Password},
	})
	if err != nil {
		log.Error("open clickhouse", "addr", ch.Addr, "err", err)
		os.Exit(1)
	}
	defer conn.Close()

	if err := conn.Ping(ctx); err != nil {
		log.Error("ping clickhouse", "addr", ch.Addr, "err", err)
		os.Exit(1)
	}

	for _, s := range stmts {
		if err := conn.Exec(ctx, s.SQL); err != nil {
			log.Error("apply statement", "file", s.File, "err", err)
			os.Exit(1)
		}
		log.Info("applied", "file", s.File)
	}

	log.Info("schema up to date", "database", ch.Database, "statements", len(stmts))
}
