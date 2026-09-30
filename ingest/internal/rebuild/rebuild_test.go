package rebuild

import (
	"strings"
	"testing"
)

// View definitions exactly as ClickHouse 24.8 stores them (system.tables.as_select) for the
// current schema (002 trace_index, 006 rollup), captured from a migrated database.
const (
	traceIndexSelect = "SELECT trace_id, min(start_time) AS start_time, max(end_time) AS end_time, " +
		"any(agent_id) AS agent_id, any(agent_version) AS agent_version, any(run_id) AS run_id, " +
		"any(run_kind) AS run_kind, any(service_name) AS service_name, sum(toUInt64(1)) AS span_count, " +
		"sum(toUInt64(status_code = 'ERROR')) AS error_count, sum(toUInt64(gen_ai_usage_total_tokens)) AS total_tokens, " +
		"sum(cost_usd) AS total_cost_usd FROM vigil.spans GROUP BY trace_id"
	rollupSelect = "SELECT toDate(start_time) AS day, agent_id, agent_version, run_kind, role, " +
		"gen_ai_operation_name, gen_ai_response_model, uniqState(run_id) AS runs, sum(toUInt64(1)) AS span_count, " +
		"sum(toUInt64(status_code = 'ERROR')) AS error_count, " +
		"quantilesTDigestState(0.5, 0.95, 0.99)(duration_ns) AS duration_quantiles, " +
		"sum(toUInt64(gen_ai_usage_input_tokens)) AS input_tokens, sum(toUInt64(gen_ai_usage_output_tokens)) AS output_tokens, " +
		"sumIf(cost_usd, cache_hit = 0) AS cost_usd FROM vigil.spans GROUP BY day, agent_id, agent_version, run_kind, " +
		"role, gen_ai_operation_name, gen_ai_response_model"
)

func TestRewriteSelect(t *testing.T) {
	tests := []struct {
		name, in, db, where string
		want                string // substring that must appear
		wantErr             bool
	}{
		{"trace_index, whole table", traceIndexSelect, "vigil", "",
			"FROM vigil.spans FINAL GROUP BY trace_id", false},
		{"rollup, one month", rollupSelect, "vigil", "toYYYYMM(start_time) = 202609",
			"FROM vigil.spans FINAL WHERE toYYYYMM(start_time) = 202609 GROUP BY day", false},
		{"other database name", strings.Replace(traceIndexSelect, "vigil.spans", "vigil_load.spans", 1), "vigil_load", "",
			"FROM vigil_load.spans FINAL GROUP BY", false},
		{"does not match a prefix-named table", strings.Replace(traceIndexSelect, "vigil.spans", "vigil.spans_import", 1), "vigil", "",
			"", true},
		{"no read of spans", "SELECT 1 FROM vigil.other", "vigil", "", "", true},
		{"two reads of spans", traceIndexSelect + " UNION ALL " + traceIndexSelect, "vigil", "", "", true},
		{"existing WHERE", strings.Replace(traceIndexSelect, "GROUP BY", "WHERE 1 GROUP BY", 1), "vigil", "", "", true},
		{"already FINAL", strings.Replace(traceIndexSelect, "vigil.spans", "vigil.spans FINAL", 1), "vigil", "", "", true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := RewriteSelect(tc.in, tc.db, tc.where)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("want an error, got %q", got)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if !strings.Contains(got, tc.want) {
				t.Fatalf("rewrite missing %q:\n%s", tc.want, got)
			}
			// Everything but the FROM clause is the view's own SELECT, untouched.
			head := tc.in[:strings.Index(tc.in, " FROM ")]
			if !strings.HasPrefix(got, head) {
				t.Fatal("the SELECT list must be the view's, verbatim")
			}
		})
	}
}

func TestPartitionLiteral(t *testing.T) {
	for in, want := range map[string]string{"": "tuple()", "all": "tuple()", "202609": "202609"} {
		if got := PartitionLiteral(in); got != want {
			t.Errorf("PartitionLiteral(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestTargets(t *testing.T) {
	ti, ok := TargetByName("trace_index")
	if !ok || ti.ScopeExpr != "" || ti.View != "trace_index_mv" {
		t.Fatalf("trace_index target wrong: %+v", ti)
	}
	r, ok := TargetByName("agent_version_stats_daily")
	if !ok || r.ScopeExpr != "toYYYYMM(start_time)" || r.View != "agent_version_stats_daily_mv" {
		t.Fatalf("rollup target wrong: %+v", r)
	}
	if _, ok := TargetByName("spans"); ok {
		t.Fatal("spans is the source, never a rebuild target")
	}
	if !(Result{SpanCount: 5, SpansFinal: 5}).Exact() || (Result{SpanCount: 6, SpansFinal: 5}).Exact() {
		t.Fatal("Exact must compare span_count with spans FINAL")
	}
}
