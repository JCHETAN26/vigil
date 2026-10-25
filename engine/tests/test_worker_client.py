"""The worker's client has one accounting owner per call: the Vigil span.

``_build_client`` puts the dev cache between the real ``AsyncAnthropic`` and ``vigil.wrap``, so
``wrap`` cannot turn the SDK's own call spans (anthropic>=1.17) off by itself; the worker does
it on the real client. These drive the real client class over a mock HTTP transport, with its
native spans sent to a local provider so they are visible here: no request leaves the
process, no model is called.
"""

from __future__ import annotations

import asyncio

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from engine.config import EngineConfig
from engine.runner import worker

anthropic = pytest.importorskip("anthropic")
if not hasattr(anthropic.AsyncAnthropic, "open_telemetry"):
    pytest.skip(f"anthropic {anthropic.__version__} has no native tracing", allow_module_level=True)
httpx = pytest.importorskip("httpx2")

MODEL = "claude-haiku-4-5-20251001"
BODY = {
    "id": "msg_1", "type": "message", "role": "assistant", "model": MODEL,
    "content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn",
    "stop_sequence": None, "usage": {"input_tokens": 9, "output_tokens": 4},
}
CALL = {"model": MODEL, "max_tokens": 8, "messages": [{"role": "user", "content": "x"}]}


class _Store:
    def __init__(self):
        self.d: dict = {}

    async def get(self, key):
        return self.d.get(key)

    async def set(self, key, value, ex=None):
        self.d[key] = value


@pytest.fixture
def native(monkeypatch):
    """Make the worker's ``AsyncAnthropic`` a real client on a mock transport whose native
    spans go to a local exporter; returns (exporter, sent requests, real client class)."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    sent: list = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json=BODY)

    real = anthropic.AsyncAnthropic

    def factory(**kw):
        return real(api_key="test-key", max_retries=0,
                    http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                    open_telemetry={"tracer_provider": provider}, **kw)

    monkeypatch.setattr(anthropic, "AsyncAnthropic", factory)
    monkeypatch.setattr(worker, "redis_url", lambda: "redis://unused")
    monkeypatch.setattr(worker, "make_store", lambda url: _Store())
    return exporter, sent, factory


def _call_twice(client):
    async def go():
        await client.messages.create(**CALL)
        await client.messages.create(**CALL)

    asyncio.run(go())


def test_precondition_the_real_client_emits_native_usage_spans(native):
    exporter, _, factory = native
    asyncio.run(factory().messages.create(**CALL))
    (span,) = exporter.get_finished_spans()
    assert span.name == "anthropic.messages.create"
    assert span.attributes["gen_ai.usage.input_tokens"] == 9


@pytest.mark.parametrize("cache_mode", ["off", "read_write"])
def test_worker_client_has_no_native_call_spans(native, cache_mode):
    exporter, sent, _ = native
    client = worker._build_client(None, EngineConfig(cache_mode=cache_mode), "development")
    _call_twice(client)
    assert exporter.get_finished_spans() == ()
    # Cache off: both calls reach the transport. Cache on: the second is a hit, so the cache
    # is still in front of the (quiet) real client rather than bypassed by the copy.
    assert len(sent) == (2 if cache_mode == "off" else 1)
