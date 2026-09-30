// Package rebuild re-derives the on-insert rollups (trace_index, agent_version_stats_daily)
// from the deduplicated raw spans (data-model §3.7). The rebuild query is taken from each
// materialized view's live definition (system.tables.as_select), so it can never drift from
// the schema the way a hand-copied recipe does; only its source changes, from `spans` to
// `spans FINAL` (merge-time dedup applied) plus an optional partition scope. The result is
// built in a shadow table and swapped in with ALTER TABLE ... REPLACE PARTITION, which is
// atomic and keeps the target (and the view's binding to it) in place.
//
// Run it with the writer paused: rows the view inserts into the target while a partition is
// being rebuilt would be replaced away by the swap.
package rebuild

import (
	"context"
	"fmt"
	"regexp"
	"strings"

	"github.com/ClickHouse/clickhouse-go/v2"
	"github.com/ClickHouse/clickhouse-go/v2/lib/driver"
)

// Target is a table fed by a materialized view on spans.
type Target struct {
	Table string // target table
	View  string // the materialized view feeding it
	// ScopeExpr maps a spans row to the target's partition id, for scoping a rebuild to
	// some partitions; "" for an unpartitioned target (rebuilt whole).
	ScopeExpr string
}

// Targets are the rollups derived from spans, in rebuild order.
var Targets = []Target{
	{Table: "trace_index", View: "trace_index_mv"},
	{Table: "agent_version_stats_daily", View: "agent_version_stats_daily_mv", ScopeExpr: "toYYYYMM(start_time)"},
}

// TargetByName finds a Target by its table name.
func TargetByName(name string) (Target, bool) {
	for _, t := range Targets {
		if t.Table == name {
			return t, true
		}
	}
	return Target{}, false
}

// RewriteSelect turns a view's SELECT (as ClickHouse stores it in system.tables.as_select)
// into the rebuild SELECT: its single `FROM <db>.spans` becomes `FROM <db>.spans FINAL`,
// followed by `WHERE <where>` when where is non-empty. It refuses anything it cannot rewrite
// safely (no or several reads of spans, or an existing WHERE the scope would conflict with).
func RewriteSelect(asSelect, db, where string) (string, error) {
	from := regexp.MustCompile(`\bFROM ` + regexp.QuoteMeta(db) + `\.spans\b`)
	locs := from.FindAllStringIndex(asSelect, -1)
	if len(locs) != 1 {
		return "", fmt.Errorf("expected exactly one `FROM %s.spans` in the view query, found %d", db, len(locs))
	}
	if regexp.MustCompile(`(?i)\bWHERE\b|\bFINAL\b`).MatchString(asSelect) {
		return "", fmt.Errorf("view query already has WHERE/FINAL; refusing to rewrite it")
	}
	repl := "FROM " + db + ".spans FINAL"
	if where != "" {
		repl += " WHERE " + where
	}
	return asSelect[:locs[0][0]] + repl + asSelect[locs[0][1]:], nil
}

// PartitionLiteral renders a partition id for ALTER ... PARTITION: tuple() for an
// unpartitioned table, the numeric id (e.g. 202609) otherwise.
func PartitionLiteral(id string) string {
	if id == "" || id == "all" {
		return "tuple()"
	}
	return id
}

// Settings keep the rebuild within a small ClickHouse memory cap (1.5 GiB as-is): few
// threads, and aggregation / sort state spilled to disk past 256 MiB.
var Settings = clickhouse.Settings{
	"max_threads":                        2,
	"max_bytes_before_external_group_by": 256 << 20,
	"max_bytes_before_external_sort":     256 << 20,
}

// Result reports one rebuilt partition and the check that it now matches spans FINAL.
type Result struct {
	Table      string `json:"table"`
	Partition  string `json:"partition"`  // "" for an unpartitioned target
	SpanCount  uint64 `json:"span_count"` // sum(span_count) in the target after the rebuild
	SpansFinal uint64 `json:"spans_final"`
}

// Exact reports whether the rebuilt partition counts every deduplicated span exactly once.
func (r Result) Exact() bool { return r.SpanCount == r.SpansFinal }

