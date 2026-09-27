import asyncio
import functools

import vigil


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Usage:
    def __init__(self, i, o):
        self.input_tokens = i
        self.output_tokens = o


class TextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class ToolBlock:
    type = "tool_use"

    def __init__(self, name, inp, id):
        self.name = name
        self.input = inp
        self.id = id


class Response:
    def __init__(self, model, usage, content, stop_reason="end_turn"):
        self.model = model
        self.usage = usage
        self.content = content
        self.stop_reason = stop_reason


def _text_and_tool_response():
    return Response(
        "claude-haiku-4-5",
        Usage(11, 22),
        [TextBlock("hi"), ToolBlock("search", {"q": "x"}, "tu_1")],
    )


# --- sync ---


class SyncMessages:
    def create(self, **kwargs):
        if kwargs.get("stream"):
            return iter(
                [
                    Obj(
                        type="message_start",
                        message=Obj(model="claude-haiku-4-5", usage=Obj(input_tokens=7)),
                    ),
                    Obj(
                        type="content_block_start",
                        content_block=Obj(
                            type="tool_use", name="search", input={"q": "x"}, id="tu_9"
                        ),
                    ),
                    Obj(type="message_delta", usage=Obj(output_tokens=13)),
                    Obj(type="message_stop"),
                ]
            )
        return _text_and_tool_response()


class SyncClient:
    def __init__(self):
        self.messages = SyncMessages()


def test_wrap_records_cache_tokens_when_present(spans):
    # A response whose usage carries prompt-cache counts records them on the span; a plain
    # usage (no cache attrs) records neither (backward compatible).
    class CacheUsage:
        def __init__(self):
            self.input_tokens = 40
            self.output_tokens = 12
            self.cache_creation_input_tokens = 5300
            self.cache_read_input_tokens = 0

    class CacheMessages:
        def create(self, **kwargs):
            return Response("claude-haiku-4-5", CacheUsage(), [TextBlock("ok")])

    class CacheClient:
        def __init__(self):
            self.messages = CacheMessages()

    client = vigil.wrap(CacheClient())
    with vigil.agent_run(run_kind="eval"):
        client.messages.create(model="claude-haiku-4-5", max_tokens=50, messages=[])
    s = [x for x in spans() if x.name.startswith("gen_ai.chat")][0]
    assert s.attributes["gen_ai.usage.cache_creation_input_tokens"] == 5300
    assert s.attributes["gen_ai.usage.cache_read_input_tokens"] == 0


def test_wrap_omits_cache_tokens_when_absent(spans):
    client = vigil.wrap(SyncClient())
    with vigil.agent_run(run_kind="eval"):
        client.messages.create(model="claude-haiku-4-5", max_tokens=100, messages=[])
    s = [x for x in spans() if x.name.startswith("gen_ai.chat")][0]
    assert "gen_ai.usage.cache_creation_input_tokens" not in s.attributes
    assert "gen_ai.usage.cache_read_input_tokens" not in s.attributes


def test_wrap_sync_records_model_tokens_and_tool_use(spans):
    client = vigil.wrap(SyncClient())
    with vigil.agent_run(run_kind="eval"):
        r = client.messages.create(
            model="claude-haiku-4-5", max_tokens=100, messages=[{"role": "user", "content": "hey"}]
        )
    assert r.model == "claude-haiku-4-5"

    s = [x for x in spans() if x.name.startswith("gen_ai.chat")][0]
    assert s.attributes["gen_ai.request.model"] == "claude-haiku-4-5"
    assert s.attributes["gen_ai.request.max_tokens"] == 100
    assert s.attributes["gen_ai.usage.input_tokens"] == 11
    assert s.attributes["gen_ai.usage.output_tokens"] == 22
    # identity from the surrounding run is stamped on the gen_ai span too
    assert s.attributes["vigil.run.kind"] == "eval"
    names = {e.name for e in s.events}
    assert "gen_ai.choice" in names
    assert "gen_ai.tool_use" in names


def test_wrap_sync_streaming(spans):
    client = vigil.wrap(SyncClient())
    with vigil.agent_run(run_kind="eval"):
        events = list(
            client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=10,
                stream=True,
                messages=[{"role": "user", "content": "x"}],
            )
        )
    assert len(events) == 4
    s = [x for x in spans() if x.name.startswith("gen_ai.chat")][0]
    assert s.attributes["gen_ai.usage.input_tokens"] == 7
    assert s.attributes["gen_ai.usage.output_tokens"] == 13
    assert "gen_ai.tool_use" in {e.name for e in s.events}


# --- async ---


class AsyncMessages:
    async def create(self, **kwargs):
        return Response("claude-haiku-4-5", Usage(5, 6), [TextBlock("yo")])


class AsyncClient:
    def __init__(self):
        self.messages = AsyncMessages()


def test_wrap_async_records(spans):
    client = vigil.wrap(AsyncClient())

    async def go():
        with vigil.agent_run(run_kind="eval"):
            return await client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=10,
                messages=[{"role": "user", "content": "hey"}],
            )

    r = asyncio.run(go())
    assert r.model == "claude-haiku-4-5"
    s = [x for x in spans() if x.name.startswith("gen_ai.chat")][0]
    assert s.attributes["gen_ai.usage.input_tokens"] == 5
    assert s.attributes["gen_ai.usage.output_tokens"] == 6


