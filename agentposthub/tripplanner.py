"""Site trip-planner tool: real calls to 1000usdinchina.com's own AI.

The site's /ai-plan page is powered by POST /api/ai/chat — a Cloudflare
Workers AI agent loop that returns an SSE stream of typed events. This
module is the Python-side tool wrapper around that contract:

  POST {site}/api/ai/chat  {"message": str<=2000, "locale": "zh"|"en"}
  SSE events:
    progress{text}                          interim ticks, ignored
    reply{text}                             assistant prose
    draft{state,breakdown,routeNames,totalUsd,caveat?}
                                            the structured itinerary
    fallback{formUrl}                       model gave up -> static form
    error{code}                             rate_limited|capacity|...
    done                                    stream end

Contract enforced here: every call gets a timeout, an event whitelist,
and a fail-soft return — a planner outage degrades the grounding pack
with a visible marker instead of killing the run.
"""
from __future__ import annotations

import json

import httpx

from .config import Settings, get_settings

DEFAULT_SITE = "https://1000usdinchina.com"
TIMEOUT_S = 120.0          # the Workers agent loop can take a while
MAX_MESSAGE_CHARS = 2000


class PlannerError(RuntimeError):
    pass


def plan_trip(message: str, locale: str = "zh",
              settings: Settings | None = None) -> dict:
    """Ask the site's AI planner for an itinerary.

    Returns {"ok": True, "reply": str, "draft": {...}|None} on success,
    {"ok": False, "error": code} on contract failures — never raises
    for expected remote failures, so callers can degrade gracefully.
    """
    settings = settings or get_settings()
    site = getattr(settings, "reference_site", None) or DEFAULT_SITE
    message = message.strip()[:MAX_MESSAGE_CHARS]
    if not message:
        return {"ok": False, "error": "empty_message"}

    events = {"reply": [], "draft": None}
    try:
        with httpx.Client(timeout=TIMEOUT_S) as client:
            with client.stream(
                "POST", f"{site}/api/ai/chat",
                json={"message": message, "locale": locale},
                headers={"Accept": "text/event-stream"},
            ) as resp:
                if resp.status_code == 429:
                    return {"ok": False, "error": "rate_limited"}
                if resp.status_code != 200:
                    return {"ok": False,
                            "error": f"http_{resp.status_code}"}
                err_code = _consume_sse(resp, events)
                if err_code:
                    return {"ok": False, "error": err_code}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"transport: {str(exc)[:80]}"}

    if events["draft"] is None and not events["reply"]:
        return {"ok": False, "error": "empty_response"}
    return {"ok": True,
            "reply": "\n".join(events["reply"]).strip(),
            "draft": events["draft"]}


def _consume_sse(resp: httpx.Response, events: dict) -> str | None:
    """Parse `data: {json}\\n\\n` frames; collect reply/draft; return an
    error code if the stream signalled one."""
    buf = ""
    for chunk in resp.iter_text():
        buf += chunk
        while "\n\n" in buf:
            frame, buf = buf.split("\n\n", 1)
            line = next((l for l in frame.split("\n")
                         if l.startswith("data: ")), None)
            if not line:
                continue
            try:
                evt = json.loads(line[6:])
            except json.JSONDecodeError:
                continue  # malformed frame: skip, keep the stream alive
            kind = evt.get("type")
            if kind == "reply":
                events["reply"].append(evt.get("text", ""))
            elif kind == "draft":
                events["draft"] = {
                    "total_usd": evt.get("totalUsd"),
                    "route_names": evt.get("routeNames", []),
                    "breakdown": evt.get("breakdown"),
                    "caveat": evt.get("caveat"),
                }
            elif kind == "fallback":
                return "planner_fallback"
            elif kind == "error":
                return evt.get("code", "internal_error")
            # progress / done: nothing to collect
    return None
