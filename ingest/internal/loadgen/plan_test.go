package loadgen

import (
	"testing"
	"time"
)

func TestPlanGroupsWholeTracesUnderCaps(t *testing.T) {
	tests := []struct {
		name               string
		spans, bytes       []int
		maxSpans, maxBytes int
		wantMaxSpans       int
	}{
		{"span cap binds", []int{5, 7, 3, 9}, []int{10, 10, 10, 10}, 12, 1 << 20, 12},
		{"byte cap binds", []int{1, 1, 1, 1}, []int{400, 400, 400, 400}, 512, 1000, 2},
		{"oversized trace goes alone", []int{50, 2}, []int{10, 10}, 10, 1 << 20, 50},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			p := NewPlan(tc.spans, tc.bytes, tc.maxSpans, tc.maxBytes, 3)
			var seq uint64
			for i := 0; i < 50; i++ {
				r := p.Next(time.Unix(0, 0), 100)
				if r.FirstSeq != seq || r.Traces < 1 {
					t.Fatalf("request %d: first seq %d (want %d), traces %d", i, r.FirstSeq, seq, r.Traces)
				}
				sum := 0
				for s := r.FirstSeq; s < r.FirstSeq+uint64(r.Traces); s++ {
					sum += tc.spans[p.TemplateFor(s)]
				}
				if sum != r.Spans {
					t.Fatalf("request spans %d != sum of its traces %d", r.Spans, sum)
				}
				if r.Traces > 1 && (r.Spans > tc.maxSpans || r.Bytes > tc.maxBytes) {
					t.Fatalf("multi-trace request over cap: %+v", r)
				}
				if r.Spans > tc.wantMaxSpans {
					t.Fatalf("request spans %d > %d", r.Spans, tc.wantMaxSpans)
				}
				seq += uint64(r.Traces)
			}
		})
	}
}

func TestPlanScheduleFollowsRate(t *testing.T) {
	p := NewPlan([]int{10, 10}, []int{1, 1}, 20, 1<<20, 1) // every request = 20 spans
	start := time.Unix(1000, 0)
	for i := 0; i < 5; i++ {
		r := p.Next(start, 200) // 200 spans/s → one 20-span request every 100 ms
		if want := start.Add(time.Duration(i) * 100 * time.Millisecond); !r.Intended.Equal(want) {
			t.Fatalf("request %d intended %v, want %v", i, r.Intended, want)
		}
	}
}

func TestPlanOrderIsSeededPermutation(t *testing.T) {
	sizes := make([]int, 20)
	a, b, c := NewPlan(sizes, sizes, 1, 1, 7), NewPlan(sizes, sizes, 1, 1, 7), NewPlan(sizes, sizes, 1, 1, 8)
	seen, differs := map[int]bool{}, false
	for s := uint64(0); s < 20; s++ {
		seen[a.TemplateFor(s)] = true
		if a.TemplateFor(s) != b.TemplateFor(s) {
			t.Fatal("same seed must give the same order")
		}
		differs = differs || a.TemplateFor(s) != c.TemplateFor(s)
		if a.TemplateFor(s) != a.TemplateFor(s+20) {
			t.Fatal("order must cycle")
		}
	}
	if len(seen) != 20 || !differs {
		t.Fatalf("want a full permutation that depends on the seed (seen %d, differs %v)", len(seen), differs)
	}
}
