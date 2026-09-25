package consumer

import (
	"testing"
	"time"

	"github.com/twmb/franz-go/pkg/kgo"

	"github.com/JCHETAN26/vigil/ingest/internal/chsink"
)

func rec(partition int32, offset int64, valueLen int) *kgo.Record {
	return &kgo.Record{Partition: partition, Offset: offset, Value: make([]byte, valueLen)}
}

func TestBatchRangeAndFlushTriggers(t *testing.T) {
	var b batch
	b.note(rec(0, 10, 5))
	b.addRows(rec(0, 10, 5), []chsink.Row{{SpanID: "b"}, {SpanID: "a"}})
	b.note(rec(0, 11, 5)) // a record that produced no rows (e.g. DLQ'd) still extends range
	b.note(rec(0, 12, 5))
	b.addRows(rec(0, 12, 5), []chsink.Row{{SpanID: "c"}})

	if b.firstOffset != 10 || b.lastOffset != 12 {
		t.Errorf("range = [%d,%d], want [10,12]", b.firstOffset, b.lastOffset)
	}
	if b.lastRecord.Offset != 12 {
		t.Errorf("lastRecord.Offset = %d, want 12", b.lastRecord.Offset)
	}
	if b.byteSize != 15 {
		t.Errorf("byteSize = %d, want 15", b.byteSize)
	}

	// Size triggers.
	if b.shouldFlushSize(2, 1<<30) != true {
		t.Error("expected row-count flush (3 rows >= 2)")
	}
	if b.shouldFlushSize(1<<30, 10) != true {
		t.Error("expected byte flush (15 bytes >= 10)")
	}
	if b.shouldFlushSize(1<<30, 1<<30) != false {
		t.Error("did not expect flush below both thresholds")
	}
}

func TestBatchShouldFlushTime(t *testing.T) {
	var b batch
	if b.shouldFlushTime(time.Millisecond) {
		t.Error("empty batch should not time-flush")
	}
	b.note(rec(0, 0, 1)) // no rows, but started
	b.createdAt = time.Now().Add(-time.Second)
	if !b.shouldFlushTime(100 * time.Millisecond) {
		t.Error("started batch older than interval should time-flush (even with no rows)")
	}
}

func TestSortedRowsDeterministicOrder(t *testing.T) {
	var b batch
	// Add out of order across offsets and span ids.
	b.note(rec(0, 20, 1))
	b.addRows(rec(0, 20, 1), []chsink.Row{{SpanID: "z"}, {SpanID: "m"}})
	b.note(rec(0, 10, 1))
	b.addRows(rec(0, 10, 1), []chsink.Row{{SpanID: "b"}, {SpanID: "a"}})

	got := b.sortedRows()
	// Expect order by (source offset, then span id): offset 10 (a,b), then offset 20 (m,z).
	expect := []string{"a", "b", "m", "z"}
	if len(got) != len(expect) {
		t.Fatalf("got %d rows, want %d", len(got), len(expect))
	}
	for i := range expect {
		if got[i].SpanID != expect[i] {
			t.Errorf("row %d SpanID = %q, want %q", i, got[i].SpanID, expect[i])
		}
	}
}
