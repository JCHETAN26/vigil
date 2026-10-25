"""Instrument an Anthropic client so every ``messages.create`` / ``messages.stream`` call
records model, tokens, latency, request/response content, and tool_use blocks as a
gen_ai span — without changing call sites. Works for both ``Anthropic`` and
``AsyncAnthropic``, including streaming. Uses duck typing, so it needs no import of the
anthropic package and works against test stubs.

    from anthropic import Anthropic
    from vigil import wrap
    client = wrap(Anthropic())
    client.messages.create(model="claude-haiku-4-5", max_tokens=1024, messages=[...])

Single accounting ownership: anthropic>=1.17 has OpenTelemetry tracing of its own, which
gives each API call an ``anthropic.messages.create`` span carrying the same
``gen_ai.usage.*`` tokens. Left on, that span lands in the same pipeline as the Vigil span
(both use the global provider ``vigil.init`` registers), as its child, and every token and
dollar is counted twice. So the calls this wrapper records (``messages.create`` and
``messages.stream``) go through a copy of the client whose native spans are off — made with
the SDK's public ``with_options(open_telemetry=...)``, sharing the client's HTTP client,
headers, retries and middleware. The caller's client is not changed, and every other method
(``count_tokens``, ``batches``, …) keeps its native spans. A client whose native spans go to a
provider other than Vigil's (its own ``tracer_provider``) is left alone, since nothing is
counted twice there.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from .spans import set_gen_ai_request, set_gen_ai_response
from .tracer import get_content_capture, get_tracer, span_provider

_REQUEST_PARAM_KEYS = ("max_tokens", "temperature", "top_p", "top_k")


# --- native SDK tracing (anthropic>=1.17): one owner per call ---


def _is_native_client(client: Any) -> bool:
    """True for a real Anthropic client that has the native ``open_telemetry`` option.

    Looked up statically, on the type: a wrapper that delegates by ``__getattr__`` (e.g. the
    engine's dev cache) must not count, or ``with_options`` would hand back a copy of the
    client *inside* it and bypass the wrapper."""
    for name in ("with_options", "open_telemetry"):
        try:
            inspect.getattr_static(client, name)
        except AttributeError:
            return False
    return True


def _native_options(client: Any) -> Mapping | None:
    """The client's resolved ``open_telemetry`` option when its native call spans are on."""
    if not _is_native_client(client):
        return None
    try:
        options = client.open_telemetry
    except Exception:
        return None
    if not isinstance(options, Mapping):
        return None
    traces = options.get("traces")
    if not isinstance(traces, Mapping) or not traces.get("enabled"):
        return None
    return options


def without_native_spans(client: Any) -> Any:
    """A copy of ``client`` whose native SDK call spans are off, or ``client`` itself when it
    has none (an older anthropic, spans already off, or not a real client).

    The copy comes from the public ``with_options(open_telemetry=...)``: it shares the
    client's HTTP client (so a custom transport is kept), credentials, headers, query,
    timeout, retries and middleware, and keeps its other ``open_telemetry`` fields. ``client``
    is not changed. ``wrap`` applies this on its own to the calls it records; use it directly
    where a wrapper sits between the real client and ``wrap``, as the engine's cache does."""
    options = _native_options(client)
    if options is None:
        return client
    return client.with_options(
        open_telemetry={**options, "traces": {**options["traces"], "enabled": False}}
    )


class _NativeSpanOwner:
    """Picks, per call, the client whose ``messages`` the wrapper calls: the caller's own, or
    (when its native call spans would reach Vigil's provider and duplicate the Vigil span) a
    lazily made copy with them off."""

    def __init__(self, client):
        self.client = client
        self._quiet = None

    def messages(self):
        options = _native_options(self.client)
        if options is None or not self._reaches_vigil(options):
            return self.client.messages
        if self._quiet is None:
            self._quiet = without_native_spans(self.client)
        return self._quiet.messages

    @staticmethod
    def _reaches_vigil(options: Mapping) -> bool:
        native = options.get("tracer_provider")
        return (native if native is not None else trace.get_tracer_provider()) is span_provider()


def _span_name(kwargs: dict) -> str:
    model = kwargs.get("model")
    return f"gen_ai.chat {model}" if model else "gen_ai.chat"


def _start_span(kwargs: dict):
    tracer = get_tracer()
    span = tracer.start_span(_span_name(kwargs), kind=SpanKind.CLIENT)
    params = {k: kwargs.get(k) for k in _REQUEST_PARAM_KEYS}
    set_gen_ai_request(
        span, system="anthropic", operation="chat", request_model=kwargs.get("model"), params=params
    )
    _record_request_content(span, kwargs)
    return span


def _record_request_content(span, kwargs: dict) -> None:
    cc = get_content_capture()
    system = kwargs.get("system")
    if system:
        cc.record_event(span, "gen_ai.system.message", system)
    for message in kwargs.get("messages", []) or []:
        role = (
            message.get("role", "user")
            if isinstance(message, dict)
            else getattr(message, "role", "user")
        )
        content = (
            message.get("content")
            if isinstance(message, dict)
            else getattr(message, "content", None)
        )
        cc.record_event(span, f"gen_ai.{role}.message", content)


def _record_response(span, response) -> None:
    cc = get_content_capture()
    usage = getattr(response, "usage", None)
    set_gen_ai_response(
        span,
        response_model=getattr(response, "model", None),
        input_tokens=getattr(usage, "input_tokens", None) if usage else None,
        output_tokens=getattr(usage, "output_tokens", None) if usage else None,
        cache_creation_input_tokens=(
            getattr(usage, "cache_creation_input_tokens", None) if usage else None
        ),
        cache_read_input_tokens=(
            getattr(usage, "cache_read_input_tokens", None) if usage else None
        ),
        finish_reason=getattr(response, "stop_reason", None),
    )
    for block in getattr(response, "content", []) or []:
        _record_content_block(span, cc, block)


def _record_content_block(span, cc, block) -> None:
    btype = getattr(block, "type", None)
    if btype == "text":
        cc.record_event(span, "gen_ai.choice", getattr(block, "text", ""))
    elif btype == "tool_use":
        cc.record_event(
            span,
            "gen_ai.tool_use",
            getattr(block, "input", {}) or {},
            extra={
                "gen_ai.tool.name": getattr(block, "name", None),
                "gen_ai.tool.id": getattr(block, "id", None),
            },
        )


def _fail(span, exc: BaseException) -> None:
    span.record_exception(exc)
    span.set_status(Status(StatusCode.ERROR))


# --- streaming accumulation (shared by sync and async) ---


class _StreamAccumulator:
    """Reads Anthropic streaming events, accumulating model, token usage, and tool_use
    blocks, then records them onto the span when the stream is exhausted."""

    def __init__(self, span):
        self.span = span
        self.model = None
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_creation_input_tokens = 0
        self.cache_read_input_tokens = 0
        self.tool_blocks: list = []

    def observe(self, event) -> None:
        etype = getattr(event, "type", None)
        if etype == "message_start":
            msg = getattr(event, "message", None)
            self.model = getattr(msg, "model", None)
            usage = getattr(msg, "usage", None)
            if usage and getattr(usage, "input_tokens", None) is not None:
                self.input_tokens = usage.input_tokens
            # Cache usage is reported on message_start (part of the input accounting).
            if usage and getattr(usage, "cache_creation_input_tokens", None) is not None:
                self.cache_creation_input_tokens = usage.cache_creation_input_tokens
            if usage and getattr(usage, "cache_read_input_tokens", None) is not None:
                self.cache_read_input_tokens = usage.cache_read_input_tokens
        elif etype == "content_block_start":
            block = getattr(event, "content_block", None)
            if getattr(block, "type", None) == "tool_use":
                self.tool_blocks.append(block)
        elif etype == "message_delta":
            usage = getattr(event, "usage", None)
            if usage and getattr(usage, "output_tokens", None) is not None:
                self.output_tokens = usage.output_tokens

    def finish(self) -> None:
        cc = get_content_capture()
        set_gen_ai_response(
            self.span,
            response_model=self.model,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens,
        )
        for block in self.tool_blocks:
            _record_content_block(self.span, cc, block)
        self.span.end()


# --- sync wrappers ---


class _SyncMessages:
    def __init__(self, owner: _NativeSpanOwner):
        self._owner = owner

    def create(self, **kwargs):
        span = _start_span(kwargs)
        if kwargs.get("stream"):
            try:
                # Current while the request is sent, so HTTP-client spans nest under it.
                with _current_span(span):
                    raw = self._owner.messages().create(**kwargs)
            except Exception as exc:
                _fail(span, exc)
                span.end()
                raise
            return _sync_stream_iter(span, raw)
        try:
            with _current_span(span):
                response = self._owner.messages().create(**kwargs)
                _record_response(span, response)
                return response
        except Exception as exc:
            _fail(span, exc)
            raise
        finally:
            span.end()

    def stream(self, **kwargs):
        return _SyncStreamCM(self._owner, kwargs)

    def __getattr__(self, name):
        # Methods the wrapper does not record go to the caller's own client, native spans and all.
        return getattr(self._owner.client.messages, name)


def _sync_stream_iter(span, raw):
    acc = _StreamAccumulator(span)
    try:
        for event in raw:
            acc.observe(event)
            yield event
    except Exception as exc:
        _fail(span, exc)
        span.end()
        raise
    else:
        acc.finish()


class _SyncStreamCM:
    def __init__(self, owner: _NativeSpanOwner, kwargs):
        self._owner = owner
        self._kwargs = kwargs

    def __enter__(self):
        self._span = _start_span(self._kwargs)
        try:
            with _current_span(self._span):
                self._mgr = self._owner.messages().stream(**self._kwargs)
                self._stream = self._mgr.__enter__()
                return self._stream
        except Exception as exc:
            _fail(self._span, exc)
            self._span.end()
            raise

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc is None:
                final = _final_message(_message_holder(self._mgr, self._stream))
                if final is not None:
                    _record_response(self._span, final)
            else:
                _fail(self._span, exc)
        finally:
            self._span.end()
        return self._mgr.__exit__(exc_type, exc, tb)


class _SyncClient:
    def __init__(self, client):
        self._client = client
        self._messages = _SyncMessages(_NativeSpanOwner(client))

    @property
    def messages(self):
        return self._messages

    def __getattr__(self, name):
        return getattr(self._client, name)


# --- async wrappers ---


class _AsyncMessages:
    def __init__(self, owner: _NativeSpanOwner):
        self._owner = owner

    async def create(self, **kwargs):
        span = _start_span(kwargs)
        if kwargs.get("stream"):
            try:
                with _current_span(span):
                    raw = await self._owner.messages().create(**kwargs)
            except Exception as exc:
                _fail(span, exc)
                span.end()
                raise
            return _async_stream_iter(span, raw)
        try:
            with _current_span(span):
                response = await self._owner.messages().create(**kwargs)
                _record_response(span, response)
                return response
        except Exception as exc:
            _fail(span, exc)
            raise
        finally:
            span.end()

    def stream(self, **kwargs):
        return _AsyncStreamCM(self._owner, kwargs)

    def __getattr__(self, name):
        return getattr(self._owner.client.messages, name)


async def _async_stream_iter(span, raw):
    acc = _StreamAccumulator(span)
    try:
        async for event in raw:
            acc.observe(event)
            yield event
    except Exception as exc:
        _fail(span, exc)
        span.end()
        raise
    else:
        acc.finish()


class _AsyncStreamCM:
    def __init__(self, owner: _NativeSpanOwner, kwargs):
        self._owner = owner
        self._kwargs = kwargs

    async def __aenter__(self):
        self._span = _start_span(self._kwargs)
        try:
            with _current_span(self._span):
                self._mgr = self._owner.messages().stream(**self._kwargs)
                self._stream = await self._mgr.__aenter__()
                return self._stream
        except Exception as exc:
            _fail(self._span, exc)
            self._span.end()
            raise

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if exc is None:
                final = await _final_message_async(_message_holder(self._mgr, self._stream))
                if final is not None:
                    _record_response(self._span, final)
            else:
                _fail(self._span, exc)
        finally:
            self._span.end()
        return await self._mgr.__aexit__(exc_type, exc, tb)


class _AsyncClient:
    def __init__(self, client):
        self._client = client
        self._messages = _AsyncMessages(_NativeSpanOwner(client))

    @property
    def messages(self):
        return self._messages

    def __getattr__(self, name):
        return getattr(self._client, name)


# --- helpers ---


def _message_holder(mgr, entered):
    """What holds the streamed message: the object ``__enter__`` returned (the real SDK's
    ``MessageStream``; its manager keeps it under a private name-mangled attribute), else a
    ``_stream`` attribute or the manager itself, as simpler stand-ins have it."""
    for candidate in (entered, getattr(mgr, "_stream", None)):
        if _has_snapshot(candidate) or callable(getattr(candidate, "get_final_message", None)):
            return candidate
    return mgr


def _has_snapshot(stream) -> bool:
    try:
        inspect.getattr_static(stream, "current_message_snapshot")
    except AttributeError:
        return False
    return True


def _snapshot(stream):
    """The real SDK's message accumulated so far, read without consuming more of the stream
    (a caller that leaves the block early is not made to wait for the rest)."""
    try:
        return stream.current_message_snapshot
    except Exception:  # nothing arrived yet
        return None


def _final_message(stream):
    if _has_snapshot(stream):
        return _snapshot(stream)
    getter = getattr(stream, "get_final_message", None)
    return getter() if callable(getter) else None


async def _final_message_async(stream):
    if _has_snapshot(stream):
        return _snapshot(stream)
    getter = getattr(stream, "get_final_message", None)
    if not callable(getter):
        return None
    result = getter()
    return await result if inspect.isawaitable(result) else result


class _current_span:
    """Make a span the current span for the duration of a with-block, without ending it
    (the caller ends it in a finally)."""

    def __init__(self, span):
        from opentelemetry import trace as _trace

        self._span = span
        self._cm = _trace.use_span(
            span, end_on_exit=False, record_exception=False, set_status_on_exception=False
        )

    def __enter__(self):
        return self._cm.__enter__()

    def __exit__(self, *exc):
        return self._cm.__exit__(*exc)


def _is_async_callable(fn: Any) -> bool:
    """True if ``fn`` is (or wraps) a coroutine function.

    The real ``AsyncAnthropic.messages.create`` is a *decorated* async method (e.g. the
    SDK's ``@required_args``), so ``inspect.iscoroutinefunction`` returns False on the
    outer wrapper even though the underlying method is ``async def`` — it exposes the real
    function via ``__wrapped__``. We must unwrap before deciding, or an async client is
    misrouted to the sync path, which never awaits the response: it would record usage off
    an un-awaited coroutine (0 tokens, empty model) and end the span before the response
    arrives. ``inspect.unwrap`` follows the ``__wrapped__`` chain."""
    if inspect.iscoroutinefunction(fn):
        return True
    try:
        return inspect.iscoroutinefunction(inspect.unwrap(fn))
    except Exception:
        return False


def wrap(client: Any):
    """Return an instrumented wrapper around an Anthropic or AsyncAnthropic client. The
    wrapper delegates every attribute except ``messages``, which it instruments. Wrapping an
    already wrapped client returns it unchanged: a second layer would record every call twice.
    """
    if isinstance(client, (_SyncClient, _AsyncClient)):
        return client
    create = getattr(getattr(client, "messages", None), "create", None)
    if create is None:
        raise TypeError("wrap() expects an Anthropic client with a .messages.create method")
    if _is_async_callable(create):
        return _AsyncClient(client)
    return _SyncClient(client)
