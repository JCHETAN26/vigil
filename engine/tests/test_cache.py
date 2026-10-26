"""Unit tests for the dev-only LLM cache (design §7): measurement refusal (at construction and
re-checked before a cached read), and cache hit/miss with vigil.cache.hit span stamping."""

from __future__ import annotations

import asyncio

import pytest

from engine.runner.cache import (
    CacheRefusedError,
    RedisLLMCache,
    _CachedMessages,
    check_refusal,
)

# --- duck-typed fakes ---------------------------------------------------------------


class _Usage:
    def __init__(self, i, o):
        self.input_tokens = i
        self.output_tokens = o


class _TextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Response:
    def __init__(self):
        self.model = "claude-haiku-4-5"
        self.usage = _Usage(5, 6)
        self.stop_reason = "end_turn"
        self.content = [_TextBlock("hi")]


class FakeRawMessages:
    def __init__(self):
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        return _Response()


class FakeRawClient:
    def __init__(self):
        self.messages = FakeRawMessages()


class FakeStore:
    def __init__(self):
        self.d: dict = {}

    async def get(self, key):
        return self.d.get(key)

    async def set(self, key, value, ex=None):
        self.d[key] = value


# --- refusal ------------------------------------------------------------------------


def test_check_refusal():
    with pytest.raises(CacheRefusedError):
        check_refusal("measurement", "read")
    with pytest.raises(CacheRefusedError):
        check_refusal("measurement", "read_write")
    check_refusal("measurement", "off")  # off is always fine
    check_refusal("development", "read_write")  # dev may cache


def test_construction_refuses_measurement_cache():
    with pytest.raises(CacheRefusedError):
        RedisLLMCache(FakeRawClient(), FakeStore(), mode="measurement", cache_mode="read")


async def test_read_path_rechecks_refusal():
    # Bypass the constructor guard to prove the worker-side re-check before any cached read.
    cm = _CachedMessages(
        FakeRawClient().messages, FakeStore(), mode="measurement", cache_mode="read", ttl=10
    )
    with pytest.raises(CacheRefusedError):
        await cm.create(model="m", max_tokens=1, messages=[])


# --- hit / miss + span stamping ------------------------------------------------------


@pytest.fixture(scope="module")
def vigil_tracing():
    import vigil
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    cfg = vigil.Config(service_name="cache-test", agent_id="cache", agent_version="v1")
    provider = vigil.init(config=cfg, exporter=exporter, set_global=True)

    def get_spans():
        provider.force_flush()
        return exporter.get_finished_spans()

    yield provider, exporter, get_spans


def test_hit_miss_and_cache_hit_stamping(vigil_tracing):
    import vigil

    provider, exporter, get_spans = vigil_tracing
    provider.force_flush()
    exporter.clear()

    raw = FakeRawClient()
    client = vigil.wrap(
        RedisLLMCache(raw, FakeStore(), mode="development", cache_mode="read_write")
    )
    kwargs = dict(
        model="claude-haiku-4-5", max_tokens=10, messages=[{"role": "user", "content": "hi"}]
    )

    async def go():
        with vigil.agent_run(run_kind="eval"):
            await client.messages.create(**kwargs)  # miss -> real call
        with vigil.agent_run(run_kind="eval"):
            await client.messages.create(**kwargs)  # hit -> served from cache

    asyncio.run(go())

    # The raw client was called exactly once; the second call was served from cache.
    assert raw.messages.calls == 1

    llm_spans = [s for s in get_spans() if s.name.startswith("gen_ai.chat")]
    assert len(llm_spans) == 2
    hit_flags = [s.attributes.get("vigil.cache.hit") for s in llm_spans]
    assert hit_flags.count(True) == 1  # exactly the second (cached) call is flagged

    # The cached span still carries real token/model attributes, so it lands in ClickHouse with
    # full detail but is excludable from cost aggregates via vigil.cache.hit.
    hit_span = next(s for s in llm_spans if s.attributes.get("vigil.cache.hit"))
    assert hit_span.attributes["gen_ai.usage.input_tokens"] == 5
    assert hit_span.attributes["gen_ai.usage.output_tokens"] == 6
    assert hit_span.attributes["gen_ai.response.model"] == "claude-haiku-4-5"


