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
