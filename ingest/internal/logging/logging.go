// Package logging provides a shared structured (JSON) slog logger for the ingest
// binaries, configured from the environment.
package logging

import (
	"log/slog"
	"os"
	"strings"
)

// New returns a JSON slog.Logger writing to stderr. The level is read from the
// VIGIL_LOG_LEVEL environment variable (debug|info|warn|error); it defaults to info.
// The service name is attached to every record as "service".
func New(service string) *slog.Logger {
	level := slog.LevelInfo
	switch strings.ToLower(os.Getenv("VIGIL_LOG_LEVEL")) {
	case "debug":
		level = slog.LevelDebug
	case "warn", "warning":
		level = slog.LevelWarn
	case "error":
		level = slog.LevelError
	}
	h := slog.NewJSONHandler(os.Stderr, &slog.HandlerOptions{Level: level})
	return slog.New(h).With("service", service)
}
