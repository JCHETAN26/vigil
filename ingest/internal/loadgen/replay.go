package loadgen

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"strconv"

	commonpb "go.opentelemetry.io/proto/otlp/common/v1"
	resourcepb "go.opentelemetry.io/proto/otlp/resource/v1"
	tracepb "go.opentelemetry.io/proto/otlp/trace/v1"
)

// Load-test identity. Every replayed span carries these, so load data is unmistakable
// even outside its dedicated database (vigil_load) and topic.
const (
	LoadAgentID   = "vigil-loadtest"
	LoadService   = "vigil-loadtest"
	LoadVersion   = "loadtest"
	KeyLoadRunID  = "vigil.loadtest.run_id"
	KeySourceAgt  = "vigil.loadtest.source_agent"
	KeySourceVer  = "vigil.loadtest.source_version"
	KeySourceSvc  = "vigil.loadtest.source_service"
	KeySentAtNano = "vigil.loadtest.sent_at_ns" // resource attr, stamped at first send
)

// Template is one recorded trace prepared for replay under a given load run: tags are
// applied once, and per-span structure (index, parent index) is precomputed so each
// Instance only allocates new ids, timestamps, and wrappers. Attribute and event payloads
// are shared read-only across instances.
type Template struct {
	resources []templateResource
	spans     int
	maxEnd    uint64 // latest span end time in the recorded trace
}

type templateResource struct {
	attrs []*commonpb.KeyValue // tagged resource attributes (len == cap)
	spans []templateSpan
}

type templateSpan struct {
	src    *tracepb.Span        // recorded span (ids/times ignored; payload shared)
	attrs  []*commonpb.KeyValue // tagged span attributes
	index  int                  // position within the trace, for the derived span id
	parent int                  // index of the parent span, or -1 for a root
	orphan []byte               // original parent id when it is not in the trace
}

// Spans returns the number of spans one instance of the template produces.
func (t *Template) Spans() int { return t.spans }

// Prepare tags a recorded trace for the load run runID: the resource becomes
// agent/service "vigil-loadtest" (originals kept under vigil.loadtest.source_*), spans are
// re-homed to run_id=runID with run_kind=live, and eval linkage (eval run/case/trial) is
// removed so load data can never join to real eval results. td is not modified.
func Prepare(td *tracepb.TracesData, runID string) *Template {
	t := &Template{}
	index := map[string]int{}
	for _, rs := range td.GetResourceSpans() {
		for _, ss := range rs.GetScopeSpans() {
			for _, sp := range ss.GetSpans() {
				index[string(sp.GetSpanId())] = len(index)
			}
		}
	}
	for _, rs := range td.GetResourceSpans() {
		tr := templateResource{attrs: tagResource(rs.GetResource().GetAttributes(), runID)}
		for _, ss := range rs.GetScopeSpans() {
			for _, sp := range ss.GetSpans() {
				ts := templateSpan{src: sp, attrs: tagSpan(sp.GetAttributes(), runID),
					index: index[string(sp.GetSpanId())], parent: -1}
				if p := sp.GetParentSpanId(); len(p) > 0 {
					if pi, ok := index[string(p)]; ok {
						ts.parent = pi
					} else {
						ts.orphan = p
					}
				}
				if sp.GetEndTimeUnixNano() > t.maxEnd {
					t.maxEnd = sp.GetEndTimeUnixNano()
				}
				tr.spans = append(tr.spans, ts)
				t.spans++
			}
		}
		t.resources = append(t.resources, tr)
	}
	return t
}

func tagResource(in []*commonpb.KeyValue, runID string) []*commonpb.KeyValue {
	m := map[string]string{}
	for _, kv := range in {
		m[kv.GetKey()] = kv.GetValue().GetStringValue()
	}
	m[KeySourceAgt] = m[keyAgentID]
	m[KeySourceVer] = m[keyAgentVersion]
	m[KeySourceSvc] = m[keyServiceName]
	m[keyAgentID] = LoadAgentID
	m[keyAgentVersion] = LoadVersion
	m[keyServiceName] = LoadService
	m[KeyLoadRunID] = runID
	kvs := stringKVs(m)
	return kvs[:len(kvs):len(kvs)]
}

