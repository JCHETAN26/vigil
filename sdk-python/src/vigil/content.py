"""Content capture: record prompts/completions as span events under the configured
policy, with redaction and truncation applied before export."""

from __future__ import annotations

import json
from typing import Any

from .config import Config
from .context import current_run_kind


class ContentCapture:
    def __init__(self, config: Config):
        self._cfg = config

    def enabled_for(self, run_kind: str) -> bool:
        if run_kind == "eval":
            return self._cfg.capture_content_eval
        # live and unknown follow the opt-in live setting.
        return self._cfg.capture_content_live

    def _to_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        return json.dumps(content, ensure_ascii=False, sort_keys=True, default=str)

    def _process(self, text: str) -> tuple[str, bool]:
        """Apply the redaction hook, then truncate to the byte limit. Returns
        (text, truncated)."""
        if self._cfg.redactor is not None:
            try:
                text = self._cfg.redactor(text)
            except Exception:
                # A failing redactor must not silently leak the raw content; drop it.
                return "", True
        data = text.encode("utf-8")
        if len(data) <= self._cfg.max_content_bytes:
            return text, False
        # Truncate on a byte boundary, then decode ignoring a split multibyte char.
        return data[: self._cfg.max_content_bytes].decode("utf-8", "ignore"), True

    def record_event(
        self,
        span,
        name: str,
        content: Any,
        *,
        extra: dict | None = None,
        run_kind: str | None = None,
    ) -> bool:
        """Record ``content`` as a span event named ``name`` if capture is enabled for the
        current run kind. Returns whether an event was recorded."""
        rk = run_kind or current_run_kind()
        if not self.enabled_for(rk):
            return False
        text, truncated = self._process(self._to_text(content))
        attrs: dict[str, Any] = {"content": text, "vigil.content.truncated": truncated}
        if extra:
            attrs.update({k: v for k, v in extra.items() if v is not None})
        span.add_event(name, attributes=attrs)
        return True
