//go:build integration

package integration

import (
	"context"
	"testing"
	"time"

	"github.com/JCHETAN26/vigil/ingest/internal/chsink"
	"github.com/JCHETAN26/vigil/ingest/internal/config"
)

func mustSink(t *testing.T) *chsink.Sink {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	s, err := chsink.Open(ctx, config.ClickHouseFromEnv())
	if err != nil {
		t.Fatalf("open sink: %v", err)
	}
	t.Cleanup(func() { _ = s.Close() })
	return s
}

func insert(t *testing.T, s *chsink.Sink, rows []chsink.Row, token string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	if err := s.Insert(ctx, rows, token); err != nil {
		t.Fatalf("insert (token %s): %v", token, err)
	}
}

// TestDuplicateBatchIsIdempotent is the headline correctness test: re-sending the exact
// same batch (same insert_deduplication_token) must leave BOTH the spans table and the
// agent_version_stats_daily rollup unchanged — proving the dedup token stops the duplicate
// before the on-insert rollup MV fires.
func TestDuplicateBatchIsIdempotent(t *testing.T) {
	conn := mustCH(t)
	sink := mustSink(t)

	av := "itest-dedup-" + uniqueSuffix()
	rows := makeSpanRows(av, 3)
	token := av + ":0:0:2" // deterministic offset-range token for this batch

	insert(t, sink, rows, token)

	rawBefore := rawSpanCount(t, conn, av)
	rollupBefore := rollupSpanCount(t, conn, av)
	runsBefore := rollupRuns(t, conn, av)
	if rawBefore != 3 || rollupBefore != 3 || runsBefore != 3 {
		t.Fatalf("after first insert: raw=%d rollupSpans=%d runs=%d, want 3/3/3", rawBefore, rollupBefore, runsBefore)
	}

	// Re-send the identical batch with the identical token.
	insert(t, sink, rows, token)

	rawAfter := rawSpanCount(t, conn, av)
	rollupAfter := rollupSpanCount(t, conn, av)
	runsAfter := rollupRuns(t, conn, av)

	if rawAfter != rawBefore {
		t.Errorf("spans changed after duplicate insert: before=%d after=%d (want unchanged)", rawBefore, rawAfter)
	}
	if rollupAfter != rollupBefore {
		t.Errorf("rollup span_count changed after duplicate insert: before=%d after=%d (want unchanged)", rollupBefore, rollupAfter)
	}
	if runsAfter != runsBefore {
		t.Errorf("rollup runs changed after duplicate insert: before=%d after=%d (want unchanged)", runsBefore, runsAfter)
	}
}

// TestDifferentTokenDoubleCounts is the negative companion: the same rows sent under a
// DIFFERENT token are not recognized as a duplicate, so the rollup MV fires again and
// double-counts. This proves it is the token (not ReplacingMergeTree) protecting the
// rollup, and guards against a regression that drops either dedup setting.
func TestDifferentTokenDoubleCounts(t *testing.T) {
	conn := mustCH(t)
	sink := mustSink(t)

	av := "itest-diff-" + uniqueSuffix()
	rows := makeSpanRows(av, 3)

	insert(t, sink, rows, av+":0:0:2")
	if got := rollupSpanCount(t, conn, av); got != 3 {
		t.Fatalf("after first insert rollup span_count = %d, want 3", got)
	}

	// Same rows, different token → not deduped → rollup double-counts.
	insert(t, sink, rows, av+":0:100:102")

	if got := rollupSpanCount(t, conn, av); got != 6 {
		t.Errorf("rollup span_count = %d after different-token re-insert, want 6 (double-counted)", got)
	}
	// The raw table's logical content is still 3 distinct spans (ReplacingMergeTree keys
	// on (trace_id, span_id)); only the un-protected rollup inflated.
	if got := dedupedSpanCount(t, conn, av); got != 3 {
		t.Errorf("deduped span count = %d, want 3 (distinct (trace_id, span_id))", got)
	}
}
