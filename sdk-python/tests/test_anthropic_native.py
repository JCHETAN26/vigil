"""wrap() against the real installed anthropic client over a mock HTTP transport.

anthropic>=1.17 traces its own API calls (an ``anthropic.messages.create`` span carrying
``gen_ai.usage.*``). Under ``vigil.wrap`` that span was a child of the Vigil gen_ai span with
the same tokens, so ClickHouse counted every call twice (HotpotQA pilot run cafae810, see
docs/results/hotpotqa_triage.md). Duck-typed stubs cannot show this — only the real client
emits the native span — so these tests drive the actual client classes; no request leaves
the process and no model is called.
"""

import asyncio
import json

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import vigil
from vigil.anthropic import without_native_spans

anthropic = pytest.importorskip("anthropic")
try:  # anthropic>=1.17 is built on httpx2; older releases on httpx
    import httpx2 as httpx
except ImportError:
    import httpx

NATIVE_SCOPE = "com.anthropic.sdk.python"
MODEL = "claude-haiku-4-5-20251001"
BODY = {
    "id": "msg_1", "type": "message", "role": "assistant", "model": MODEL,
    "content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn",
    "stop_sequence": None, "usage": {"input_tokens": 9, "output_tokens": 4},
}
SSE_EVENTS = [
    ("message_start", {"type": "message_start", "message": {
        **BODY, "content": [], "stop_reason": None,
        "usage": {"input_tokens": 9, "output_tokens": 1}}}),
    ("content_block_start", {"type": "content_block_start", "index": 0,
                             "content_block": {"type": "text", "text": ""}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "text_delta", "text": "hi"}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    ("message_delta", {"type": "message_delta",
                       "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                       "usage": {"output_tokens": 4}}),
    ("message_stop", {"type": "message_stop"}),
]
CALL = {"model": MODEL, "max_tokens": 8, "messages": [{"role": "user", "content": "x"}]}


class Transport:
    """Answers /v1/messages (JSON or SSE) and /v1/messages/count_tokens; records requests."""

    def __init__(self):
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if request.url.path.endswith("/count_tokens"):
            return httpx.Response(200, json={"input_tokens": 3})
        if json.loads(request.content).get("stream"):
            sse = "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in SSE_EVENTS)
            return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=BODY)


def _sync_client(transport, **kw):
    return anthropic.Anthropic(api_key="test-key", max_retries=0,
                               http_client=httpx.Client(transport=httpx.MockTransport(transport)),
                               **kw)


def _async_client(transport, **kw):
    return anthropic.AsyncAnthropic(
        api_key="test-key", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)), **kw)


def _native(spans):
    return [s for s in spans if s.instrumentation_scope.name == NATIVE_SCOPE]


def _usage_spans(spans):
    return [s for s in spans if "gen_ai.usage.input_tokens" in s.attributes]


@pytest.fixture
def native_tracing():
    """Skip where the installed anthropic has no native tracing (nothing to duplicate)."""
    if not hasattr(anthropic.Anthropic, "open_telemetry"):
        pytest.skip(f"anthropic {anthropic.__version__} has no native OpenTelemetry tracing")


def _assert_one_owned_llm_span(spans):
    usage = _usage_spans(spans)
    assert [s.name for s in usage] == [f"gen_ai.chat {MODEL}"], [s.name for s in spans]
    assert usage[0].instrumentation_scope.name == "vigil"
    assert usage[0].attributes["gen_ai.usage.input_tokens"] == 9
    assert usage[0].attributes["gen_ai.usage.output_tokens"] == 4
    assert _native(spans) == []


def test_unwrapped_client_emits_native_usage_span(spans, native_tracing):
    # The precondition this suite guards: the real client, on Vigil's global provider, records
    # usage on a span of its own. Without wrap that is the only (and correct) record.
    _sync_client(Transport()).messages.create(**CALL)
    (native,) = _native(spans())
    assert native.name == "anthropic.messages.create"
    assert native.attributes["gen_ai.usage.input_tokens"] == 9


