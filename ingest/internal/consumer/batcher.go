package consumer

import (
	"sort"
	"time"

	"github.com/twmb/franz-go/pkg/kgo"

	"github.com/JCHETAN26/vigil/ingest/internal/chsink"
)

// pendingRow is a span row plus the source coordinates used to order the batch
// deterministically before insert.
type pendingRow struct {
	row    chsink.Row
	offset int64
	spanID string
}

// batch accumulates the span rows for one contiguous per-partition offset range. The
// offset range spans every consumed record in the range (including ones routed to the
// DLQ that contribute no rows), so the committed offset and the dedup token cover the
// whole range with no gaps.
type batch struct {
	rows        []pendingRow
	started     bool
	firstOffset int64
	lastOffset  int64
	lastRecord  *kgo.Record
	byteSize    int
	createdAt   time.Time
}

// note records that a record at this offset belongs to the range, extending it. It is
// called for every consumed record, whether or not it produced rows.
func (b *batch) note(rec *kgo.Record) {
	if !b.started {
		b.started = true
		b.firstOffset = rec.Offset
		b.createdAt = time.Now()
	}
	b.lastOffset = rec.Offset
	b.lastRecord = rec
	b.byteSize += len(rec.Value)
}

// addRows appends the normalized rows from a record to the batch.
func (b *batch) addRows(rec *kgo.Record, rows []chsink.Row) {
	for _, r := range rows {
		b.rows = append(b.rows, pendingRow{row: r, offset: rec.Offset, spanID: r.SpanID})
	}
}

// shouldFlushSize reports whether the batch has hit the row or byte threshold.
func (b *batch) shouldFlushSize(maxRows, maxBytes int) bool {
	return b.started && (len(b.rows) >= maxRows || b.byteSize >= maxBytes)
}

// shouldFlushTime reports whether the batch has been open longer than the flush interval.
// It applies even to a batch that produced no rows (all records DLQ'd), so offsets still
// advance for such ranges.
func (b *batch) shouldFlushTime(interval time.Duration) bool {
	return b.started && time.Since(b.createdAt) >= interval
}

// sortedRows returns the batch's rows in a deterministic order (source offset, then span
// id). Stable ordering keeps ClickHouse wire-block splitting reproducible across replays,
// which is what makes the per-block dedup tokens match on redelivery.
func (b *batch) sortedRows() []chsink.Row {
	sort.SliceStable(b.rows, func(i, j int) bool {
		if b.rows[i].offset != b.rows[j].offset {
			return b.rows[i].offset < b.rows[j].offset
		}
		return b.rows[i].spanID < b.rows[j].spanID
	})
	out := make([]chsink.Row, len(b.rows))
	for i := range b.rows {
		out[i] = b.rows[i].row
	}
	return out
}
