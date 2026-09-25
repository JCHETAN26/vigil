// Package pricing computes per-span USD cost from model id and token counts, using an
// embedded price table. Prices are USD per one million tokens; the table records the
// date they were taken (prices_as_of) so its provenance is auditable.
package pricing

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"log/slog"
	"regexp"
	"strings"
	"sync"
)

//go:embed prices.json
var pricesJSON []byte

// Providers return dated model identifiers (e.g. claude-haiku-4-5-20251001 or
// gpt-4o-2024-08-06). Lookups normalize to the base model name by stripping a trailing
// date suffix so a dated id resolves to the same table entry.
var (
	reDate8    = regexp.MustCompile(`-\d{8}$`)             // -YYYYMMDD
	reDateDash = regexp.MustCompile(`-\d{4}-\d{2}-\d{2}$`) // -YYYY-MM-DD
)

type modelPrice struct {
	Input  float64 `json:"input"`  // USD per 1M input tokens
	Output float64 `json:"output"` // USD per 1M output tokens
}

// Table is a loaded price table.
type Table struct {
	PricesAsOf string                `json:"prices_as_of"`
	Unit       string                `json:"unit"`
	Source     string                `json:"source"`
	Models     map[string]modelPrice `json:"models"`

	mu     sync.Mutex
	warned map[string]struct{}
	log    *slog.Logger
}

// Load parses the embedded price table.
func Load() (*Table, error) {
	var t Table
	if err := json.Unmarshal(pricesJSON, &t); err != nil {
		return nil, fmt.Errorf("parse prices.json: %w", err)
	}
	if len(t.Models) == 0 {
		return nil, fmt.Errorf("prices.json has no models")
	}
	t.warned = map[string]struct{}{}
	return &t, nil
}

// SetLogger attaches a logger used to warn (once per normalized model) about models that
// are missing from the table. Without a logger, unknown models are still costed as 0
// silently.
func (t *Table) SetLogger(l *slog.Logger) { t.log = l }

// Cost returns the USD cost for a span given its model and token counts. Unknown or empty
// models cost 0 (the consumer records 0 rather than failing), matching the schema note
// "computed by consumer; 0 if unknown". An unknown non-empty model is logged once.
func (t *Table) Cost(model string, inputTokens, outputTokens uint32) float64 {
	base := normalizeModel(model)
	if base == "" {
		return 0 // spans with no model (e.g. non-LLM spans) are not a pricing gap
	}
	p, ok := t.Models[base]
	if !ok {
		t.warnUnknown(base, model)
		return 0
	}
	return float64(inputTokens)/1e6*p.Input + float64(outputTokens)/1e6*p.Output
}

func (t *Table) warnUnknown(base, raw string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if _, seen := t.warned[base]; seen {
		return
	}
	t.warned[base] = struct{}{}
	if t.log != nil {
		t.log.Warn("model not in pricing table; cost recorded as 0",
			"model", raw, "normalized", base, "prices_as_of", t.PricesAsOf)
	}
}

// normalizeModel lowercases the id, strips a provider prefix (e.g. "anthropic."), and
// strips a trailing date suffix so dated identifiers resolve to their base model.
func normalizeModel(model string) string {
	m := strings.ToLower(strings.TrimSpace(model))
	if i := strings.Index(m, "."); i >= 0 {
		if prefix := m[:i]; prefix == "anthropic" || prefix == "openai" {
			m = m[i+1:]
		}
	}
	m = reDateDash.ReplaceAllString(m, "")
	m = reDate8.ReplaceAllString(m, "")
	return m
}
