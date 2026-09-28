// Package loadgen replays real recorded traces against the OTLP receiver for load and
// fault-injection tests. Templates are rebuilt from stored `spans` rows (Denormalize, the
// exact inverse of normalize.Normalize — verified row-for-row at export time), rewritten
// per replay with fresh deterministic ids and current timestamps, tagged as load-test data,
// and sent at a controlled open-loop rate over OTLP gRPC or HTTP.
package loadgen

import (
	"bytes"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"
	"strconv"
	"strings"

	commonpb "go.opentelemetry.io/proto/otlp/common/v1"
	resourcepb "go.opentelemetry.io/proto/otlp/resource/v1"
	tracepb "go.opentelemetry.io/proto/otlp/trace/v1"

	"github.com/JCHETAN26/vigil/ingest/internal/chsink"
)

// Attribute keys the normalizer promotes to columns. Kept in sync with
// internal/normalize by the round-trip tests (Denormalize → Normalize must be identity).
const (
	keyServiceName    = "service.name"
	keyServiceVersion = "service.version"
	keyAgentID        = "vigil.agent.id"
	keyAgentVersion   = "vigil.agent.version"
	keyAgentGitSHA    = "vigil.agent.git_sha"

	keyRunID        = "vigil.run.id"
	keyRunKind      = "vigil.run.kind"
	keyEvalRunID    = "vigil.eval.run_id"
	keyEvalCaseID   = "vigil.eval.case_id"
	keyEvalTrial    = "vigil.eval.trial"
	keyRole         = "vigil.role"
	keyCacheHit     = "vigil.cache.hit"
	keySessionID    = "gen_ai.conversation.id"
	keyGenAISystem  = "gen_ai.system"
	keyGenAIOp      = "gen_ai.operation.name"
	keyReqModel     = "gen_ai.request.model"
	keyRespModel    = "gen_ai.response.model"
	keyInputTokens  = "gen_ai.usage.input_tokens"
	keyOutputTokens = "gen_ai.usage.output_tokens"
	keyCacheWrite   = "gen_ai.usage.cache_creation_input_tokens"
	keyCacheRead    = "gen_ai.usage.cache_read_input_tokens"
)

// Denormalize rebuilds one trace's OTLP TracesData from its stored rows. Spans sharing a
// resource identity (service, agent, and leftover resource attributes) share one
// ResourceSpans. All attributes are emitted as strings: the normalizer stringifies every
// attribute, so the stored row — which is what the pipeline produces — round-trips exactly.
func Denormalize(rows []chsink.Row) (*tracepb.TracesData, error) {
	td := &tracepb.TracesData{}
	byResource := map[string]*tracepb.ScopeSpans{}
	for i := range rows {
		r := &rows[i]
		sp, err := spanFromRow(r)
		if err != nil {
			return nil, fmt.Errorf("span %s/%s: %w", r.TraceID, r.SpanID, err)
		}
		key := resourceKey(r)
		ss := byResource[key]
		if ss == nil {
			ss = &tracepb.ScopeSpans{}
			td.ResourceSpans = append(td.ResourceSpans, &tracepb.ResourceSpans{
				Resource:   &resourcepb.Resource{Attributes: resourceAttrs(r)},
				ScopeSpans: []*tracepb.ScopeSpans{ss},
			})
			byResource[key] = ss
		}
		ss.Spans = append(ss.Spans, sp)
	}
	return td, nil
}

func resourceAttrs(r *chsink.Row) []*commonpb.KeyValue {
	m := copyMap(r.ResourceAttributes)
	setNonEmpty(m, keyServiceName, r.ServiceName)
	setNonEmpty(m, keyServiceVersion, r.ServiceVersion)
	setNonEmpty(m, keyAgentID, r.AgentID)
	setNonEmpty(m, keyAgentVersion, r.AgentVersion)
	setNonEmpty(m, keyAgentGitSHA, r.GitSHA)
	return stringKVs(m)
}

