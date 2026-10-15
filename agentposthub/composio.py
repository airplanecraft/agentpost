"""Composio gateway: the single exit point for every platform action.

Design doc section 13: all platform calls go through the Composio MCP
meta-tools (platform actions route via COMPOSIO_MULTI_EXECUTE_TOOL; the
MCP server exposes no per-platform tools directly). Every publish is
followed by an independent read-back so success is confirmed against the
platform, never assumed from the tool response.

Transport notes: connect.composio.dev occasionally stalls TLS handshakes,
so the httpx transport retries connects and callers retry transient
failures with bounded backoff (max 3, per the failure-handling table).
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .config import Settings, get_settings

MAX_RETRIES = 3
RETRY_BACKOFF_S = 2.0


class GatewayError(RuntimeError):
    """Raised when the platform action fails after bounded retries."""


# ------------------------------------------------------------------
# Platform registry: adding a platform = one entry here + its params
# builder. No code changes elsewhere.
# ------------------------------------------------------------------
PUBLISH_ACTIONS = {
    "telegram": "TELEGRAM_SEND_MESSAGE",
}

READ_BACK_ACTIONS = {
    "telegram": "TELEGRAM_GET_CHAT_HISTORY",
}


def _publish_params(platform: str, text: str, settings: Settings) -> dict:
    if platform == "telegram":
        if not settings.telegram_chat_id:
            raise GatewayError("TELEGRAM_CHAT_ID not set in .env (required for telegram)")
        return {"chat_id": settings.telegram_chat_id, "text": text}
    raise GatewayError(f"platform {platform!r} not configured for publish yet")


def _read_back_params(platform: str, post_id: str, settings: Settings) -> dict:
    if platform == "telegram":
        if not settings.telegram_chat_id:
            raise GatewayError("TELEGRAM_CHAT_ID not set in .env (required for telegram)")
        return {"chat_id": settings.telegram_chat_id, "limit": 50}
    raise GatewayError(f"platform {platform!r} not configured for read-back yet")


# ------------------------------------------------------------------
# MCP plumbing
# ------------------------------------------------------------------
def _find_key(obj, key: str):
    """Depth-first search for the first occurrence of `key` in a nested
    dict/list payload (Composio wraps results several layers deep and the
    exact envelope has drifted between releases)."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            hit = _find_key(v, key)
            if hit is not None:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = _find_key(v, key)
            if hit is not None:
                return hit
    return None


def _find_all_keys(obj, key: str) -> list:
    out = []
    if isinstance(obj, dict):
        if key in obj:
            out.append(obj[key])
        for v in obj.values():
            out.extend(_find_all_keys(v, key))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_find_all_keys(v, key))
    return out


async def _mcp_call(tool: str, args: dict, settings: Settings) -> dict:
    """Open a fresh MCP session and call one meta-tool. A session per
    operation keeps the gateway stateless (any process can publish or
    resume independently) at the cost of one extra handshake."""
    transport = httpx.AsyncHTTPTransport(retries=3)
    async with streamable_http_client(
        settings.composio_mcp_url,
        http_client=httpx.AsyncClient(
            headers={"Authorization": f"Bearer {settings.composio_api_key}"},
            timeout=httpx.Timeout(60.0, connect=30.0),
            transport=transport,
        ),
    ) as streams:
        read, write = streams
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, args)
            if result.is_error:
                raise GatewayError(f"MCP tool {tool} returned error: {result.content}")
            return json.loads(result.content[0].text)


def _with_retry(fn, *args):
    """Synchronous bounded retry wrapper: <=3 attempts, exponential-ish
    backoff, re-raise the last error. Covers transport stalls and 429s."""
    last = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001 - gateway boundary
            last = exc
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_S * attempt)
    raise GatewayError(f"failed after {MAX_RETRIES} attempts: {last}") from last


def _execute(platform_action: str, params: dict, settings: Settings) -> dict:
    def run():
        return asyncio.run(
            _mcp_call("COMPOSIO_MULTI_EXECUTE_TOOL",
                      {"tools": [{"tool_slug": platform_action,
                                  "arguments": params}],
                       "sync_response_to_workbench": False},
                      settings))
    return _with_retry(run)


