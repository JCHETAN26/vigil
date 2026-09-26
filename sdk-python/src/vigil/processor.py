"""A span processor that stamps run/eval identity onto every span at start.

Because the identity is denormalized onto every span (design doc §2.2), analytical
queries filter by run/agent without self-joins to the root. This processor reads the
ambient RunContext at span start and sets the vigil.* / gen_ai.conversation.id attributes,
so any span opened inside an ``agent_run`` carries them automatically."""

from __future__ import annotations

from opentelemetry.context import Context
from opentelemetry.sdk.trace import Span, SpanProcessor

from .context import current_role, current_run


class IdentitySpanProcessor(SpanProcessor):
    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        rc = current_run()
        role = current_role()
        if rc is None and role is None:
            return
        attrs: dict = {}
        if rc is not None:
            attrs["vigil.run.id"] = rc.run_id
            attrs["vigil.run.kind"] = rc.run_kind
            if rc.eval_run_id:
                attrs["vigil.eval.run_id"] = rc.eval_run_id
            if rc.eval_case_id:
                attrs["vigil.eval.case_id"] = rc.eval_case_id
            if rc.trial is not None:  # trial 0 is valid, so test presence, not truthiness
                attrs["vigil.eval.trial"] = rc.trial
            if rc.session_id:
                attrs["gen_ai.conversation.id"] = rc.session_id
            if rc.dataset:
                attrs["vigil.eval.dataset"] = rc.dataset
        if role:
            attrs["vigil.role"] = role
        try:
            span.set_attributes(attrs)
        except Exception:
            # A processor must never break span creation.
            pass

    def on_end(self, span) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True