def test_sync_create_has_a_single_owner(spans, native_tracing):
    transport = Transport()
    raw = _sync_client(transport, default_headers={"x-custom": "kept"})
    with vigil.agent_run(run_kind="eval"):
        r = vigil.wrap(raw).messages.create(**CALL)
    assert r.usage.input_tokens == 9
    _assert_one_owned_llm_span(spans())
    # The copy shares the caller's HTTP client (custom transport) and headers.
    assert len(transport.requests) == 1
    assert transport.requests[0].headers["x-custom"] == "kept"


def test_async_create_has_a_single_owner(spans, native_tracing):
    transport = Transport()

    async def go():
        with vigil.agent_run(run_kind="eval"):
            return await vigil.wrap(_async_client(transport)).messages.create(**CALL)

    assert asyncio.run(go()).usage.output_tokens == 4
    _assert_one_owned_llm_span(spans())
    assert len(transport.requests) == 1


def test_sync_stream_true_has_a_single_owner(spans, native_tracing):
    client = vigil.wrap(_sync_client(Transport()))
    events = list(client.messages.create(**CALL, stream=True))
    assert [e.type for e in events][-1] == "message_stop"
    _assert_one_owned_llm_span(spans())


def test_async_stream_true_has_a_single_owner(spans, native_tracing):
    client = vigil.wrap(_async_client(Transport()))

    async def go():
        stream = await client.messages.create(**CALL, stream=True)
        return [e async for e in stream]

    assert asyncio.run(go())[-1].type == "message_stop"
    _assert_one_owned_llm_span(spans())


def test_sync_messages_stream_has_a_single_owner(spans, native_tracing):
    client = vigil.wrap(_sync_client(Transport()))
    with client.messages.stream(**CALL) as stream:
        text = "".join(stream.text_stream)
    assert text == "hi"
    _assert_one_owned_llm_span(spans())


def test_async_messages_stream_has_a_single_owner(spans, native_tracing):
    client = vigil.wrap(_async_client(Transport()))

    async def go():
        async with client.messages.stream(**CALL) as stream:
            return "".join([t async for t in stream.text_stream])

    assert asyncio.run(go()) == "hi"
    _assert_one_owned_llm_span(spans())


def test_equal_valued_calls_each_count(spans, native_tracing):
    # Two distinct calls with identical usage are two spans, not one deduplicated pair.
    client = vigil.wrap(_sync_client(Transport()))
    client.messages.create(**CALL)
    client.messages.create(**CALL)
    usage = _usage_spans(spans())
    assert len(usage) == 2 and len({s.context.span_id for s in usage}) == 2
    assert _native(spans()) == []


def test_callers_client_and_unrecorded_methods_keep_native_spans(spans, native_tracing):
    raw = _sync_client(Transport())
    client = vigil.wrap(raw)
    client.messages.create(**CALL)
    assert raw.open_telemetry["traces"]["enabled"] is True  # caller's client unchanged
    client.messages.count_tokens(model=MODEL, messages=CALL["messages"])  # not recorded by Vigil
    raw.messages.create(**CALL)  # direct use of the caller's client
    names = sorted(s.name for s in _native(spans()))
    assert names == ["anthropic.messages.count_tokens", "anthropic.messages.create"]


def test_native_spans_on_another_provider_are_left_alone(spans, native_tracing):
    # A client that sends its native spans elsewhere duplicates nothing in Vigil's pipeline.
    other_exporter = InMemorySpanExporter()
    other = TracerProvider()
    other.add_span_processor(SimpleSpanProcessor(other_exporter))
    raw = _sync_client(Transport(), open_telemetry={"tracer_provider": other})
    vigil.wrap(raw).messages.create(**CALL)
    _assert_one_owned_llm_span(spans())
    assert [s.name for s in other_exporter.get_finished_spans()] == ["anthropic.messages.create"]


def test_native_spans_explicitly_on_vigils_provider_are_suppressed(
        spans, native_tracing, provider_and_exporter):
    provider, _ = provider_and_exporter
    raw = _sync_client(Transport(), open_telemetry={"tracer_provider": provider})
    vigil.wrap(raw).messages.create(**CALL)
    _assert_one_owned_llm_span(spans())


def test_nested_wrap_records_once(spans, native_tracing):
    once = vigil.wrap(_async_client(Transport()))
    assert vigil.wrap(once) is once

    async def go():
        await vigil.wrap(vigil.wrap(once)).messages.create(**CALL)

    asyncio.run(go())
    _assert_one_owned_llm_span(spans())