# --- copies keep the cache (the real anthropic client over a mock transport) --------------
#
# ``vigil.wrap(cache).with_options(...)`` used to reach the raw client's with_options through
# delegation and return a bare copy: no cache, no Vigil span. No request leaves the process.

_MODEL = "claude-haiku-4-5-20251001"
_BODY = {
    "id": "msg_1", "type": "message", "role": "assistant", "model": _MODEL,
    "content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn",
    "stop_sequence": None, "usage": {"input_tokens": 9, "output_tokens": 4},
}
_CALL = {"model": _MODEL, "max_tokens": 8, "messages": [{"role": "user", "content": "x"}]}


def _real_async_client(requests: list):
    anthropic = pytest.importorskip("anthropic")
    try:  # anthropic>=1.17 is built on httpx2; older releases on httpx
        import httpx2 as httpx
    except ImportError:
        import httpx

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=_BODY)

    return anthropic.AsyncAnthropic(
        api_key="test-key", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _cached(requests, store, cache_mode):
    from vigil.anthropic import without_native_spans

    # As the worker builds it: native spans off, the cache, then vigil.wrap on top.
    raw = without_native_spans(_real_async_client(requests))
    return raw, RedisLLMCache(raw, store, mode="development", cache_mode=cache_mode, ttl=60)


@pytest.mark.parametrize("method", ["with_options", "copy"])
def test_wrapped_cache_copy_keeps_cache_and_tracing(vigil_tracing, method):
    import vigil

    provider, exporter, get_spans = vigil_tracing
    provider.force_flush()
    exporter.clear()
    requests: list = []
    store = FakeStore()
    raw, cache = _cached(requests, store, "read_write")
    client = vigil.wrap(cache)
    copied = getattr(client, method)(default_headers={"x-copy": method}, timeout=7.0)

    inner = copied._client
    assert type(copied) is type(client) and copied is not client
    assert isinstance(inner, RedisLLMCache) and inner is not cache
    assert inner._store is store and inner._settings == cache._settings
    assert inner._raw is not raw and inner._raw.timeout == 7.0

    async def go():
        await copied.messages.create(**_CALL)  # miss: one request, stored
        await copied.messages.create(**_CALL)  # hit: served from the shared store

    asyncio.run(go())
    assert len(requests) == 1 and requests[0].headers["x-copy"] == method
    assert len(store.d) == 1
    llm = [s for s in get_spans() if "gen_ai.usage.input_tokens" in s.attributes]
    assert [s.name for s in llm] == [f"gen_ai.chat {_MODEL}"] * 2  # no native duplicate
    assert [s.attributes.get("vigil.cache.hit") for s in llm].count(True) == 1
    # The caller's objects are unchanged.
    assert cache._raw is raw and raw.timeout != 7.0 and "x-copy" not in raw.default_headers


def test_wrapped_cache_copy_with_cache_off_passes_through(vigil_tracing):
    import vigil

    requests: list = []
    store = FakeStore()
    _, cache = _cached(requests, store, "off")
    copied = vigil.wrap(cache).with_options(max_retries=0)
    assert isinstance(copied._client, RedisLLMCache)
    assert copied._client._settings["cache_mode"] == "off"

    async def go():
        await copied.messages.create(**_CALL)
        await copied.messages.create(**_CALL)

    asyncio.run(go())
    assert len(requests) == 2 and store.d == {}


def test_cache_copy_keeps_native_spans_off_toward_vigil(vigil_tracing):
    from opentelemetry.sdk.trace import TracerProvider
    from vigil.tracer import span_provider

    _, cache = _cached([], FakeStore(), "read_write")
    assert cache._raw.open_telemetry["traces"]["enabled"] is False
    # A plain copy inherits the raw client's option (native spans off).
    assert cache.with_options(timeout=3.0)._raw.open_telemetry["traces"]["enabled"] is False
    # Turned back on toward Vigil's provider they would duplicate Vigil's span: off again.
    toward_vigil = cache.with_options(open_telemetry={"tracer_provider": span_provider()})
    assert toward_vigil._raw.open_telemetry["traces"]["enabled"] is False
    # A separate provider duplicates nothing in Vigil's pipeline: the caller's choice is kept.
    other = TracerProvider()
    elsewhere = cache.with_options(open_telemetry={"tracer_provider": other})
    assert elsewhere._raw.open_telemetry["traces"]["enabled"] is True