func tagSpan(in []*commonpb.KeyValue, runID string) []*commonpb.KeyValue {
	out := make([]*commonpb.KeyValue, 0, len(in)+2)
	for _, kv := range in {
		switch kv.GetKey() {
		case keyRunID, keyRunKind, keyEvalRunID, keyEvalCaseID, keyEvalTrial:
			continue
		}
		out = append(out, kv)
	}
	out = append(out, strKV(keyRunID, runID), strKV(keyRunKind, "live"))
	return out[:len(out):len(out)]
}

// TraceID derives the replayed trace id for sequence number seq of a load run: the first
// 16 bytes of sha256(runID, seq). Deterministic, so reconciliation can recompute the
// expected ids from the request log; random-looking, so ClickHouse sees realistic ids.
func TraceID(runID string, seq uint64) []byte {
	h := sha256.New()
	h.Write([]byte(runID))
	var b [8]byte
	binary.BigEndian.PutUint64(b[:], seq)
	h.Write(b[:])
	return h.Sum(nil)[:16]
}

// TraceIDHex is TraceID in the lowercase hex form stored in ClickHouse.
func TraceIDHex(runID string, seq uint64) string { return hex.EncodeToString(TraceID(runID, seq)) }

func spanID(traceID []byte, index int) []byte {
	h := sha256.New()
	h.Write(traceID)
	var b [8]byte
	binary.BigEndian.PutUint64(b[:], uint64(index))
	h.Write(b[:])
	return h.Sum(nil)[:8]
}

// Instance materializes replay seq of the template, shifted so the trace's last span ends
// at endAtNano (a real exporter sends a trace after its spans finish). Span ids are derived
// from the new trace id and each span's position, preserving the parent/child structure.
func (t *Template) Instance(runID string, seq uint64, endAtNano uint64) []*tracepb.ResourceSpans {
	tid := TraceID(runID, seq)
	ids := make([][]byte, t.spans)
	for _, r := range t.resources {
		for _, s := range r.spans {
			ids[s.index] = spanID(tid, s.index)
		}
	}
	shift := func(ts uint64) uint64 { return ts + endAtNano - t.maxEnd }

	out := make([]*tracepb.ResourceSpans, 0, len(t.resources))
	for _, r := range t.resources {
		attrs := make([]*commonpb.KeyValue, len(r.attrs), len(r.attrs)+1) // room for sent_at
		copy(attrs, r.attrs)
		ss := &tracepb.ScopeSpans{Spans: make([]*tracepb.Span, 0, len(r.spans))}
		for _, s := range r.spans {
			parent := s.orphan
			if s.parent >= 0 {
				parent = ids[s.parent]
			}
			sp := &tracepb.Span{
				TraceId:           tid,
				SpanId:            ids[s.index],
				ParentSpanId:      parent,
				TraceState:        s.src.GetTraceState(),
				Name:              s.src.GetName(),
				Kind:              s.src.GetKind(),
				StartTimeUnixNano: shift(s.src.GetStartTimeUnixNano()),
				EndTimeUnixNano:   shift(s.src.GetEndTimeUnixNano()),
				Attributes:        s.attrs,
				Status:            s.src.GetStatus(),
			}
			for _, ev := range s.src.GetEvents() {
				sp.Events = append(sp.Events, &tracepb.Span_Event{
					TimeUnixNano: shift(ev.GetTimeUnixNano()),
					Name:         ev.GetName(),
					Attributes:   ev.GetAttributes(),
				})
			}
			ss.Spans = append(ss.Spans, sp)
		}
		out = append(out, &tracepb.ResourceSpans{
			Resource:   &resourcepb.Resource{Attributes: attrs},
			ScopeSpans: []*tracepb.ScopeSpans{ss},
		})
	}
	return out
}

// StampSentAt records the first-send wall clock on every resource in a request, for the
// end-to-end latency measurement (ClickHouse ingested_at minus this).
func StampSentAt(rss []*tracepb.ResourceSpans, sentAtNano int64) {
	kv := strKV(KeySentAtNano, strconv.FormatInt(sentAtNano, 10))
	for _, rs := range rss {
		rs.Resource.Attributes = append(rs.Resource.Attributes, kv)
	}
}