// Run rebuilds target in db. partitions scopes a partitioned target (e.g. "202609"); empty
// means every partition present in spans or in the target. An unpartitioned target is
// always rebuilt whole.
func Run(ctx context.Context, conn driver.Conn, db string, target Target, partitions []string) ([]Result, error) {
	ctx = clickhouse.Context(ctx, clickhouse.WithSettings(Settings))
	var asSelect string
	if err := conn.QueryRow(ctx, "SELECT as_select FROM system.tables WHERE database = ? AND name = ?",
		db, target.View).Scan(&asSelect); err != nil {
		return nil, fmt.Errorf("read %s.%s definition: %w", db, target.View, err)
	}
	if target.ScopeExpr == "" {
		partitions = []string{""}
	} else if len(partitions) == 0 {
		var err error
		if partitions, err = presentPartitions(ctx, conn, db, target); err != nil {
			return nil, err
		}
	}

	shadow := fmt.Sprintf("%s.%s__rebuild", db, target.Table)
	full := fmt.Sprintf("%s.%s", db, target.Table)
	var out []Result
	for _, p := range partitions {
		where := ""
		if p != "" {
			where = fmt.Sprintf("%s = %s", target.ScopeExpr, p)
		}
		sel, err := RewriteSelect(asSelect, db, where)
		if err != nil {
			return out, err
		}
		for _, stmt := range []string{
			"DROP TABLE IF EXISTS " + shadow,
			fmt.Sprintf("CREATE TABLE %s AS %s", shadow, full),
			fmt.Sprintf("INSERT INTO %s %s", shadow, sel),
			fmt.Sprintf("ALTER TABLE %s REPLACE PARTITION %s FROM %s", full, PartitionLiteral(p), shadow),
			"DROP TABLE " + shadow,
		} {
			if err := conn.Exec(ctx, stmt); err != nil {
				return out, fmt.Errorf("rebuild %s partition %q: %s: %w", target.Table, p, firstWords(stmt), err)
			}
		}
		r, err := verify(ctx, conn, db, target, p)
		if err != nil {
			return out, err
		}
		out = append(out, r)
	}
	return out, nil
}

// presentPartitions lists the partitions of a partitioned target that exist in spans or in
// the target itself (so a partition whose spans are gone is emptied, not left stale).
func presentPartitions(ctx context.Context, conn driver.Conn, db string, target Target) ([]string, error) {
	rows, err := conn.Query(ctx, fmt.Sprintf(`
SELECT DISTINCT toString(%s) FROM %s.spans
UNION DISTINCT
SELECT DISTINCT partition_id FROM system.parts WHERE database = ? AND table = ? AND active`,
		target.ScopeExpr, db), db, target.Table)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var p string
		if err := rows.Scan(&p); err != nil {
			return nil, err
		}
		out = append(out, p)
	}
	return out, rows.Err()
}

func verify(ctx context.Context, conn driver.Conn, db string, target Target, p string) (Result, error) {
	r := Result{Table: target.Table, Partition: p}
	tw, sw := "", ""
	if p != "" {
		tw = fmt.Sprintf(" WHERE _partition_id = '%s'", p)
		sw = fmt.Sprintf(" WHERE %s = %s", target.ScopeExpr, p)
	}
	if err := conn.QueryRow(ctx, fmt.Sprintf("SELECT toUInt64(sum(span_count)) FROM %s.%s%s", db, target.Table, tw)).
		Scan(&r.SpanCount); err != nil {
		return r, fmt.Errorf("verify %s: %w", target.Table, err)
	}
	if err := conn.QueryRow(ctx, fmt.Sprintf("SELECT count() FROM %s.spans FINAL%s", db, sw)).
		Scan(&r.SpansFinal); err != nil {
		return r, fmt.Errorf("verify spans: %w", err)
	}
	return r, nil
}

func firstWords(s string) string {
	f := strings.Fields(s)
	if len(f) > 4 {
		f = f[:4]
	}
	return strings.Join(f, " ")
}
