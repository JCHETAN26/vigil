package loadgen

// StoredTrace is what ClickHouse holds for one trace: total rows and distinct span ids.
// Rows are counted WITHOUT FINAL, so duplicates are visible before any background merge
// of the ReplacingMergeTree collapses them.
type StoredTrace struct {
	Rows   int64
	Unique int64
}

// Reconciliation compares what the generator sent with what landed in ClickHouse.
// "Acked" spans got a successful OTLP response: each must be stored exactly once.
// "Unacked" spans exhausted their retries or failed hard; they may or may not have been
// stored (a lost response is indistinguishable from a lost request), so they are reported
// separately rather than counted as loss.
type Reconciliation struct {
	AckedTraces       int   `json:"acked_traces"`
	AckedSpans        int64 `json:"acked_spans"`
	StoredAckedUnique int64 `json:"stored_acked_unique_spans"`
	MissingSpans      int64 `json:"missing_spans"`     // acked but not stored
	DuplicateRows     int64 `json:"duplicate_rows"`    // rows beyond one per (trace, span)
	UnackedSpans      int64 `json:"unacked_spans"`     // sent without a success response
	UnackedStored     int64 `json:"unacked_stored"`    // of those, how many landed anyway
	UnexpectedTraces  int   `json:"unexpected_traces"` // stored traces never sent in this run
	StoredRows        int64 `json:"stored_rows"`
}

// Reconcile computes the reconciliation. acked and unacked map trace id → expected spans.
func Reconcile(acked, unacked map[string]int, stored map[string]StoredTrace) Reconciliation {
	var r Reconciliation
	for tid, want := range acked {
		r.AckedTraces++
		r.AckedSpans += int64(want)
		got := stored[tid].Unique
		if got > int64(want) {
			got = int64(want) // extra span ids would be unexpected, not "found"
		}
		r.StoredAckedUnique += got
		r.MissingSpans += int64(want) - got
	}
	for tid, want := range unacked {
		r.UnackedSpans += int64(want)
		r.UnackedStored += min(stored[tid].Unique, int64(want))
	}
	for tid, s := range stored {
		r.StoredRows += s.Rows
		r.DuplicateRows += s.Rows - s.Unique
		if _, ok := acked[tid]; !ok {
			if _, ok := unacked[tid]; !ok {
				r.UnexpectedTraces++
			}
		}
	}
	return r
}

// Clean reports whether the run met the correctness bar: every acked span stored, no
// duplicate rows, and nothing stored that the run did not send.
func (r Reconciliation) Clean() bool {
	return r.MissingSpans == 0 && r.DuplicateRows == 0 && r.UnexpectedTraces == 0
}

// GroupLag is a consumer group's position on one topic, computed from committed offsets
// against each partition's log start/end — independent of group membership. (A group with
// no live member, e.g. while its consumer crash-loops, has no assignments, and
// assignment-based lag reports 0 even with a large backlog.)
type GroupLag struct {
	State   string `json:"state"`
	Members int    `json:"members"`
	Lag     int64  `json:"lag"`
}

// Drained reports whether the group is actively consuming (Stable, with a member) and has
// consumed everything.
func (g GroupLag) Drained() bool { return g.State == "Stable" && g.Members > 0 && g.Lag == 0 }

// PartitionOffsets is one partition's consumer position against its retained log. It shows
// whether retention has deleted records the consumer never read (a real loss even though
// every one was acknowledged to the client) and how much is still waiting.
type PartitionOffsets struct {
	Partition int32 `json:"partition"`
	Committed int64 `json:"committed"` // -1: the group has no commit for this partition
	LogStart  int64 `json:"log_start"`
	End       int64 `json:"end"`
}

// DeletedUnconsumed is the number of records retention removed before the group consumed
// them: log start above the committed offset. Without a commit it cannot be known (0).
func (p PartitionOffsets) DeletedUnconsumed() int64 {
	if p.Committed < 0 || p.LogStart <= p.Committed {
		return 0
	}
	return p.LogStart - p.Committed
}

// Pending is the number of retained records the group has yet to consume.
func (p PartitionOffsets) Pending() int64 {
	from := max(p.Committed, p.LogStart)
	return max(p.End-from, 0)
}

// ConsumedRetained is the number of already-consumed records still ahead of the pending
// ones in the log: the cushion retention deletes before it reaches unconsumed data.
func (p PartitionOffsets) ConsumedRetained() int64 {
	if p.Committed < 0 {
		return 0
	}
	return max(min(p.Committed, p.End)-p.LogStart, 0)
}

// TotalLag sums end - committed over every partition in end. A partition without a commit
// is lagging from its log start (the writer resets to the earliest offset).
func TotalLag(start, end, committed map[int32]int64) int64 {
	var lag int64
	for p, e := range end {
		c, ok := committed[p]
		if !ok || c < start[p] {
			c = start[p]
		}
		if e > c {
			lag += e - c
		}
	}
	return lag
}
