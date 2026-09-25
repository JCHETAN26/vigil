// Package health provides a small HTTP handler exposing /healthz (liveness) and
// /readyz (readiness) for the ingest binaries.
package health

import (
	"context"
	"net/http"
	"time"
)

// Check reports whether a dependency is ready. It should be quick and side-effect free.
type Check func(ctx context.Context) error

// Mux returns an http.Handler serving /healthz (always 200 while the process runs) and
// /readyz (200 only when ready returns nil). A nil ready check makes /readyz always 200.
func Mux(ready Check) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})
	mux.HandleFunc("/readyz", func(w http.ResponseWriter, r *http.Request) {
		if ready == nil {
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte("ready"))
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
		defer cancel()
		if err := ready(ctx); err != nil {
			http.Error(w, "not ready: "+err.Error(), http.StatusServiceUnavailable)
			return
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ready"))
	})
	return mux
}