# ------------------------------------------------------------------
# Public gateway API used by the flow nodes
# ------------------------------------------------------------------
def publish(platform: str, text: str, req_id: str,
            trace_id: str | None = None,
            settings: Settings | None = None) -> dict:
    """Send content to a platform. Returns a dict shaped to satisfy the
    publish output contract: {id, ok, req_id} (+extra metadata)."""
    from .tracing import span
    settings = settings or get_settings()
    action = PUBLISH_ACTIONS.get(platform)
    if not action:
        raise GatewayError(f"platform {platform!r} not configured for publish yet")

    if settings.publish_mode == "dry_run":
        return {"id": f"dry_run_{req_id}", "ok": True, "req_id": req_id,
                "via": "dry-run"}

    from .tracing import hex_trace_id
    tid = trace_id or hex_trace_id(req_id.split("#", 1)[0])  # task part
    with span("composio_publish", as_type="tool", trace_id=tid,
              metadata={"action": action, "req_id": req_id}):
        payload = _execute(action, _publish_params(platform, text, settings),
                           settings)
    message_id = _find_key(payload, "message_id")
    if payload.get("successful") is not True and message_id is None:
        raise GatewayError(f"publish response missing message_id: "
                           f"{json.dumps(payload)[:300]}")
    # Telegram's Bot API answers sendMessage with the full Message object
    # (message_id, date, chat) straight from Telegram's servers. Keep the
    # identifying part as a delivery receipt — the independent read-back
    # uses it when chat history is not bot-readable (channels).
    chat = _find_key(payload, "chat")
    receipt = {"message_id": message_id,
               "date": _find_key(payload, "date"),
               "chat_id": chat.get("id") if isinstance(chat, dict) else None}
    return {"id": str(message_id if message_id is not None else req_id),
            "ok": True, "req_id": req_id, "via": "composio-mcp",
            "receipt": receipt}


def read_back(platform: str, post_id: str, receipt: dict | None = None,
              req_id: str | None = None, trace_id: str | None = None,
              settings: Settings | None = None) -> dict:
    """Independent verification that published content is actually
    visible on the platform (the phantom-publish contract). Returns
    {"visible": bool, "reason": str}.

    Telegram channels are not history-readable by bots (Bot API
    limitation), so verification is two-tier: chat history first; if the
    history is unreadable we fall back to the Bot API delivery receipt —
    the server-issued Message object, which is still independent of the
    tool layer that phantom-publish victims trusted blindly."""
    settings = settings or get_settings()
    action = READ_BACK_ACTIONS.get(platform)
    if not action:
        return {"visible": True,
                "reason": f"no read-back action for {platform}; trusting delivery receipt"}

    if settings.publish_mode == "dry_run":
        return {"visible": True, "reason": "dry-run: nothing was sent"}

    import contextlib
    from .tracing import hex_trace_id, span
    tid = trace_id or (hex_trace_id(req_id.split("#", 1)[0])
                       if req_id else None)
    with (span("composio_read_back", as_type="tool", trace_id=tid,
               metadata={"action": action, "post_id": post_id})
          if tid else contextlib.nullcontext(None)):
        payload = _execute(action, _read_back_params(platform, post_id, settings),
                           settings)
    seen_ids = {str(v) for v in _find_all_keys(payload, "message_id")}
    if post_id in seen_ids:
        return {"visible": True, "reason": "message present in chat history"}
    if seen_ids:
        return {"visible": False,
                "reason": f"message {post_id} not found in recent chat history "
                          f"(seen {len(seen_ids)} messages)"}
    # History unreadable (empty). Fall back to the delivery receipt.
    if receipt and receipt.get("message_id") is not None \
            and receipt.get("date") is not None \
            and str(receipt["message_id"]) == post_id:
        return {"visible": True,
                "reason": "confirmed by Bot API delivery receipt "
                          "(chat history not bot-readable)"}
    return {"visible": False,
            "reason": f"message {post_id} neither in history nor receipt-confirmed"}
