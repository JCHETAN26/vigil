//go:build integration

package integration

import (
	"context"
	"fmt"
	"testing"
	"time"

	"github.com/ClickHouse/clickhouse-go/v2"
	"github.com/ClickHouse/clickhouse-go/v2/lib/driver"

	"github.com/JCHETAN26/vigil/ingest/internal/chsink"
	"github.com/JCHETAN26/vigil/ingest/internal/config"
	"github.com/JCHETAN26/vigil/ingest/internal/rebuild"
)

// isolatedDB creates a throwaway database with the full ingest schema, so a test can inject
// faults (constraints that make a view's insert fail) without touching the shared database.
// It returns the database name, a connection defaulting to it, and a sink writing to it.
func isolatedDB(t *testing.T) (string, driver.Conn, *chsink.Sink) {
	t.Helper()
	admin := mustCH(t)
	db := "vigil_itest_" + uniqueSuffix()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := admin.Exec(ctx, "CREATE DATABASE "+db); err != nil {
		t.Fatalf("create database: %v", err)
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		_ = admin.Exec(ctx, "DROP DATABASE IF EXISTS "+db+" SYNC")
	})
	cfg := config.ClickHouseFromEnv()
	cfg.Database = db
	conn, err := chOpen(cfg)
	if err != nil {
		t.Fatalf("open %s: %v", db, err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	if err := applySchema(conn); err != nil {
		t.Fatalf("apply schema to %s: %v", db, err)
	}
	sink, err := chsink.Open(ctx, cfg)
	if err != nil {
		t.Fatalf("open sink: %v", err)
	}
	t.Cleanup(func() { _ = sink.Close() })
	return db, conn, sink
}

func chOpen(cfg config.ClickHouse) (driver.Conn, error) {
	conn, err := clickhouse.Open(&clickhouse.Options{
		Addr: []string{cfg.Addr},
		Auth: clickhouse.Auth{Database: cfg.Database, Username: cfg.Username, Password: cfg.Password},
	})
	if err != nil {
		return nil, err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	return conn, conn.Ping(ctx)
}

// failInserts makes every insert into table fail (a CHECK constraint no row satisfies);
// the returned func removes the fault.
func failInserts(t *testing.T, conn driver.Conn, table string) func() {
	t.Helper()
	exec(t, conn, fmt.Sprintf("ALTER TABLE %s ADD CONSTRAINT fault_inject CHECK 0 = 1", table))
	return func() { exec(t, conn, fmt.Sprintf("ALTER TABLE %s DROP CONSTRAINT fault_inject", table)) }
}

func exec(t *testing.T, conn driver.Conn, stmt string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := conn.Exec(ctx, stmt); err != nil {
		t.Fatalf("%s: %v", stmt, err)
	}
}

// counts is the state of the three tables an insert into spans writes.
type counts struct {
	spansRows, spansUnique, traceIndex, rollup, badTraces uint64
}

func tableCounts(t *testing.T, conn driver.Conn) counts {
	t.Helper()
	return counts{
		spansRows:   queryU64(t, conn, "SELECT count() FROM spans"),
		spansUnique: queryU64(t, conn, "SELECT uniqExact(trace_id, span_id) FROM spans"),
		traceIndex:  queryU64(t, conn, "SELECT toUInt64(sum(span_count)) FROM trace_index"),
		rollup:      queryU64(t, conn, "SELECT toUInt64(sum(span_count)) FROM agent_version_stats_daily"),
		// traces whose trace_index span_count differs from their spans
		badTraces: queryU64(t, conn, `SELECT count() FROM
			(SELECT trace_id, sum(span_count) c FROM trace_index GROUP BY trace_id) ti
			FULL OUTER JOIN (SELECT trace_id, count() n FROM spans FINAL GROUP BY trace_id) s USING trace_id
			WHERE ti.c != s.n`),
	}
}

func insertErr(conn *chsink.Sink, rows []chsink.Row, token string) error {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	return conn.Insert(ctx, rows, token)
}

// TestPartialViewFailureIdenticalRetry is the fault-injection test for the writer's
// identical-batch retry (stage-4 fix 2). One INSERT into spans is not atomic across its
// materialized views: if a view's push fails, the spans part and any earlier view's block
// are already committed, yet the INSERT reports failure — what the 6 h soak hit ~56 times
// on MEMORY_LIMIT_EXCEEDED. Retrying the IDENTICAL batch with the SAME dedup token must
// leave all three tables exact: the source block and the views that got it are
// deduplicated, and a view that never got it (dedup must not skip it) receives it once.
func TestPartialViewFailureIdenticalRetry(t *testing.T) {
	for _, failing := range []string{"agent_version_stats_daily", "trace_index"} {
		t.Run(failing+" push fails", func(t *testing.T) {
			_, conn, sink := isolatedDB(t)
			rows := makeSpanRows("partial-"+uniqueSuffix(), 40)
			n := uint64(len(rows))
			const token = "otlp.spans.raw:0:100:139"

			restore := failInserts(t, conn, failing)
			if err := insertErr(sink, rows, token); err == nil {
				t.Fatal("the injected view failure must fail the INSERT")
			}
			partial := tableCounts(t, conn)
			if partial.spansRows != n {
				t.Fatalf("spans part must already be committed on a view failure: %+v", partial)
			}
			if failedCount := map[string]uint64{"trace_index": partial.traceIndex,
				"agent_version_stats_daily": partial.rollup}[failing]; failedCount != 0 {
				t.Fatalf("the failing view's target must be empty after the failure: %+v", partial)
			}
			t.Logf("after the failed INSERT: %+v", partial)

			restore()
			if err := insertErr(sink, rows, token); err != nil {
				t.Fatalf("identical retry: %v", err)
			}
			got := tableCounts(t, conn)
			want := counts{spansRows: n, spansUnique: n, traceIndex: n, rollup: n, badTraces: 0}
			if got != want {
				t.Fatalf("after the identical retry:\n got  %+v\n want %+v", got, want)
			}
		})
	}
}

// TestReformedRetryDoubleCountsAndRebuildRepairs reproduces the soak's failure mode — the
// retry of a partially applied INSERT carries a DIFFERENT token (a time-flushed batch
// re-formed over a different offset range) — and shows the §3.7 rebuild repairs it.
func TestReformedRetryDoubleCountsAndRebuildRepairs(t *testing.T) {
	db, conn, sink := isolatedDB(t)
	rows := makeSpanRows("reformed-"+uniqueSuffix(), 40)
	n := uint64(len(rows))

	restore := failInserts(t, conn, "agent_version_stats_daily")
	if err := insertErr(sink, rows, "otlp.spans.raw:0:100:139"); err == nil {
		t.Fatal("the injected rollup failure must fail the INSERT")
	}
	restore()
	if err := insertErr(sink, rows, "otlp.spans.raw:0:100:152"); err != nil { // re-formed range
		t.Fatalf("re-formed retry: %v", err)
	}
	got := tableCounts(t, conn)
	if got.spansRows != 2*n || got.spansUnique != n || got.traceIndex != 2*n || got.rollup != n {
		t.Fatalf("a re-formed retry must duplicate spans rows and double trace_index only: %+v", got)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	for run := 1; run <= 2; run++ { // the rebuild is idempotent
		for _, target := range rebuild.Targets {
			results, err := rebuild.Run(ctx, conn, db, target, nil)
			if err != nil {
				t.Fatalf("rebuild %s (run %d): %v", target.Table, run, err)
			}
			for _, r := range results {
				if !r.Exact() || r.SpansFinal != n {
					t.Fatalf("rebuild %s (run %d) not exact: %+v", target.Table, run, r)
				}
			}
		}
		got := tableCounts(t, conn)
		if got.traceIndex != n || got.rollup != n || got.badTraces != 0 {
			t.Fatalf("after rebuild run %d: %+v", run, got)
		}
		if got.spansRows != 2*n {
			t.Fatalf("the rebuild must not touch spans itself: %+v", got)
		}
	}
	// The rebuild left no shadow table behind and the views still feed the targets.
	if left := queryU64(t, conn, "SELECT count() FROM system.tables WHERE database = ? AND name LIKE '%__rebuild'", db); left != 0 {
		t.Fatalf("%d shadow tables left behind", left)
	}
	more := makeSpanRows("after-rebuild-"+uniqueSuffix(), 5)
	if err := insertErr(sink, more, "otlp.spans.raw:0:200:204"); err != nil {
		t.Fatal(err)
	}
	if after := tableCounts(t, conn); after.traceIndex != n+5 || after.rollup != n+5 {
		t.Fatalf("views must keep feeding the rebuilt targets: %+v", after)
	}
}
