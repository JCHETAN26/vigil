package loadgen

import "testing"

func TestReconcile(t *testing.T) {
	tests := []struct {
		name    string
		acked   map[string]int
		unacked map[string]int
		stored  map[string]StoredTrace
		want    Reconciliation
		clean   bool
	}{
		{
			name:   "everything stored once",
			acked:  map[string]int{"a": 3, "b": 2},
			stored: map[string]StoredTrace{"a": {3, 3}, "b": {2, 2}},
			want:   Reconciliation{AckedTraces: 2, AckedSpans: 5, StoredAckedUnique: 5, StoredRows: 5},
			clean:  true,
		},
		{
			name:   "missing trace and missing span",
			acked:  map[string]int{"a": 3, "b": 2},
			stored: map[string]StoredTrace{"a": {2, 2}},
			want:   Reconciliation{AckedTraces: 2, AckedSpans: 5, StoredAckedUnique: 2, MissingSpans: 3, StoredRows: 2},
		},
		{
			name:   "duplicate rows",
			acked:  map[string]int{"a": 3},
			stored: map[string]StoredTrace{"a": {5, 3}},
			want:   Reconciliation{AckedTraces: 1, AckedSpans: 3, StoredAckedUnique: 3, DuplicateRows: 2, StoredRows: 5},
		},
		{
			name:    "unacked spans are reported, not counted as loss",
			acked:   map[string]int{"a": 1},
			unacked: map[string]int{"u1": 4, "u2": 2},
			stored:  map[string]StoredTrace{"a": {1, 1}, "u1": {4, 4}},
			want:    Reconciliation{AckedTraces: 1, AckedSpans: 1, StoredAckedUnique: 1, UnackedSpans: 6, UnackedStored: 4, StoredRows: 5},
			clean:   true,
		},
		{
			name:   "stored trace that was never sent",
			acked:  map[string]int{"a": 1},
			stored: map[string]StoredTrace{"a": {1, 1}, "zzz": {1, 1}},
			want:   Reconciliation{AckedTraces: 1, AckedSpans: 1, StoredAckedUnique: 1, UnexpectedTraces: 1, StoredRows: 2},
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := Reconcile(tc.acked, tc.unacked, tc.stored)
			if got != tc.want {
				t.Fatalf("got  %+v\nwant %+v", got, tc.want)
			}
			if got.Clean() != tc.clean {
				t.Fatalf("clean = %v, want %v", got.Clean(), tc.clean)
			}
		})
	}
}

func TestPartitionOffsets(t *testing.T) {
	tests := []struct {
		name                               string
		p                                  PartitionOffsets
		deleted, pending, consumedRetained int64
	}{
		{"backlog, cushion intact", PartitionOffsets{0, 930, 915, 1000}, 0, 70, 15},
		{"caught up", PartitionOffsets{0, 1000, 915, 1000}, 0, 0, 85},
		{"retention overtook the consumer", PartitionOffsets{0, 900, 915, 1000}, 15, 85, 0},
		{"no commit yet", PartitionOffsets{0, -1, 915, 1000}, 0, 85, 0},
		{"empty partition", PartitionOffsets{0, 10, 10, 10}, 0, 0, 0},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := tc.p.DeletedUnconsumed(); got != tc.deleted {
				t.Errorf("DeletedUnconsumed = %d, want %d", got, tc.deleted)
			}
			if got := tc.p.Pending(); got != tc.pending {
				t.Errorf("Pending = %d, want %d", got, tc.pending)
			}
			if got := tc.p.ConsumedRetained(); got != tc.consumedRetained {
				t.Errorf("ConsumedRetained = %d, want %d", got, tc.consumedRetained)
			}
		})
	}
}

func TestTotalLag(t *testing.T) {
	tests := []struct {
		name                  string
		start, end, committed map[int32]int64
		want                  int64
	}{
		{"caught up", map[int32]int64{0: 0, 1: 0}, map[int32]int64{0: 10, 1: 5}, map[int32]int64{0: 10, 1: 5}, 0},
		{"behind", map[int32]int64{0: 0, 1: 0}, map[int32]int64{0: 10, 1: 5}, map[int32]int64{0: 4, 1: 5}, 6},
		{"no commit counts from log start", map[int32]int64{0: 3}, map[int32]int64{0: 10}, map[int32]int64{}, 7},
		{"commit below retained start", map[int32]int64{0: 8}, map[int32]int64{0: 10}, map[int32]int64{0: 2}, 2},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := TotalLag(tc.start, tc.end, tc.committed); got != tc.want {
				t.Fatalf("got %d, want %d", got, tc.want)
			}
		})
	}
	if (GroupLag{State: "PreparingRebalance", Members: 2, Lag: 0}).Drained() {
		t.Fatal("a group that is not Stable must not count as drained")
	}
	if (GroupLag{State: "Stable", Members: 0, Lag: 0}).Drained() {
		t.Fatal("a group without members must not count as drained")
	}
	if !(GroupLag{State: "Stable", Members: 1, Lag: 0}).Drained() {
		t.Fatal("stable, member, zero lag is drained")
	}
}
