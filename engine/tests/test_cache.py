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
