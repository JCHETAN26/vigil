// Package pricing computes per-span USD cost from model id and token counts, using an
// embedded price table. Prices are USD per one million tokens; the table records the
// date they were taken (prices_as_of) so its provenance is auditable.
package pricing

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"strings"
)

//go:embed prices.json
var pricesJSON []byte

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
	return &t, nil
}

// Cost returns the USD cost for a span given its model and token counts. Unknown or
// empty models cost 0 (the consumer records 0 rather than failing), matching the schema
// note "computed by consumer; 0 if unknown".
func (t *Table) Cost(model string, inputTokens, outputTokens uint32) float64 {
	p, ok := t.Models[normalizeModel(model)]
	if !ok {
		return 0
	}
	return float64(inputTokens)/1e6*p.Input + float64(outputTokens)/1e6*p.Output
}

// normalizeModel lowercases the id and strips a provider prefix (e.g. "anthropic.") so
// ids from different transports resolve to the same table entry.
func normalizeModel(model string) string {
	m := strings.ToLower(strings.TrimSpace(model))
	if i := strings.Index(m, "."); i >= 0 {
		if _, isVersion := map[string]struct{}{"anthropic": {}, "openai": {}}[m[:i]]; isVersion {
			m = m[i+1:]
		}
	}
	return m
}