// resourceKey identifies a row's resource: the promoted resource columns plus the
// leftover resource attributes, in a canonical order.
func resourceKey(r *chsink.Row) string {
	var b strings.Builder
	for _, kv := range resourceAttrs(r) {
		b.WriteString(kv.GetKey())
		b.WriteByte(0)
		b.WriteString(kv.GetValue().GetStringValue())
		b.WriteByte(0)
	}
	return b.String()
}

func spanFromRow(r *chsink.Row) (*tracepb.Span, error) {
	traceID, err := hex.DecodeString(r.TraceID)
	if err != nil {
		return nil, fmt.Errorf("trace id: %w", err)
	}
	spanID, err := hex.DecodeString(r.SpanID)
	if err != nil {
		return nil, fmt.Errorf("span id: %w", err)
	}
	parentID, err := hex.DecodeString(r.ParentSpanID)
	if err != nil {
		return nil, fmt.Errorf("parent span id: %w", err)
	}

	m := copyMap(r.SpanAttributes)
	setNonEmpty(m, keyRunID, r.RunID)
	if r.RunKind == "live" || r.RunKind == "eval" {
		m[keyRunKind] = r.RunKind
	}
	setNonEmpty(m, keyEvalRunID, r.EvalRunID)
	setNonEmpty(m, keyEvalCaseID, r.EvalCaseID)
	if r.EvalTrial >= 0 {
		m[keyEvalTrial] = strconv.FormatInt(int64(r.EvalTrial), 10)
	}
	setNonEmpty(m, keyRole, r.Role)
	if r.CacheHit != 0 {
		m[keyCacheHit] = "true"
	}
	setNonEmpty(m, keySessionID, r.SessionID)
	setNonEmpty(m, keyGenAISystem, r.GenAISystem)
	setNonEmpty(m, keyGenAIOp, r.GenAIOperationName)
	setNonEmpty(m, keyReqModel, r.GenAIRequestModel)
	setNonEmpty(m, keyRespModel, r.GenAIResponseModel)
	setNonZero(m, keyInputTokens, r.GenAIUsageInputTokens)
	setNonZero(m, keyOutputTokens, r.GenAIUsageOutputTokens)
	setNonZero(m, keyCacheWrite, r.GenAIUsageCacheCreationInputTokens)
	setNonZero(m, keyCacheRead, r.GenAIUsageCacheReadInputTokens)

	sp := &tracepb.Span{
		TraceId:           traceID,
		SpanId:            spanID,
		ParentSpanId:      parentID,
		TraceState:        r.TraceState,
		Name:              r.SpanName,
		Kind:              spanKind(r.SpanKind),
		StartTimeUnixNano: uint64(r.StartTime.UnixNano()),
		EndTimeUnixNano:   uint64(r.EndTime.UnixNano()),
		Attributes:        stringKVs(m),
	}
	if r.StatusCode != "UNSET" || r.StatusMessage != "" {
		sp.Status = &tracepb.Status{Code: statusCode(r.StatusCode), Message: r.StatusMessage}
	}
	if len(r.EventsName) != len(r.EventsTimestamp) || len(r.EventsName) != len(r.EventsAttributes) {
		return nil, fmt.Errorf("events arrays have mismatched lengths")
	}
	for i := range r.EventsName {
		attrs, err := kvsFromJSON(r.EventsAttributes[i])
		if err != nil {
			return nil, fmt.Errorf("event %d attributes: %w", i, err)
		}
		sp.Events = append(sp.Events, &tracepb.Span_Event{
			TimeUnixNano: uint64(r.EventsTimestamp[i].UnixNano()),
			Name:         r.EventsName[i],
			Attributes:   attrs,
		})
	}
	return sp, nil
}