# --- async: decorated create (mimics the real AsyncAnthropic) + response recording ---

# The real AsyncAnthropic.messages.create is a *decorated* async method: inspect
# .iscoroutinefunction returns False on the outer wrapper, and the true coroutine function is
# reachable via __wrapped__. These stubs reproduce that so the tests catch the detection bug
# (async client misrouted to the sync path -> span ended before the await -> 0 tokens).

_SLEEP = 0.02  # long enough that a span ending before the await would have ~0 duration


def _decorated_async(fn):
    @functools.wraps(fn)  # sets outer.__wrapped__ = fn, so inspect.unwrap reaches the coroutine
    def outer(self, **kwargs):
        return fn(self, **kwargs)  # returns a coroutine / async generator, not awaited here

    return outer


async def _async_stream_events():
    yield Obj(type="message_start", message=Obj(model="claude-haiku-4-5", usage=Obj(input_tokens=7)))
    yield Obj(
        type="content_block_start",
        content_block=Obj(type="tool_use", name="search", input={"q": "x"}, id="tu_9"),
    )
    yield Obj(type="message_delta", usage=Obj(output_tokens=13))
    yield Obj(type="message_stop")


class DecoratedAsyncMessages:
    @_decorated_async
    async def create(self, **kwargs):
        await asyncio.sleep(_SLEEP)  # simulate network latency spanning the awaited call
        if kwargs.get("stream"):
            return _async_stream_events()
        return Response("claude-haiku-4-5", Usage(5, 6), [TextBlock("yo"), ToolBlock("search", {"q": "x"}, "tu_1")])


class DecoratedAsyncClient:
    def __init__(self):
        self.messages = DecoratedAsyncMessages()


def test_wrap_detects_decorated_async_client():
    # Regression: a decorated async create must still route to the async wrapper.
    from vigil.anthropic import _AsyncClient

    assert isinstance(vigil.wrap(DecoratedAsyncClient()), _AsyncClient)


def _duration_ns(span):
    return span.end_time - span.start_time


def test_wrap_async_records_tokens_model_and_duration(spans):
    client = vigil.wrap(DecoratedAsyncClient())

    async def go():
        with vigil.agent_run(run_kind="eval"):
            return await client.messages.create(
                model="claude-haiku-4-5", max_tokens=10, messages=[{"role": "user", "content": "hey"}]
            )

    r = asyncio.run(go())
    assert r.model == "claude-haiku-4-5"

    s = [x for x in spans() if x.name.startswith("gen_ai.chat")][0]
    assert s.attributes["gen_ai.usage.input_tokens"] == 5
    assert s.attributes["gen_ai.usage.output_tokens"] == 6
    assert s.attributes["gen_ai.response.model"] == "claude-haiku-4-5"
    assert "gen_ai.tool_use" in {e.name for e in s.events}
    # The span must stay open across the awaited call: its duration covers the latency, not ~0
    # (the bug ended the span before the await, giving a near-zero duration).
    assert _duration_ns(s) >= _SLEEP * 1e9 * 0.5


def test_wrap_async_streaming_records_tokens_model_and_duration(spans):
    client = vigil.wrap(DecoratedAsyncClient())

    async def go():
        with vigil.agent_run(run_kind="eval"):
            stream = await client.messages.create(
                model="claude-haiku-4-5", max_tokens=10, stream=True,
                messages=[{"role": "user", "content": "x"}],
            )
            return [event async for event in stream]

    events = asyncio.run(go())
    assert len(events) == 4

    s = [x for x in spans() if x.name.startswith("gen_ai.chat")][0]
    assert s.attributes["gen_ai.usage.input_tokens"] == 7
    assert s.attributes["gen_ai.usage.output_tokens"] == 13
    assert s.attributes["gen_ai.response.model"] == "claude-haiku-4-5"
    assert "gen_ai.tool_use" in {e.name for e in s.events}
    assert _duration_ns(s) >= _SLEEP * 1e9 * 0.5


# --- kwargs pass-through (the wrapper must forward call args verbatim, add/drop nothing) ---

class RecordingMessages:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return _text_and_tool_response()


class RecordingClient:
    def __init__(self):
        self.messages = RecordingMessages()


class AsyncRecordingMessages:
    def __init__(self):
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return _text_and_tool_response()


class AsyncRecordingClient:
    def __init__(self):
        self.messages = AsyncRecordingMessages()


_CALL_KWARGS = {
    "model": "claude-haiku-4-5",
    "max_tokens": 100,
    "system": "you are helpful",
    "tools": [{"name": "t"}],
    "messages": [{"role": "user", "content": "hi"}],
}


def test_wrap_sync_forwards_kwargs_verbatim(spans):
    raw = RecordingClient()
    client = vigil.wrap(raw)
    with vigil.agent_run(run_kind="eval"):
        client.messages.create(**_CALL_KWARGS)
    # The wrapper adds nothing and drops nothing.
    assert raw.messages.kwargs == _CALL_KWARGS


def test_wrap_async_forwards_kwargs_verbatim(spans):
    raw = AsyncRecordingClient()
    client = vigil.wrap(raw)

    async def go():
        with vigil.agent_run(run_kind="eval"):
            await client.messages.create(**_CALL_KWARGS)

    asyncio.run(go())
    assert raw.messages.kwargs == _CALL_KWARGS
