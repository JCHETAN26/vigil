"""Instrument an Anthropic client so every ``messages.create`` / ``messages.stream`` call
records model, tokens, latency, request/response content, and tool_use blocks as a
gen_ai span — without changing call sites. Works for both ``Anthropic`` and
``AsyncAnthropic``, including streaming. Uses duck typing, so it needs no import of the
anthropic package and works against test stubs.

    from anthropic import Anthropic
    from vigil import wrap
    client = wrap(Anthropic())
    client.messages.create(model="claude-haiku-4-5", max_tokens=1024, messages=[...])
"""

from __future__ import annotations

import inspect
from typing import Any

from opentelemetry.trace import SpanKind, Status, StatusCode

from .spans import set_gen_ai_request, set_gen_ai_response
from .tracer import get_content_capture, get_tracer

_REQUEST_PARAM_KEYS = ("max_tokens", "temperature", "top_p", "top_k")


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
        self.tool_blocks: list = []

    def observe(self, event) -> None:
        etype = getattr(event, "type", None)
        if etype == "message_start":
            msg = getattr(event, "message", None)
            self.model = getattr(msg, "model", None)
            usage = getattr(msg, "usage", None)
            if usage and getattr(usage, "input_tokens", None) is not None:
                self.input_tokens = usage.input_tokens
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
        )
        for block in self.tool_blocks:
            _record_content_block(self.span, cc, block)
        self.span.end()


# --- sync wrappers ---


class _SyncMessages:
    def __init__(self, messages):
        self._messages = messages

    def create(self, **kwargs):
        span = _start_span(kwargs)
        if kwargs.get("stream"):
            try:
                raw = self._messages.create(**kwargs)
            except Exception as exc:
                _fail(span, exc)
                span.end()
                raise
            return _sync_stream_iter(span, raw)
        try:
            with _current_span(span):
                response = self._messages.create(**kwargs)
                _record_response(span, response)
                return response
        except Exception as exc:
            _fail(span, exc)
            raise
        finally:
            span.end()

    def stream(self, **kwargs):
        return _SyncStreamCM(self._messages, kwargs)

    def __getattr__(self, name):
        return getattr(self._messages, name)


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
    def __init__(self, messages, kwargs):
        self._messages = messages
        self._kwargs = kwargs

    def __enter__(self):
        self._span = _start_span(self._kwargs)
        self._mgr = self._messages.stream(**self._kwargs)
        return self._mgr.__enter__()

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc is None:
                stream = getattr(self._mgr, "_stream", None) or self._mgr
                final = _final_message(stream)
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
        self._messages = _SyncMessages(client.messages)

    @property
    def messages(self):
        return self._messages

    def __getattr__(self, name):
        return getattr(self._client, name)


# --- async wrappers ---


class _AsyncMessages:
    def __init__(self, messages):
        self._messages = messages

    async def create(self, **kwargs):
        span = _start_span(kwargs)
        if kwargs.get("stream"):
            try:
                raw = await self._messages.create(**kwargs)
            except Exception as exc:
                _fail(span, exc)
                span.end()
                raise
            return _async_stream_iter(span, raw)
        try:
            with _current_span(span):
                response = await self._messages.create(**kwargs)
                _record_response(span, response)
                return response
        except Exception as exc:
            _fail(span, exc)
            raise
        finally:
            span.end()

    def stream(self, **kwargs):
        return _AsyncStreamCM(self._messages, kwargs)

    def __getattr__(self, name):
        return getattr(self._messages, name)


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
    def __init__(self, messages, kwargs):
        self._messages = messages
        self._kwargs = kwargs

    async def __aenter__(self):
        self._span = _start_span(self._kwargs)
        self._mgr = self._messages.stream(**self._kwargs)
        return await self._mgr.__aenter__()

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if exc is None:
                stream = getattr(self._mgr, "_stream", None) or self._mgr
                final = await _final_message_async(stream)
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
        self._messages = _AsyncMessages(client.messages)

    @property
    def messages(self):
        return self._messages

    def __getattr__(self, name):
        return getattr(self._client, name)


# --- helpers ---


def _final_message(stream):
    getter = getattr(stream, "get_final_message", None)
    return getter() if callable(getter) else None


async def _final_message_async(stream):
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


def wrap(client: Any):
    """Return an instrumented wrapper around an Anthropic or AsyncAnthropic client. The
    wrapper delegates every attribute except ``messages``, which it instruments."""
    create = getattr(getattr(client, "messages", None), "create", None)
    if create is None:
        raise TypeError("wrap() expects an Anthropic client with a .messages.create method")
    if inspect.iscoroutinefunction(create):
        return _AsyncClient(client)
    return _SyncClient(client)
