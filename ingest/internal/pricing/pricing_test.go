package pricing

import (
	"bytes"
	"log/slog"
	"math"
	"strings"
	"testing"
)

func TestCost(t *testing.T) {
	tbl, err := Load()
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if tbl.PricesAsOf == "" {
		t.Error("prices_as_of must be recorded")
	}
	if _, ok := tbl.Models["claude-haiku-4-5"]; !ok {
		t.Fatal("claude-haiku-4-5 must be present in the pricing table")
	}

	tests := []struct {
		name    string
		model   string
		in, out uint32
		wantUSD float64
	}{
		// haiku 4.5: $1/M in, $5/M out. 1M in + 1M out = 1 + 5 = 6.
		{"haiku exact million", "claude-haiku-4-5", 1_000_000, 1_000_000, 6.0},
		{"haiku small", "claude-haiku-4-5", 1000, 2000, 1000.0/1e6*1.0 + 2000.0/1e6*5.0},
		{"dated identifier (YYYYMMDD)", "claude-haiku-4-5-20251001", 1_000_000, 0, 1.0},
		{"dated identifier (YYYY-MM-DD)", "claude-haiku-4-5-2025-10-01", 0, 1_000_000, 5.0},
		{"provider prefix + dated", "anthropic.claude-haiku-4-5-20251001", 1_000_000, 0, 1.0},
		{"case insensitive", "Claude-Haiku-4-5", 0, 1_000_000, 5.0},
		{"sonnet 5", "claude-sonnet-5", 1_000_000, 1_000_000, 12.0},
		{"opus 5.5", "claude-opus-5-5", 1_000_000, 1_000_000, 24.0},
		{"opus 5.5 dated", "claude-opus-5-5-20260401", 1_000_000, 0, 4.0},
		{"unknown model is zero", "some-unknown-model", 1_000_000, 1_000_000, 0.0},
		{"unknown dated model is zero", "mystery-model-20251001", 1_000_000, 0, 0.0},
		{"empty model is zero", "", 500, 500, 0.0},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := tbl.Cost(tt.model, tt.in, tt.out)
			if math.Abs(got-tt.wantUSD) > 1e-9 {
				t.Errorf("Cost(%q, %d, %d) = %v, want %v", tt.model, tt.in, tt.out, got, tt.wantUSD)
			}
		})
	}
}

func TestCostWithCache(t *testing.T) {
	tbl, err := Load()
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	// The cache rates must be present for the models we cache with.
	if p := tbl.Models["claude-haiku-4-5"]; p.CacheWrite5m != 1.25 || p.CacheRead != 0.1 {
		t.Fatalf("haiku cache rates = write %v read %v, want 1.25 / 0.1", p.CacheWrite5m, p.CacheRead)
	}

	tests := []struct {
		name                           string
		model                          string
		in, cacheWrite, cacheRead, out uint32
		wantUSD                        float64
	}{
		// haiku 4.5: in $1, out $5, cache-write(5m) $1.25, cache-read $0.10 per 1M.
		{"haiku cache write only", "claude-haiku-4-5", 0, 1_000_000, 0, 0, 1.25},
		{"haiku cache read only", "claude-haiku-4-5", 0, 0, 1_000_000, 0, 0.10},
		// A realistic call: a small uncached delta, a large cache read, a little output.
		{"haiku mixed", "claude-haiku-4-5", 200, 0, 8000, 150,
			200.0/1e6*1.0 + 8000.0/1e6*0.10 + 150.0/1e6*5.0},
		// All four buckets at 1M each: 1.0 + 1.25 + 0.10 + 5.0 = 7.35.
		{"haiku all buckets", "claude-haiku-4-5", 1_000_000, 1_000_000, 1_000_000, 1_000_000, 7.35},
		// Opus 5.5 cache read is the 0.05x exception: 4.0 * 0.05 = $0.20/M.
		{"opus cache read exception", "claude-opus-5-5", 0, 0, 1_000_000, 0, 0.20},
		{"opus cache write", "claude-opus-5-5", 0, 1_000_000, 0, 0, 5.0},
		// Unknown model costs 0 even with cache tokens.
		{"unknown with cache is zero", "mystery", 1_000_000, 1_000_000, 1_000_000, 1_000_000, 0.0},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := tbl.CostWithCache(tt.model, tt.in, tt.cacheWrite, tt.cacheRead, tt.out)
			if math.Abs(got-tt.wantUSD) > 1e-9 {
				t.Errorf("CostWithCache(%q, in=%d, cw=%d, cr=%d, out=%d) = %v, want %v",
					tt.model, tt.in, tt.cacheWrite, tt.cacheRead, tt.out, got, tt.wantUSD)
			}
		})
	}

	// Cost is exactly the zero-cache case of CostWithCache.
	if a, b := tbl.Cost("claude-haiku-4-5", 1000, 2000),
		tbl.CostWithCache("claude-haiku-4-5", 1000, 0, 0, 2000); a != b {
		t.Errorf("Cost != CostWithCache zero-cache case: %v vs %v", a, b)
	}
}

func TestUnknownModelWarnsOnce(t *testing.T) {
	tbl, err := Load()
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	var buf bytes.Buffer
	tbl.SetLogger(slog.New(slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: slog.LevelWarn})))

	// Same unknown model, twice, plus a dated variant of the same base — one warning total.
	tbl.Cost("mystery-model", 1, 1)
	tbl.Cost("mystery-model", 2, 2)
	tbl.Cost("mystery-model-20251001", 3, 3)

	if n := strings.Count(buf.String(), "not in pricing table"); n != 1 {
		t.Errorf("expected exactly 1 warning for the unknown model, got %d\n%s", n, buf.String())
	}

	// An empty model must never warn (many spans legitimately have no model).
	buf.Reset()
	tbl.Cost("", 1, 1)
	if buf.Len() != 0 {
		t.Errorf("empty model should not warn, got: %s", buf.String())
	}
}
