"""Dev-only LLM cache backed by Redis (design §7, approved change on cache-hit stamping).

Ordering: the cache wraps the **raw** Anthropic client, and ``vigil.wrap`` wraps the cache
(``vigil.wrap(RedisLLMCache(raw, ...))``). So a Vigil gen_ai span is created for **every**
call, cached or not — and on a cache hit the cache stamps ``vigil.cache.hit=true`` on that
span (it runs while the span is current) and returns the stored response, which Vigil records
with real token/model attributes. That lets ClickHouse cost aggregates exclude cached spans
(``WHERE vigil_cache_hit = false``) instead of the hit vanishing entirely.

Guards (both enforced):
- **Refused for measurement.** ``mode='measurement'`` with caching enabled raises at
  construction — and again before any cached *read* — because a cached measurement hides the
  run-to-run variance regression detection depends on.
- **Off by default** (``cache_mode='off'`` passes everything through, stores nothing).

Streaming calls are passed straight through (not cached); the eval agents use non-streaming.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from opentelemetry import trace

_READ_MODES = ("read", "read_write")


class CacheRefusedError(RuntimeError):
    """Raised when caching is requested for a measurement run."""


def check_refusal(mode: str, cache_mode: str) -> None:
    if mode == "measurement" and cache_mode in _READ_MODES:
        raise CacheRefusedError(
            "LLM caching is refused for measurement runs: a cached measurement would serve "
            "identical responses across runs and hide run-to-run variance (design §7). "
            "Use mode='development' for cached runs, or cache_mode='off'."
        )


# --- (de)serialization of a messages.create response ---------------------------------


class _Ns:
    """A tiny duck-typed stand-in for an Anthropic response/usage/content block."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _serialize_block(block: Any) -> dict:
    btype = _get(block, "type")
    if btype == "text":
        return {"type": "text", "text": _get(block, "text", "")}
    if btype == "tool_use":
        return {
            "type": "tool_use",
            "name": _get(block, "name"),
            "input": _get(block, "input", {}),
            "id": _get(block, "id"),
        }
    return {"type": btype}


def _get(obj: Any, name: str, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _serialize_response(resp: Any) -> dict:
    usage = _get(resp, "usage")
    return {
        "model": _get(resp, "model"),
        "stop_reason": _get(resp, "stop_reason"),
        "usage": {
            "input_tokens": _get(usage, "input_tokens", 0) or 0,
            "output_tokens": _get(usage, "output_tokens", 0) or 0,
        },
        "content": [_serialize_block(b) for b in (_get(resp, "content", []) or [])],
    }


def _deserialize_response(doc: dict) -> _Ns:
    usage = _Ns(
        input_tokens=doc["usage"]["input_tokens"], output_tokens=doc["usage"]["output_tokens"]
    )
    content = [_Ns(**b) for b in doc.get("content", [])]
    return _Ns(
        model=doc.get("model"), stop_reason=doc.get("stop_reason"), usage=usage, content=content
    )


# --- cache key -----------------------------------------------------------------------


def _to_jsonable(obj: Any):
    # Normalize content blocks (real Anthropic objects on a miss, _Ns on a later hit) through
    # the same shape so the key is stable across runs regardless of object type.
    if _get(obj, "type") in ("text", "tool_use"):
        return _serialize_block(obj)
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            pass
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return repr(obj)


def cache_key(kwargs: dict) -> str:
    """sha256 over the canonical request: model, decoding params, system, tools, messages."""
    payload = {
        "model": kwargs.get("model"),
        "max_tokens": kwargs.get("max_tokens"),
        "temperature": kwargs.get("temperature"),
        "top_p": kwargs.get("top_p"),
        "top_k": kwargs.get("top_k"),
        "system": kwargs.get("system"),
        "tools": kwargs.get("tools"),
        "messages": kwargs.get("messages"),
    }
    blob = json.dumps(payload, sort_keys=True, default=_to_jsonable, ensure_ascii=False)
    return "vigil:evalcache:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _mark_cache_hit() -> None:
    span = trace.get_current_span()
    try:
        span.set_attribute("vigil.cache.hit", True)
    except Exception:
        pass  # never let telemetry stamping break a call


# --- the cache wrapper ---------------------------------------------------------------


class _CachedMessages:
    def __init__(self, raw_messages, store, *, mode, cache_mode, ttl):
        self._raw = raw_messages
        self._store = store
        self._mode = mode
        self._cache_mode = cache_mode
        self._ttl = ttl

    async def create(self, **kwargs):
        # Streaming and cache-off both pass straight through.
        if self._cache_mode == "off" or kwargs.get("stream"):
            return await self._raw.create(**kwargs)

        key = cache_key(kwargs)
        if self._cache_mode in _READ_MODES:
            check_refusal(self._mode, self._cache_mode)  # re-check before any cached read
            raw = await self._store.get(key)
            if raw is not None:
                _mark_cache_hit()
                doc = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                return _deserialize_response(doc)

        resp = await self._raw.create(**kwargs)
        if self._cache_mode == "read_write":
            await self._store.set(key, json.dumps(_serialize_response(resp)), ex=self._ttl)
        return resp

    def __getattr__(self, name):
        return getattr(self._raw, name)


class RedisLLMCache:
    """Wraps a raw async Anthropic client's ``messages`` with a Redis-backed cache. Construct
    it, then hand it to ``vigil.wrap``."""

    def __init__(
        self, raw_client, store, *, mode: str, cache_mode: str = "read_write", ttl: int = 86400
    ):
        check_refusal(mode, cache_mode)
        self._raw = raw_client
        self.messages = _CachedMessages(
            raw_client.messages, store, mode=mode, cache_mode=cache_mode, ttl=ttl
        )

    def __getattr__(self, name):
        return getattr(self._raw, name)


def make_store(redis_url: str):
    """Build an async Redis client for the cache store. Imported lazily so a no-cache run
    doesn't require redis to be installed/reachable."""
    import redis.asyncio as aioredis

    return aioredis.from_url(redis_url)
