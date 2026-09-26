"""Tracer bootstrap: build a TracerProvider that exports OTLP to the Vigil receiver,
stamps run/eval identity on every span, and flushes + shuts down on process exit."""

from __future__ import annotations

import atexit
import threading

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

from .config import Config
from .content import ContentCapture
from .processor import IdentitySpanProcessor

_PROVIDER: TracerProvider | None = None
_CONFIG: Config | None = None
_CONTENT: ContentCapture | None = None
_lock = threading.Lock()


def _build_resource(cfg: Config) -> Resource:
    attrs = {"service.name": cfg.service_name}
    if cfg.service_version:
        attrs["service.version"] = cfg.service_version
    if cfg.agent_id:
        attrs["vigil.agent.id"] = cfg.agent_id
    if cfg.agent_version:
        attrs["vigil.agent.version"] = cfg.agent_version
    if cfg.git_sha:
        attrs["vigil.agent.git_sha"] = cfg.git_sha
    return Resource.create(attrs)


def _build_exporter(cfg: Config) -> SpanExporter:
    """Construct the OTLP exporter for the configured protocol. Imported lazily so the
    exporter packages are only required when actually exporting over OTLP."""
    if cfg.protocol == "http":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        return OTLPSpanExporter(endpoint=cfg.endpoint) if cfg.endpoint else OTLPSpanExporter()
    if cfg.protocol == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        endpoint = cfg.endpoint or "http://localhost:4317"
        return OTLPSpanExporter(endpoint=endpoint, insecure=True)
    raise ValueError(f"unknown VIGIL_OTLP_PROTOCOL {cfg.protocol!r} (expected 'grpc' or 'http')")


def init(
    config: Config | None = None,
    *,
    exporter: SpanExporter | None = None,
    set_global: bool = True,
    **overrides,
) -> TracerProvider:
    """Initialize Vigil tracing and return the TracerProvider.

    Builds a TracerProvider with the resource identity, an IdentitySpanProcessor, and a
    BatchSpanProcessor around the OTLP exporter (or the provided ``exporter``, e.g. for
    tests). Registers an atexit handler that flushes and shuts the provider down so a
    short-lived process does not drop buffered spans.
    """
    global _PROVIDER, _CONFIG, _CONTENT

    cfg = (config or Config.from_env()).with_overrides(**overrides)
    provider = TracerProvider(resource=_build_resource(cfg), shutdown_on_exit=False)
    provider.add_span_processor(IdentitySpanProcessor())
    provider.add_span_processor(
        BatchSpanProcessor(exporter if exporter is not None else _build_exporter(cfg))
    )

    with _lock:
        _PROVIDER = provider
        _CONFIG = cfg
        _CONTENT = ContentCapture(cfg)

    if set_global:
        trace.set_tracer_provider(provider)

    atexit.register(_shutdown_provider, provider)
    return provider


_shutdown_done: set[int] = set()


def _shutdown_provider(provider: TracerProvider) -> None:
    """Flush and shut a provider down exactly once (idempotent for atexit + explicit calls)."""
    with _lock:
        if id(provider) in _shutdown_done:
            return
        _shutdown_done.add(id(provider))
    try:
        provider.force_flush()
    finally:
        provider.shutdown()


def shutdown() -> None:
    """Flush and shut the current provider down explicitly (also runs at exit)."""
    if _PROVIDER is not None:
        _shutdown_provider(_PROVIDER)


def get_tracer():
    if _PROVIDER is not None:
        return _PROVIDER.get_tracer("vigil")
    return trace.get_tracer("vigil")


def get_config() -> Config:
    return _CONFIG if _CONFIG is not None else Config.from_env()


def get_content_capture() -> ContentCapture:
    global _CONTENT
    if _CONTENT is None:
        _CONTENT = ContentCapture(get_config())
    return _CONTENT