def test_sync_copies_stay_wrapped(spans, native_tracing):
    # with_options / copy / with_middleware return new clients; reached through delegation they
    # came back bare, so their calls produced native spans only and no Vigil span.
    transport = Transport()
    raw = _sync_client(transport)
    client = vigil.wrap(raw)
    copies = [
        client.with_options(default_headers={"x-copy": "with_options"}, max_retries=1),
        client.copy(default_headers={"x-copy": "copy"}),
        client.with_middleware(),
    ]
    for c in copies:
        assert type(c) is type(client) and c is not client
        spans_before = len(spans())
        c.messages.create(**CALL)
        _assert_one_owned_llm_span(spans()[spans_before:])
    assert [r.headers.get("x-copy") for r in transport.requests] == ["with_options", "copy", None]
    # The caller's raw client is not changed by copying through the wrapper.
    assert raw.max_retries == 0 and "x-copy" not in raw.default_headers
    assert raw.open_telemetry["traces"]["enabled"] is True


def test_async_copies_stay_wrapped(spans, native_tracing):
    transport = Transport()
    client = vigil.wrap(_async_client(transport))

    async def go():
        a = await client.with_options(timeout=5.0).messages.create(**CALL)
        b = await client.copy(default_headers={"x-copy": "1"}).messages.create(**CALL)
        return a, b

    a, b = asyncio.run(go())
    assert a.usage.input_tokens == b.usage.input_tokens == 9
    usage = _usage_spans(spans())
    assert [s.instrumentation_scope.name for s in usage] == ["vigil", "vigil"]
    assert _native(spans()) == []
    assert transport.requests[1].headers["x-copy"] == "1"


def test_copy_to_another_provider_keeps_its_native_spans(spans, native_tracing):
    # A copy is judged on its own open_telemetry: sent to a separate provider, its native spans
    # duplicate nothing in Vigil's pipeline and are kept; the original stays suppressed.
    other_exporter = InMemorySpanExporter()
    other = TracerProvider()
    other.add_span_processor(SimpleSpanProcessor(other_exporter))
    client = vigil.wrap(_sync_client(Transport()))
    client.with_options(open_telemetry={"tracer_provider": other}).messages.create(**CALL)
    client.messages.create(**CALL)
    usage = _usage_spans(spans())
    assert [s.instrumentation_scope.name for s in usage] == ["vigil", "vigil"]
    assert _native(spans()) == []
    assert [s.name for s in other_exporter.get_finished_spans()] == ["anthropic.messages.create"]


def test_copy_through_a_delegating_wrapper_is_refused(native_tracing):
    raw = _sync_client(Transport())

    class Delegating:  # copies only by delegation: the copy would drop this layer
        def __init__(self, inner):
            self._inner = inner
            self.messages = inner.messages

        def __getattr__(self, name):
            return getattr(self._inner, name)

    client = vigil.wrap(Delegating(raw))
    for method in ("with_options", "copy", "with_middleware"):
        with pytest.raises(TypeError, match=f"only delegates {method}"):
            getattr(client, method)()

    class Bare:  # no copy method at all: the usual AttributeError
        def __init__(self, inner):
            self.messages = inner.messages

    with pytest.raises(AttributeError):
        vigil.wrap(Bare(raw)).with_options(timeout=1)


def test_without_native_spans(native_tracing):
    transport = Transport()
    raw = _sync_client(transport, open_telemetry={"traces": {"content_mode": "content"}})
    quiet = without_native_spans(raw)
    assert quiet is not raw
    assert quiet.open_telemetry["traces"]["enabled"] is False
    assert quiet.open_telemetry["traces"]["content_mode"] == "content"  # other fields kept
    assert raw.open_telemetry["traces"]["enabled"] is True
    assert without_native_spans(quiet) is quiet  # already off: nothing to copy

    class Delegating:  # e.g. the engine's dev cache: must not be bypassed by a copy
        def __init__(self, inner):
            self._inner = inner
            self.messages = inner.messages

        def __getattr__(self, name):
            return getattr(self._inner, name)

    cache = Delegating(raw)
    assert without_native_spans(cache) is cache
