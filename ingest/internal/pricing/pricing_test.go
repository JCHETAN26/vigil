package pricing

import (
	"math"
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
		{"provider prefix stripped", "anthropic.claude-haiku-4-5", 1_000_000, 0, 1.0},
		{"case insensitive", "Claude-Haiku-4-5", 0, 1_000_000, 5.0},
		{"unknown model is zero", "some-unknown-model", 1_000_000, 1_000_000, 0.0},
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
