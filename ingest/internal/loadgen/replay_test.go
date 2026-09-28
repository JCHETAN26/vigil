package loadgen

import (
	"bytes"
	"encoding/hex"
	"testing"
	"time"

	tracepb "go.opentelemetry.io/proto/otlp/trace/v1"
	"google.golang.org/protobuf/proto"

	"github.com/JCHETAN26/vigil/ingest/internal/chsink"
	"github.com/JCHETAN26/vigil/ingest/internal/normalize"
)

// recordedTrace is a 4-span eval trace: root → {llm (with an event), tool}, plus a span
// whose parent is outside the trace (an orphan, as happens with cross-process context).
func recordedTrace(t *testing.T) *tracepb.TracesData {
	t.Helper()
	root := baseRow(tidA, "1111111111111111", "")
	llm := baseRow(tidA, "2222222222222222", "1111111111111111")
	llm.StartTime, llm.EndTime = t0.Add(100*time.Millisecond), t0.Add(900*time.Millisecond)
	llm.EventsTimestamp, llm.EventsName, llm.EventsAttributes = []time.Time{t0.Add(200 * time.Millisecond)}, []string{"e"}, []string{`{}`}
	tool := baseRow(tidA, "3333333333333333", "1111111111111111")
	tool.StartTime, tool.EndTime = t0.Add(1000*time.Millisecond), t0.Add(3000*time.Millisecond) // latest end
	orphan := baseRow(tidA, "4444444444444444", "9999999999999999")
	td, err := Denormalize([]chsink.Row{root, llm, tool, orphan})
	if err != nil {
		t.Fatal(err)
	}
	return td
}

func allSpans(rss []*tracepb.ResourceSpans) []*tracepb.Span {
	var out []*tracepb.Span
	for _, rs := range rss {
		for _, ss := range rs.GetScopeSpans() {
			out = append(out, ss.GetSpans()...)
		}
	}
	return out
}

func TestPrepareDoesNotModifyRecordedTrace(t *testing.T) {
	td := recordedTrace(t)
	before := proto.Clone(td)
	tm := Prepare(td, "run-x")
	rss := tm.Instance("run-x", 7, uint64(time.Now().UnixNano()))
	StampSentAt(rss, 42)
	if !proto.Equal(before, td) {
		t.Fatal("Prepare/Instance mutated the recorded trace")
	}
}

func TestInstanceIDsAndStructure(t *testing.T) {
	tm := Prepare(recordedTrace(t), "run-x")
	if tm.Spans() != 4 {
		t.Fatalf("spans = %d", tm.Spans())
	}
	a := allSpans(tm.Instance("run-x", 1, uint64(t0.UnixNano())))
	b := allSpans(tm.Instance("run-x", 2, uint64(t0.UnixNano())))
	again := allSpans(tm.Instance("run-x", 1, uint64(t0.UnixNano())))

	if !bytes.Equal(a[0].GetTraceId(), TraceID("run-x", 1)) || bytes.Equal(a[0].GetTraceId(), b[0].GetTraceId()) {
		t.Fatal("trace ids must be TraceID(run, seq) and differ per seq")
	}
	if bytes.Equal(TraceID("run-x", 1), TraceID("run-y", 1)) {
		t.Fatal("trace ids must differ across runs")
	}
	seen := map[string]bool{}
	byOldID := map[string]*tracepb.Span{}
	for i, sp := range a {
		if seen[string(sp.GetSpanId())] || len(sp.GetSpanId()) != 8 {
			t.Fatalf("span id %x duplicated or malformed", sp.GetSpanId())
		}
		seen[string(sp.GetSpanId())] = true
		if !bytes.Equal(sp.GetSpanId(), again[i].GetSpanId()) {
			t.Fatal("span ids must be deterministic")
		}
		byOldID[[]string{"root", "llm", "tool", "orphan"}[i]] = sp
	}
	root, llm, tool, orphan := byOldID["root"], byOldID["llm"], byOldID["tool"], byOldID["orphan"]
	if len(root.GetParentSpanId()) != 0 {
		t.Fatal("root must stay a root")
	}
	if !bytes.Equal(llm.GetParentSpanId(), root.GetSpanId()) || !bytes.Equal(tool.GetParentSpanId(), root.GetSpanId()) {
		t.Fatal("parent links must follow the remapped root id")
	}
	if hex.EncodeToString(orphan.GetParentSpanId()) != "9999999999999999" {
		t.Fatal("an out-of-trace parent id must be kept as-is")
	}
}