// kvsFromJSON parses an events.attributes JSON object back into typed OTLP key-values such
// that the normalizer's attrsToJSON re-encodes it to the identical string: integers become
// IntValue, other numbers DoubleValue, arrays ArrayValue, and objects KvlistValue.
func kvsFromJSON(s string) ([]*commonpb.KeyValue, error) {
	dec := json.NewDecoder(bytes.NewReader([]byte(s)))
	dec.UseNumber()
	var m map[string]any
	if err := dec.Decode(&m); err != nil {
		return nil, err
	}
	keys := sortedKeys(m)
	out := make([]*commonpb.KeyValue, 0, len(keys))
	for _, k := range keys {
		out = append(out, &commonpb.KeyValue{Key: k, Value: anyValue(m[k])})
	}
	return out, nil
}

func anyValue(v any) *commonpb.AnyValue {
	switch x := v.(type) {
	case string:
		return &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: x}}
	case bool:
		return &commonpb.AnyValue{Value: &commonpb.AnyValue_BoolValue{BoolValue: x}}
	case json.Number:
		if n, err := x.Int64(); err == nil {
			return &commonpb.AnyValue{Value: &commonpb.AnyValue_IntValue{IntValue: n}}
		}
		f, _ := x.Float64()
		return &commonpb.AnyValue{Value: &commonpb.AnyValue_DoubleValue{DoubleValue: f}}
	case []any:
		arr := &commonpb.ArrayValue{}
		for _, e := range x {
			arr.Values = append(arr.Values, anyValue(e))
		}
		return &commonpb.AnyValue{Value: &commonpb.AnyValue_ArrayValue{ArrayValue: arr}}
	case map[string]any:
		kv := &commonpb.KeyValueList{}
		for _, k := range sortedKeys(x) {
			kv.Values = append(kv.Values, &commonpb.KeyValue{Key: k, Value: anyValue(x[k])})
		}
		return &commonpb.AnyValue{Value: &commonpb.AnyValue_KvlistValue{KvlistValue: kv}}
	default: // JSON null
		return &commonpb.AnyValue{}
	}
}

func spanKind(name string) tracepb.Span_SpanKind {
	switch name {
	case "INTERNAL":
		return tracepb.Span_SPAN_KIND_INTERNAL
	case "SERVER":
		return tracepb.Span_SPAN_KIND_SERVER
	case "CLIENT":
		return tracepb.Span_SPAN_KIND_CLIENT
	case "PRODUCER":
		return tracepb.Span_SPAN_KIND_PRODUCER
	case "CONSUMER":
		return tracepb.Span_SPAN_KIND_CONSUMER
	default:
		return tracepb.Span_SPAN_KIND_UNSPECIFIED
	}
}

func statusCode(name string) tracepb.Status_StatusCode {
	switch name {
	case "OK":
		return tracepb.Status_STATUS_CODE_OK
	case "ERROR":
		return tracepb.Status_STATUS_CODE_ERROR
	default:
		return tracepb.Status_STATUS_CODE_UNSET
	}
}

func copyMap(m map[string]string) map[string]string {
	out := make(map[string]string, len(m)+8)
	for k, v := range m {
		out[k] = v
	}
	return out
}

func setNonEmpty(m map[string]string, k, v string) {
	if v != "" {
		m[k] = v
	}
}

func setNonZero(m map[string]string, k string, v uint32) {
	if v != 0 {
		m[k] = strconv.FormatUint(uint64(v), 10)
	}
}

// stringKVs renders a map as string-valued key-values in key order (deterministic output).
func stringKVs(m map[string]string) []*commonpb.KeyValue {
	out := make([]*commonpb.KeyValue, 0, len(m))
	for _, k := range sortedKeys(m) {
		out = append(out, strKV(k, m[k]))
	}
	return out
}

func strKV(k, v string) *commonpb.KeyValue {
	return &commonpb.KeyValue{Key: k, Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: v}}}
}

func sortedKeys[V any](m map[string]V) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}
