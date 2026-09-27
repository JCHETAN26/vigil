// Package normalize converts raw per-trace OTLP payloads into chsink.Row values: it
// promotes the known resource/span attributes (§2) to typed columns, resolves run_kind
// and the run_id default, computes cost from the pricing table, and leaves the remaining
// attributes in the map "tail". Unmarshal or structural failures return an error, which
// the writer routes to the DLQ.
package normalize

import (
	"encoding/hex"
	"encoding/json"
	"fmt"
	"strconv"
	"time"

	commonpb "go.opentelemetry.io/proto/otlp/common/v1"
	tracepb "go.opentelemetry.io/proto/otlp/trace/v1"
	"google.golang.org/protobuf/proto"

	"github.com/JCHETAN26/vigil/ingest/internal/chsink"
	"github.com/JCHETAN26/vigil/ingest/internal/pricing"
)

// Promoted attribute keys (removed from the map tail once lifted to a column).
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

// FromOTLP unmarshals a raw per-trace OTLP payload and normalizes it into span rows.
func FromOTLP(raw []byte, prices *pricing.Table) ([]chsink.Row, error) {
	var td tracepb.TracesData
	if err := proto.Unmarshal(raw, &td); err != nil {
		return nil, fmt.Errorf("unmarshal OTLP: %w", err)
	}
	return Normalize(&td, prices)
}

// Normalize converts a TracesData into span rows.
func Normalize(td *tracepb.TracesData, prices *pricing.Table) ([]chsink.Row, error) {
	var rows []chsink.Row
	for _, rs := range td.GetResourceSpans() {
		resAttrs := attrsToMap(rs.GetResource().GetAttributes())
		serviceName := take(resAttrs, keyServiceName)
		serviceVersion := take(resAttrs, keyServiceVersion)
		agentID := take(resAttrs, keyAgentID)
		agentVersion := take(resAttrs, keyAgentVersion)
		gitSHA := take(resAttrs, keyAgentGitSHA)

		for _, ss := range rs.GetScopeSpans() {
			for _, sp := range ss.GetSpans() {
				spanAttrs := attrsToMap(sp.GetAttributes())
				traceID := hex.EncodeToString(sp.GetTraceId())

				runID := take(spanAttrs, keyRunID)
				if runID == "" {
					runID = traceID // default run_id := trace_id (§2.2)
				}
				inTok := parseUint32(take(spanAttrs, keyInputTokens))
				outTok := parseUint32(take(spanAttrs, keyOutputTokens))
				cacheWriteTok := parseUint32(take(spanAttrs, keyCacheWrite))
				cacheReadTok := parseUint32(take(spanAttrs, keyCacheRead))
				reqModel := take(spanAttrs, keyReqModel)
				respModel := take(spanAttrs, keyRespModel)
				costModel := respModel
				if costModel == "" {
					costModel = reqModel
				}

				row := chsink.Row{
					TraceID:      traceID,
					SpanID:       hex.EncodeToString(sp.GetSpanId()),
					ParentSpanID: hex.EncodeToString(sp.GetParentSpanId()),
					TraceState:   sp.GetTraceState(),

					StartTime: time.Unix(0, int64(sp.GetStartTimeUnixNano())).UTC(),
					EndTime:   time.Unix(0, int64(sp.GetEndTimeUnixNano())).UTC(),

					SpanName:      sp.GetName(),
					SpanKind:      spanKindName(sp.GetKind()),
					StatusCode:    statusCodeName(sp.GetStatus().GetCode()),
					StatusMessage: sp.GetStatus().GetMessage(),

					ServiceName:    serviceName,
					ServiceVersion: serviceVersion,

					AgentID:      agentID,
					AgentVersion: agentVersion,
					GitSHA:       gitSHA,
					RunID:        runID,
					RunKind:      runKindName(take(spanAttrs, keyRunKind)),
					EvalRunID:    take(spanAttrs, keyEvalRunID),
					EvalCaseID:   take(spanAttrs, keyEvalCaseID),
					EvalTrial:    parseInt32(take(spanAttrs, keyEvalTrial), -1),
					Role:         take(spanAttrs, keyRole),
					CacheHit:     parseBool01(take(spanAttrs, keyCacheHit)),
					SessionID:    take(spanAttrs, keySessionID),

					GenAISystem:                        take(spanAttrs, keyGenAISystem),
					GenAIOperationName:                 take(spanAttrs, keyGenAIOp),
					GenAIRequestModel:                  reqModel,
					GenAIResponseModel:                 respModel,
					GenAIUsageInputTokens:              inTok,
					GenAIUsageOutputTokens:             outTok,
					GenAIUsageCacheCreationInputTokens: cacheWriteTok,
					GenAIUsageCacheReadInputTokens:     cacheReadTok,
					CostUSD:                            prices.CostWithCache(costModel, inTok, cacheWriteTok, cacheReadTok, outTok),

					ResourceAttributes: resAttrs,
					SpanAttributes:     spanAttrs,
				}

				for _, ev := range sp.GetEvents() {
					row.EventsTimestamp = append(row.EventsTimestamp, time.Unix(0, int64(ev.GetTimeUnixNano())).UTC())
					row.EventsName = append(row.EventsName, ev.GetName())
					row.EventsAttributes = append(row.EventsAttributes, attrsToJSON(ev.GetAttributes()))
				}

				rows = append(rows, row)
			}
		}
	}
	if len(rows) == 0 {
		return nil, fmt.Errorf("payload contains no spans")
	}
	return rows, nil
}