func TestInstanceShiftsTimesPreservingOffsets(t *testing.T) {
	td := recordedTrace(t)
	tm := Prepare(td, "run-x")
	endAt := uint64(time.Date(2026, 9, 28, 9, 0, 0, 0, time.UTC).UnixNano())
	orig, inst := allSpans(td.GetResourceSpans()), allSpans(tm.Instance("run-x", 1, endAt))
	shift := endAt - uint64(t0.Add(3000*time.Millisecond).UnixNano()) // recorded latest end
	var maxEnd uint64
	for i := range orig {
		if inst[i].GetStartTimeUnixNano() != orig[i].GetStartTimeUnixNano()+shift ||
			inst[i].GetEndTimeUnixNano() != orig[i].GetEndTimeUnixNano()+shift {
			t.Fatalf("span %d not shifted uniformly", i)
		}
		for j, ev := range inst[i].GetEvents() {
			if ev.GetTimeUnixNano() != orig[i].GetEvents()[j].GetTimeUnixNano()+shift {
				t.Fatalf("event %d/%d not shifted", i, j)
			}
		}
		maxEnd = max(maxEnd, inst[i].GetEndTimeUnixNano())
	}
	if maxEnd != endAt {
		t.Fatalf("latest end = %d, want %d", maxEnd, endAt)
	}
}

func TestStampSentAtDoesNotLeakAcrossInstances(t *testing.T) {
	tm := Prepare(recordedTrace(t), "run-x")
	a := tm.Instance("run-x", 1, 1)
	b := tm.Instance("run-x", 2, 1)
	StampSentAt(a, 111)
	StampSentAt(b, 222)
	for _, pair := range []struct {
		rss  []*tracepb.ResourceSpans
		want string
	}{{a, "111"}, {b, "222"}} {
		for _, rs := range pair.rss {
			n := 0
			for _, kv := range rs.GetResource().GetAttributes() {
				if kv.GetKey() == KeySentAtNano {
					n++
					if kv.GetValue().GetStringValue() != pair.want {
						t.Fatalf("sent_at = %s, want %s", kv.GetValue().GetStringValue(), pair.want)
					}
				}
			}
			if n != 1 {
				t.Fatalf("sent_at stamped %d times", n)
			}
		}
	}
}

// The production normalizer must see replayed spans as load-test data: tagged identity,
// the load run's run_id, run_kind live (30-day TTL), and no eval linkage.
func TestReplayedSpansNormalizeAsLoadTestData(t *testing.T) {
	tm := Prepare(recordedTrace(t), "run-x")
	rss := tm.Instance("run-x", 5, uint64(time.Now().UnixNano()))
	StampSentAt(rss, 12345)
	rows, err := normalize.Normalize(&tracepb.TracesData{ResourceSpans: rss}, mustPrices(t))
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 4 {
		t.Fatalf("rows = %d", len(rows))
	}
	for _, r := range rows {
		if r.AgentID != LoadAgentID || r.ServiceName != LoadService || r.AgentVersion != LoadVersion {
			t.Fatalf("identity not tagged: %s/%s/%s", r.AgentID, r.ServiceName, r.AgentVersion)
		}
		if r.RunID != "run-x" || r.RunKind != "live" || r.EvalRunID != "" || r.EvalCaseID != "" || r.EvalTrial != -1 {
			t.Fatalf("run identity wrong: %+v", r)
		}
		if r.TraceID != TraceIDHex("run-x", 5) {
			t.Fatal("stored trace id must be TraceIDHex(run, seq)")
		}
		ra := r.ResourceAttributes
		if ra[KeyLoadRunID] != "run-x" || ra[KeySourceAgt] != "hotpotqa-agent" ||
			ra[KeySourceVer] != "v-abc123" || ra[KeySentAtNano] != "12345" {
			t.Fatalf("load tags missing: %v", ra)
		}
		if r.SpanAttributes["custom.flag"] != "true" {
			t.Fatal("recorded span attributes must be preserved")
		}
	}
}
