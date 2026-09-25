// Command writer consumes raw per-trace OTLP records from Redpanda, normalizes them, and
// inserts them into ClickHouse in deterministic contiguous offset-range batches with
// insert-time deduplication. It commits offsets only after successful inserts, flushes on
// partition revocation, and flushes everything on graceful shutdown.
package main

import (
	"context"
	"errors"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/JCHETAN26/vigil/ingest/internal/chsink"
	"github.com/JCHETAN26/vigil/ingest/internal/config"
	"github.com/JCHETAN26/vigil/ingest/internal/consumer"
	"github.com/JCHETAN26/vigil/ingest/internal/health"
	"github.com/JCHETAN26/vigil/ingest/internal/logging"
	"github.com/JCHETAN26/vigil/ingest/internal/pricing"
	"github.com/JCHETAN26/vigil/ingest/internal/producer"
)

func main() {
	log := logging.New("writer")
	if err := run(); err != nil {
		log.Error("writer exited", "err", err)
		os.Exit(1)
	}
	log.Info("writer stopped")
}

func run() error {
	log := logging.New("writer")

	kcfg := config.KafkaFromEnv()
	chcfg := config.ClickHouseFromEnv()
	wcfg := config.WriterFromEnv()

	if err := config.MustNonEmpty("CLICKHOUSE_PASSWORD", chcfg.Password); err != nil {
		return err
	}

	prices, err := pricing.Load()
	if err != nil {
		return err
	}
	prices.SetLogger(log)
	log.Info("pricing loaded", "prices_as_of", prices.PricesAsOf, "models", len(prices.Models))

	openCtx, cancelOpen := context.WithTimeout(context.Background(), 15*time.Second)
	sink, err := chsink.Open(openCtx, chcfg)
	cancelOpen()
	if err != nil {
		return err
	}
	defer sink.Close()

	dlq, err := producer.New(kcfg.Brokers)
	if err != nil {
		return err
	}

	cons, err := consumer.New(kcfg, wcfg, sink, dlq, prices, log)
	if err != nil {
		return err
	}
	defer cons.Close()

	healthSrv := &http.Server{Addr: wcfg.HealthAddr, Handler: health.Mux(cons.Ping)}
	go func() {
		if err := healthSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error("health server error", "err", err)
		}
	}()

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	log.Info("writer started",
		"brokers", kcfg.Brokers, "group", wcfg.Group, "raw_topic", wcfg.RawTopic,
		"clickhouse", chcfg.Addr, "batch_max_rows", wcfg.BatchMaxRows,
		"batch_max_bytes", wcfg.BatchMaxBytes, "flush_interval", wcfg.FlushInterval.String())

	// Run blocks until ctx is canceled, then flushes and commits in-flight batches.
	runErr := cons.Run(ctx)

	// Drain remaining DLQ sends and stop the health server.
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = dlq.Close(shutdownCtx)
	_ = healthSrv.Shutdown(shutdownCtx)

	return runErr
}
