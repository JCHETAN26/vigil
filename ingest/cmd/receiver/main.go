// Command receiver accepts OTLP traces over gRPC (:4317) and HTTP (:4318), validates and
// splits them by trace id, and publishes per-trace OTLP payloads to Redpanda (raw topic,
// or the DLQ on failure). It exposes health endpoints and shuts down gracefully, flushing
// buffered records.
package main

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	collectortracepb "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	"google.golang.org/grpc"

	"github.com/JCHETAN26/vigil/ingest/internal/config"
	"github.com/JCHETAN26/vigil/ingest/internal/health"
	"github.com/JCHETAN26/vigil/ingest/internal/logging"
	"github.com/JCHETAN26/vigil/ingest/internal/producer"
	"github.com/JCHETAN26/vigil/ingest/internal/receiver"
)

func main() {
	log := logging.New("receiver")
	if err := run(log); err != nil {
		log.Error("receiver exited", "err", err)
		os.Exit(1)
	}
}

func run(log *slog.Logger) error {
	rcfg := config.ReceiverFromEnv()
	kcfg := config.KafkaFromEnv()

	prod, err := producer.New(kcfg.Brokers)
	if err != nil {
		return err
	}

	sink := receiver.NewKafkaSink(prod, rcfg.RawTopic, rcfg.DLQTopic)
	handler := receiver.NewHandler(sink, log)

	// gRPC server.
	grpcSrv := grpc.NewServer(grpc.MaxRecvMsgSize(rcfg.MaxRequestBytes))
	collectortracepb.RegisterTraceServiceServer(grpcSrv, receiver.NewGRPCServer(handler, log))
	grpcLn, err := net.Listen("tcp", rcfg.GRPCAddr)
	if err != nil {
		return err
	}

	// HTTP (OTLP) server.
	httpMux := http.NewServeMux()
	httpMux.Handle("/v1/traces", receiver.NewHTTPHandler(handler, rcfg.MaxRequestBytes, log))
	httpSrv := &http.Server{Addr: rcfg.HTTPAddr, Handler: httpMux}

	// Health server (readiness = brokers reachable).
	healthSrv := &http.Server{
		Addr:    rcfg.HealthAddr,
		Handler: health.Mux(prod.Ping),
	}

	errCh := make(chan error, 3)
	go func() { errCh <- grpcSrv.Serve(grpcLn) }()
	go func() { errCh <- serveHTTP(httpSrv) }()
	go func() { errCh <- serveHTTP(healthSrv) }()

	log.Info("receiver started",
		"grpc", rcfg.GRPCAddr, "http", rcfg.HTTPAddr, "health", rcfg.HealthAddr,
		"raw_topic", rcfg.RawTopic, "dlq_topic", rcfg.DLQTopic)

	// Wait for a signal or a fatal serve error.
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	select {
	case <-stop:
		log.Info("shutdown signal received")
	case err := <-errCh:
		log.Error("server error, shutting down", "err", err)
	}

	// Graceful shutdown: stop accepting, then flush + close the producer.
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()

	grpcSrv.GracefulStop()
	_ = httpSrv.Shutdown(shutdownCtx)
	_ = healthSrv.Shutdown(shutdownCtx)
	if err := prod.Close(shutdownCtx); err != nil {
		return err
	}
	log.Info("receiver stopped")
	return nil
}

// serveHTTP runs an HTTP server, treating a clean shutdown as a non-error.
func serveHTTP(s *http.Server) error {
	if err := s.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	return nil
}
