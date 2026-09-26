import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import vigil


@pytest.fixture(scope="session")
def provider_and_exporter():
    """Initialize Vigil once for the test session with an in-memory exporter. The OTel
    global provider can only be set once per process, so this is session-scoped."""
    exporter = InMemorySpanExporter()
    cfg = vigil.Config(
        service_name="test",
        agent_id="itest",
        agent_version="v1",
        capture_content_eval=True,
        capture_content_live=False,
        max_content_bytes=8192,
    )
    provider = vigil.init(config=cfg, exporter=exporter, set_global=True)
    yield provider, exporter


@pytest.fixture
def spans(provider_and_exporter):
    """Return a getter for finished spans, isolating each test: flush and clear any
    buffered spans before the test, and force-flush before reading."""
    provider, exporter = provider_and_exporter
    provider.force_flush()
    exporter.clear()

    def _get():
        provider.force_flush()
        return exporter.get_finished_spans()

    return _get