// runKindName maps the vigil.run.kind attribute to the spans.run_kind enum name.
func runKindName(v string) string {
	switch v {
	case "live":
		return "live"
	case "eval":
		return "eval"
	default:
		return "unknown"
	}
}

func spanKindName(k tracepb.Span_SpanKind) string {
	switch k {
	case tracepb.Span_SPAN_KIND_INTERNAL:
		return "INTERNAL"
	case tracepb.Span_SPAN_KIND_SERVER:
		return "SERVER"
	case tracepb.Span_SPAN_KIND_CLIENT:
		return "CLIENT"
	case tracepb.Span_SPAN_KIND_PRODUCER:
		return "PRODUCER"
	case tracepb.Span_SPAN_KIND_CONSUMER:
		return "CONSUMER"
	default:
		return "UNSPECIFIED"
	}
}

func statusCodeName(c tracepb.Status_StatusCode) string {
	switch c {
	case tracepb.Status_STATUS_CODE_OK:
		return "OK"
	case tracepb.Status_STATUS_CODE_ERROR:
		return "ERROR"
	default:
		return "UNSET"
	}
}

// take returns m[key] and deletes it, so promoted keys don't linger in the map tail.
func take(m map[string]string, key string) string {
	v := m[key]
	delete(m, key)
	return v
}

func parseUint32(s string) uint32 {
	if s == "" {
		return 0
	}
	n, err := strconv.ParseUint(s, 10, 32)
	if err != nil {
		return 0
	}
	return uint32(n)
}

// parseInt32 parses a signed int attribute, returning def when absent/unparseable. Used for
// vigil.eval.trial, where trial 0 is valid, so a distinct sentinel (-1) marks "not present".
func parseInt32(s string, def int32) int32 {
	if s == "" {
		return def
	}
	n, err := strconv.ParseInt(s, 10, 32)
	if err != nil {
		return def
	}
	return int32(n)
}

// parseBool01 maps a boolean attribute string to 0/1. OTLP bool attributes stringify to
// "true"/"false" (anyToString); "1" is tolerated. Anything else (incl. absent) is 0.
func parseBool01(s string) uint8 {
	if s == "true" || s == "1" {
		return 1
	}
	return 0
}

// attrsToMap flattens OTLP key-values into a string map (never nil).
func attrsToMap(kvs []*commonpb.KeyValue) map[string]string {
	m := make(map[string]string, len(kvs))
	for _, kv := range kvs {
		m[kv.GetKey()] = anyToString(kv.GetValue())
	}
	return m
}

// attrsToJSON encodes event attributes as a JSON object string (per the events.attributes
// column, "JSON-encoded per event").
func attrsToJSON(kvs []*commonpb.KeyValue) string {
	m := make(map[string]any, len(kvs))
	for _, kv := range kvs {
		m[kv.GetKey()] = anyToInterface(kv.GetValue())
	}
	b, err := json.Marshal(m)
	if err != nil {
		return "{}"
	}
	return string(b)
}

// anyToString renders an OTLP AnyValue as a string for the flat attribute maps. Scalars
// format directly; arrays and kvlists are JSON-encoded.
func anyToString(v *commonpb.AnyValue) string {
	switch x := v.GetValue().(type) {
	case *commonpb.AnyValue_StringValue:
		return x.StringValue
	case *commonpb.AnyValue_BoolValue:
		return strconv.FormatBool(x.BoolValue)
	case *commonpb.AnyValue_IntValue:
		return strconv.FormatInt(x.IntValue, 10)
	case *commonpb.AnyValue_DoubleValue:
		return strconv.FormatFloat(x.DoubleValue, 'g', -1, 64)
	case *commonpb.AnyValue_BytesValue:
		return hex.EncodeToString(x.BytesValue)
	case *commonpb.AnyValue_ArrayValue, *commonpb.AnyValue_KvlistValue:
		b, _ := json.Marshal(anyToInterface(v))
		return string(b)
	default:
		return ""
	}
}

// anyToInterface converts an AnyValue into a Go value for JSON encoding.
func anyToInterface(v *commonpb.AnyValue) any {
	switch x := v.GetValue().(type) {
	case *commonpb.AnyValue_StringValue:
		return x.StringValue
	case *commonpb.AnyValue_BoolValue:
		return x.BoolValue
	case *commonpb.AnyValue_IntValue:
		return x.IntValue
	case *commonpb.AnyValue_DoubleValue:
		return x.DoubleValue
	case *commonpb.AnyValue_BytesValue:
		return hex.EncodeToString(x.BytesValue)
	case *commonpb.AnyValue_ArrayValue:
		arr := make([]any, 0, len(x.ArrayValue.GetValues()))
		for _, e := range x.ArrayValue.GetValues() {
			arr = append(arr, anyToInterface(e))
		}
		return arr
	case *commonpb.AnyValue_KvlistValue:
		m := make(map[string]any, len(x.KvlistValue.GetValues()))
		for _, kv := range x.KvlistValue.GetValues() {
			m[kv.GetKey()] = anyToInterface(kv.GetValue())
		}
		return m
	default:
		return nil
	}
}
