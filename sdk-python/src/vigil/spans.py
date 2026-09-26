"""Context managers and decorators for the four span kinds — agent runs, LLM calls, tool
calls, and retrieval steps — plus helpers that set the gen_ai.* attributes."""

from __future__ import annotations

import functools
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry.trace import SpanKind, Status, StatusCode

from .context import RunContext, reset_run, set_run
from .tracer import get_content_capture, get_tracer

# --- gen_ai attribute helpers (usable directly and by the Anthropic wrapper) ---


def set_gen_ai_request(
    span,
    *,
    system: str | None = None,
    operation: str = "chat",
    request_model: str | None = None,
    params: dict | None = None,
) -> None:
    if system:
        span.set_attribute("gen_ai.system", system)
    span.set_attribute("gen_ai.operation.name", operation)
    if request_model:
        span.set_attribute("gen_ai.request.model", request_model)
    for key, attr in (
        ("max_tokens", "gen_ai.request.max_tokens"),
        ("temperature", "gen_ai.request.temperature"),
        ("top_p", "gen_ai.request.top_p"),
        ("top_k", "gen_ai.request.top_k"),
    ):
        if params and params.get(key) is not None:
            span.set_attribute(attr, params[key])


def set_gen_ai_response(
    span,
    *,
    response_model: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    finish_reason: str | None = None,
) -> None:
    if response_model:
        span.set_attribute("gen_ai.response.model", response_model)
    if input_tokens is not None:
        span.set_attribute("gen_ai.usage.input_tokens", int(input_tokens))
    if output_tokens is not None:
        span.set_attribute("gen_ai.usage.output_tokens", int(output_tokens))
    if finish_reason:
        span.set_attribute("gen_ai.response.finish_reasons", [finish_reason])


def _apply_run_attrs(span, rc: RunContext) -> None:
    span.set_attribute("vigil.run.id", rc.run_id)
    span.set_attribute("vigil.run.kind", rc.run_kind)
    if rc.eval_run_id:
        span.set_attribute("vigil.eval.run_id", rc.eval_run_id)
    if rc.eval_case_id:
        span.set_attribute("vigil.eval.case_id", rc.eval_case_id)
    if rc.session_id:
        span.set_attribute("gen_ai.conversation.id", rc.session_id)
    if rc.dataset:
        span.set_attribute("vigil.eval.dataset", rc.dataset)


# --- span context managers ---


@contextmanager
def agent_run(
    *,
    run_kind: str = "live",
    run_id: str | None = None,
    eval_run_id: str | None = None,
    eval_case_id: str | None = None,
    session_id: str | None = None,
    dataset: str | None = None,
    name: str = "agent.run",
    attributes: dict | None = None,
) -> Iterator[Any]:
    """Open a root span for one agent execution and set the ambient run identity, so every
    child span inherits it. ``run_id`` defaults to the trace id when not given (§2.2)."""
    tracer = get_tracer()
    with tracer.start_as_current_span(name) as span:
        trace_id = format(span.get_span_context().trace_id, "032x")
        rc = RunContext(
            run_id=run_id or trace_id,
            run_kind=run_kind,
            eval_run_id=eval_run_id,
            eval_case_id=eval_case_id,
            session_id=session_id,
            dataset=dataset,
        )
        token = set_run(rc)
        try:
            _apply_run_attrs(
                span, rc
            )  # the root started before the context was set; stamp it directly
            if attributes:
                span.set_attributes(attributes)
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR))
            raise
        finally:
            reset_run(token)


@contextmanager
def llm_call(
    *,
    system: str = "anthropic",
    operation: str = "chat",
    request_model: str | None = None,
    params: dict | None = None,
    name: str | None = None,
    attributes: dict | None = None,
) -> Iterator[Any]:
    """Open a CLIENT span for one LLM call. Set the response with ``set_gen_ai_response``
    (the Anthropic wrapper does this automatically)."""
    tracer = get_tracer()
    with tracer.start_as_current_span(name or f"gen_ai.{operation}", kind=SpanKind.CLIENT) as span:
        set_gen_ai_request(
            span, system=system, operation=operation, request_model=request_model, params=params
        )
        if attributes:
            span.set_attributes(attributes)
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR))
            raise


@contextmanager
def tool_call(
    *,
    tool_name: str,
    name: str | None = None,
    arguments: Any = None,
    attributes: dict | None = None,
) -> Iterator[Any]:
    """Open a span for a tool/function call. Arguments are captured under the content
    policy."""
    tracer = get_tracer()
    with tracer.start_as_current_span(name or f"tool.{tool_name}", kind=SpanKind.INTERNAL) as span:
        span.set_attribute("gen_ai.tool.name", tool_name)
        if attributes:
            span.set_attributes(attributes)
        if arguments is not None:
            get_content_capture().record_event(span, "gen_ai.tool.arguments", arguments)
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR))
            raise


@contextmanager
def retrieval(
    *,
    name: str = "retrieval",
    query: Any = None,
    system: str | None = None,
    attributes: dict | None = None,
) -> Iterator[Any]:
    """Open a span for a retrieval/RAG step. The query is captured under the content
    policy."""
    tracer = get_tracer()
    with tracer.start_as_current_span(name, kind=SpanKind.CLIENT) as span:
        span.set_attribute("gen_ai.operation.name", "retrieval")
        if system:
            span.set_attribute("gen_ai.system", system)
        if attributes:
            span.set_attributes(attributes)
        if query is not None:
            get_content_capture().record_event(span, "gen_ai.retrieval.query", query)
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR))
            raise


# --- decorator forms (wrap a function in the corresponding span) ---


def _decorator(cm_factory):
    def make(**cm_kwargs):
        def deco(fn):
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                with cm_factory(**cm_kwargs):
                    return fn(*args, **kwargs)

            return wrapper

        return deco

    return make


traced_agent_run = _decorator(agent_run)
traced_tool_call = _decorator(tool_call)
traced_retrieval = _decorator(retrieval)
