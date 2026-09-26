from vigil.config import Config
from vigil.content import ContentCapture


class FakeSpan:
    def __init__(self):
        self.events = []

    def add_event(self, name, attributes=None):
        self.events.append((name, attributes or {}))


def test_truncation_marks_truncated():
    cc = ContentCapture(Config(max_content_bytes=5, capture_content_eval=True))
    span = FakeSpan()
    recorded = cc.record_event(span, "e", "abcdefghij", run_kind="eval")
    assert recorded is True
    _, attrs = span.events[0]
    assert attrs["vigil.content.truncated"] is True
    assert len(attrs["content"].encode("utf-8")) <= 5


def test_short_content_not_truncated():
    cc = ContentCapture(Config(max_content_bytes=100, capture_content_eval=True))
    span = FakeSpan()
    cc.record_event(span, "e", "hello", run_kind="eval")
    _, attrs = span.events[0]
    assert attrs["vigil.content.truncated"] is False
    assert attrs["content"] == "hello"


def test_redaction_applied_before_export():
    cc = ContentCapture(
        Config(capture_content_eval=True, redactor=lambda t: t.replace("secret", "***"))
    )
    span = FakeSpan()
    cc.record_event(span, "e", "my secret token", run_kind="eval")
    assert "secret" not in span.events[0][1]["content"]
    assert "***" in span.events[0][1]["content"]


def test_live_capture_is_opt_in():
    off = ContentCapture(Config(capture_content_live=False))
    on = ContentCapture(Config(capture_content_live=True))
    s_off, s_on = FakeSpan(), FakeSpan()
    assert off.record_event(s_off, "e", "x", run_kind="live") is False
    assert on.record_event(s_on, "e", "x", run_kind="live") is True
    assert len(s_off.events) == 0
    assert len(s_on.events) == 1


def test_eval_capture_on_by_default():
    cc = ContentCapture(Config())  # defaults: eval True, live False
    span = FakeSpan()
    assert cc.record_event(span, "e", "x", run_kind="eval") is True


def test_failing_redactor_drops_content():
    def boom(_):
        raise ValueError("nope")

    cc = ContentCapture(Config(capture_content_eval=True, redactor=boom))
    span = FakeSpan()
    cc.record_event(span, "e", "leak me", run_kind="eval")
    # Content dropped to empty and marked truncated, never the raw text.
    assert span.events[0][1]["content"] == ""
    assert span.events[0][1]["vigil.content.truncated"] is True
