"""A2A client for the reference site's trip-planner agent.

The planner is itself an LLM agent (Cloudflare Workers AI loop); we
talk to it with the A2A protocol (agent-to-agent), not a private HTTP
contract:

  1. GET  {site}/.well-known/agent-card.json   - capability discovery,
     cached per process; the card's url tells us the RPC endpoint
  2. POST {endpoint}  JSON-RPC 2.0  message/send
       params.message.parts = [{kind:"text", text: brief}]
     response result.parts = [{kind:"text", reply},
                              {kind:"data",  structured plan}]

Mapping into the grounding contract is unchanged downstream:
  {"ok": True, "reply": str, "draft": {total_usd, route_names, ...}}
  {"ok": False, "error": rate_limited|no_draft|http_N|transport:...}

A reply without a data part means the agent asked a clarifying
question (no usable plan) -> ok=False error=no_draft.
"""
from __future__ import annotations

import httpx

from .config import Settings, get_settings

TIMEOUT_S = 120.0
MAX_MESSAGE_CHARS = 2000

_card_cache: dict = {}


def _card_url(site: str) -> str:
    return f"{site.rstrip('/')}/.well-known/agent-card.json"


def _discover_endpoint(site: str, transport=None) -> tuple[str, str]:
    """Agent Card discovery; falls back to {site}/a2a when the card is
    absent (older deployments). Returns (endpoint, agent_name)."""
    if site in _card_cache:
        return _card_cache[site]
    try:
        with httpx.Client(timeout=15, transport=transport) as c:
            r = c.get(_card_url(site))
        if r.status_code == 200:
            card = r.json()
            entry = (str(card.get("url") or f"{site.rstrip('/')}/a2a"),
                     str(card.get("name") or "unknown-agent"))
            _card_cache[site] = entry
            return entry
    except httpx.HTTPError:
        pass
    entry = (f"{site.rstrip('/')}/a2a", "fallback-agent")
    _card_cache[site] = entry
    return entry


def plan_trip(message: str, locale: str = "zh",
              settings: Settings | None = None) -> dict:
    """Ask the planner agent for an itinerary via A2A message/send.

    Returns {"ok": True, "reply", "draft"} or {"ok": False, "error"} —
    never raises for expected remote failures."""
    settings = settings or get_settings()
    message = message.strip()[:MAX_MESSAGE_CHARS]
    if not message:
        return {"ok": False, "error": "empty_message"}

    endpoint, agent = _discover_endpoint(settings.reference_site)
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "message/send",
        "params": {
            "message": {"role": "user",
                        "parts": [{"kind": "text", "text": message}]},
            "metadata": {"locale": locale},
        },
    }
    try:
        with httpx.Client(timeout=TIMEOUT_S) as c:
            r = c.post(endpoint, json=payload)
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"transport: {str(exc)[:80]}"}
    if r.status_code == 429:
        return {"ok": False, "error": "rate_limited"}
    if r.status_code != 200:
        return {"ok": False, "error": f"http_{r.status_code}"}

    body = r.json()
    rpc_err = body.get("error")
    if rpc_err:
        msg = str(rpc_err.get("message", ""))
        if "rate" in msg:
            return {"ok": False, "error": "rate_limited"}
        if msg in ("capacity", "internal_error", "service unavailable"):
            return {"ok": False, "error": msg.replace(" ", "_")}
        return {"ok": False, "error": f"rpc: {msg[:60]}"}

    parts = (body.get("result") or {}).get("parts") or []
    reply = "\n".join(p.get("text", "") for p in parts
                      if p.get("kind") == "text").strip()
    data = next((p.get("data") for p in parts if p.get("kind") == "data"),
                None)
    if data is None:
        if not reply:
            return {"ok": False, "error": "empty_response"}
        return {"ok": False, "error": "no_draft",
                "reply": reply[:200]}
    return {"ok": True, "reply": reply or f"[{agent}]",
            "draft": {
                "total_usd": data.get("totalUsd"),
                "route_names": data.get("routeNames", []),
                "breakdown": data.get("breakdown"),
                "caveat": data.get("caveat"),
            }}

