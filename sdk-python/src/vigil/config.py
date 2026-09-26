"""Configuration for the Vigil SDK, resolved from explicit values or the environment."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field, replace

# A redaction hook: given a piece of content about to be exported, return the
# redacted version. Applied before truncation and before export.
Redactor = Callable[[str], str]


def _env_bool(key: str, default: bool) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_int(key: str, default: int) -> int:
    v = os.getenv(key)
    if v is None:
        return default
    try:
        return int(v)
    except ValueError:
        return default


@dataclass
class Config:
    """Vigil SDK configuration.

    Content capture follows the OpenTelemetry GenAI conventions (prompts/completions as
    span events). It is always on for eval runs (regression detection needs the full
    record) and opt-in for live runs. Payloads over ``max_content_bytes`` are truncated
    and marked; ``redactor`` runs on every captured string before truncation and export.
    """

    # Identity (resource attributes; see design doc §2).
    service_name: str = "unknown-service"
    service_version: str | None = None
    agent_id: str | None = None
    agent_version: str | None = None
    git_sha: str | None = None

    # Exporter.
    protocol: str = "grpc"  # "grpc" (default, :4317) or "http" (:4318)
    endpoint: str | None = None

    # Content capture.
    capture_content_eval: bool = True
    capture_content_live: bool = False
    max_content_bytes: int = 8192
    redactor: Redactor | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            service_name=os.getenv(
                "VIGIL_SERVICE_NAME", os.getenv("OTEL_SERVICE_NAME", "unknown-service")
            ),
            service_version=os.getenv("VIGIL_SERVICE_VERSION"),
            agent_id=os.getenv("VIGIL_AGENT_ID"),
            agent_version=os.getenv("VIGIL_AGENT_VERSION"),
            git_sha=os.getenv("VIGIL_AGENT_GIT_SHA"),
            protocol=os.getenv("VIGIL_OTLP_PROTOCOL", "grpc").lower(),
            endpoint=os.getenv("VIGIL_OTLP_ENDPOINT", os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")),
            capture_content_eval=_env_bool("VIGIL_CAPTURE_CONTENT_EVAL", True),
            capture_content_live=_env_bool("VIGIL_CAPTURE_CONTENT_LIVE", False),
            max_content_bytes=_env_int("VIGIL_MAX_CONTENT_BYTES", 8192),
        )

    def with_overrides(self, **overrides) -> Config:
        """Return a copy with the given non-None fields overridden."""
        clean = {k: v for k, v in overrides.items() if v is not None}
        return replace(self, **clean) if clean else self
