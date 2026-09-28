package loadgen

import (
	"math/rand"
	"time"
)

// Plan decides which template each replay sequence number uses and how sequences are
// grouped into requests. Sequence seq replays template order[seq % len(order)], where order
// is a seeded shuffle of the template indices — traces from all agents interleave, and the
// same (seed, templates) always produce the same stream.
type Plan struct {
	order      []int
	spans      []int // spans per template
	bytes      []int // approximate encoded bytes per template instance
	maxSpans   int
	maxBytes   int
	nextSeq    uint64
	spansSoFar int64
}

// NewPlan builds a plan over templates with the given span and byte sizes. A request holds
// whole traces up to maxSpans (like an SDK batch processor's max export batch) and maxBytes
// (kept under the receiver's request limit); a single trace over either cap is sent alone.
func NewPlan(spans, bytes []int, maxSpans, maxBytes int, seed int64) *Plan {
	order := rand.New(rand.NewSource(seed)).Perm(len(spans))
	return &Plan{order: order, spans: spans, bytes: bytes, maxSpans: maxSpans, maxBytes: maxBytes}
}

// TemplateFor returns the template index replay seq uses.
func (p *Plan) TemplateFor(seq uint64) int { return p.order[seq%uint64(len(p.order))] }

// Request is one planned export: consecutive replay sequences [FirstSeq, FirstSeq+Traces).
type Request struct {
	FirstSeq uint64
	Traces   int
	Spans    int
	Bytes    int
	Intended time.Time // open-loop scheduled send time
}

// Next returns the next request, scheduled so the cumulative span count follows rate
// (spans/sec) from start: a request is due when the spans before it have "arrived".
// Scheduling from the plan — never from when the previous send finished — keeps the load
// open-loop, so a slow receiver cannot hide latency by slowing the generator down.
func (p *Plan) Next(start time.Time, rate float64) Request {
	r := Request{FirstSeq: p.nextSeq}
	r.Intended = start.Add(time.Duration(float64(p.spansSoFar) / rate * float64(time.Second)))
	for {
		t := p.TemplateFor(p.nextSeq)
		if r.Traces > 0 && (r.Spans+p.spans[t] > p.maxSpans || r.Bytes+p.bytes[t] > p.maxBytes) {
			break
		}
		r.Traces++
		r.Spans += p.spans[t]
		r.Bytes += p.bytes[t]
		p.nextSeq++
	}
	p.spansSoFar += int64(r.Spans)
	return r
}
