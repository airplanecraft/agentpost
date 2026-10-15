"""Langfuse tracing: spans for the graph and the raw LLM/MCP calls.

Self-hosted by default (http://localhost:3000 — see the local docker
compose stack). Tracing is strictly opt-in: without keys in .env every
helper here is a no-op, so the system runs identically offline.

Linkage: the LangGraph callback handler and the manual spans share a
deterministic trace_id (= task_id), so a task's graph-node spans and
its DeepSeek generation span land in one trace.
"""
from __future__ import annotations

import hashlib
import time
from contextlib import contextmanager

from .config import get_settings

_client = None


def hex_trace_id(task_id: str) -> str:
    """Langfuse/OTel trace ids must be 32-char hex. Deterministic map
    from task_id keeps spans, traces and scores correlated."""
    return hashlib.sha1(task_id.encode()).hexdigest()[:32]


def tracing_enabled() -> bool:
    s = get_settings()
    return bool(s.langfuse_public_key and s.langfuse_secret_key)


def get_client():
    """Cached Langfuse client; None when not configured (no-op mode)."""
    global _client
    if not tracing_enabled():
        return None
    if _client is None:
        from langfuse import Langfuse
        s = get_settings()
        _client = Langfuse(public_key=s.langfuse_public_key,
                           secret_key=s.langfuse_secret_key,
                           host=s.langfuse_host)
    return _client


def observe_config(task_id: str) -> dict:
    """LangGraph invoke config carrying the callback handler, with the
    trace pinned to task_id so manual spans join the same trace."""
    if not tracing_enabled():
        return {}
    from langfuse.langchain import CallbackHandler
    handler = CallbackHandler(trace_context={"trace_id": hex_trace_id(task_id)})
    return {"callbacks": [handler]}


@contextmanager
def span(name: str, *, as_type: str = "span", input=None,
         trace_id: str | None = None, metadata: dict | None = None):
    """Manual span for calls the callback handler cannot see (raw
    urllib/httpx). No-op when tracing is off."""
    lf = get_client()
    if lf is None:
        yield None
        return
    kw = {"name": name, "as_type": as_type}
    if input is not None:
        kw["input"] = input
    if metadata:
        kw["metadata"] = metadata
    if trace_id:
        kw["trace_context"] = {"trace_id": hex_trace_id(trace_id)}
    with lf.start_as_current_observation(**kw) as obs:
        t0 = time.time()
        try:
            yield obs
        except Exception as exc:
            obs.update(output={"error": str(exc)[:200]},
                       metadata={**(metadata or {}),
                                 "duration_s": round(time.time() - t0, 3)})
            raise
        else:
            obs.update(metadata={**(metadata or {}),
                                 "duration_s": round(time.time() - t0, 3)})


def score(trace_id: str, name: str, value: float, *,
          session_id: str | None = None, comment: str = "") -> None:
    """Push an eval score; no-op when tracing is off.

    Langfuse's API is XOR: a score attaches to a trace_id OR a
    session_id, never both — sending both rejects the whole ingestion
    batch server-side, taking the spans down with it."""
    lf = get_client()
    if lf is None:
        return
    kw = {"name": name, "value": value}
    if trace_id:
        kw["trace_id"] = hex_trace_id(trace_id)
    elif session_id:
        kw["session_id"] = session_id
    if comment:
        kw["comment"] = comment[:300]
    lf.create_score(**kw)


def flush() -> None:
    if _client is not None:
        _client.shutdown()
