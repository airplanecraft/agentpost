"""AgentPostHub publish flow (LangGraph).

Pipeline: retrieve -> generate -> dedup_guard -> [HUMAN approve interrupt]
          -> policy_validator -> publish -> verify -> record

Mechanisms: durable interrupt/resume (checkpointed, can hang for days),
soft vs hard tool rules (visibility gating vs prompt policy),
memory-driven trust gating (incident -> downgrade -> next decision
changes), output-contract enforcement on platform responses, idempotent
ledger writes, bounded failure handling.

Configuration: .env at the project root (see .env.example). Publish goes
through the Composio MCP gateway (agentposthub.composio) — the only exit
point for platform actions — and every publish is verified by an
independent read-back.
"""
from __future__ import annotations

import hashlib
import json
import operator
import sqlite3
import time
import urllib.request as rq
from functools import lru_cache
from typing import Annotated, Literal, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.store.sqlite import SqliteStore
from langgraph.types import Command, Send, interrupt

from . import composio
from .composio import RETRY_BACKOFF_S, GatewayError
from .config import Settings, get_settings, ssl_ctx


class FlowState(TypedDict):
    """Graph state ledger. Single-value fields use the default overwrite
    reducer; parallel-collected fields declare append reducers so Send
    fan-out results never clobber each other."""
    task_id: str
    trace_id: str                  # observability key, persisted with state
    platforms: list                # target platforms for this task
    brief: dict                    # route brief: cities/days/budget/notes

    grounding: dict                # RAG grounding pack (shared, fetched once)
    drafts: Annotated[list[dict], operator.add]   # one per platform (Send)
    approval: str                  # pending|approved|rejected|edited
    retry_count: int               # bounded publish retries after failure
    escalation_action: str         # retry|drop (set by the escalation gate)
    retry_platforms: list          # failed platforms targeted by a retry
    budget_usd: float              # per-task spend cap
    spend_usd: Annotated[list[dict], operator.add]   # per-call cost ledger

    platform_results: Annotated[list[dict], operator.add]
    verifications: Annotated[list[dict], operator.add]
    failures: Annotated[list[dict], operator.add]


# ------------------------------------------------------------------
# Tool layer: declaration (contract) vs executor (implementation)
# ------------------------------------------------------------------
CREATE_IMAGE_TOOL = {
    "type": "function",
    "function": {
        "name": "create_image_content",
        "description": ("Generate an image-based post (cover/collage) "
                        "for the platform. Call ONLY if images are "
                        "allowed on this platform and would help."),
        "parameters": {"type": "object",
                       "properties": {
                           "style": {"type": "string",
                                     "enum": ["photo_collage", "infographic",
                                              "illustration"]},
                           "text_overlay": {"type": "string"}},
                       "required": ["style"]}}}

# Hard rule: tool visibility gating. Red-line platforms never see the
# tool, so the model physically cannot order it.
PLATFORM_RULES = {
    # hard rules, enforced structurally: reddit never sees the image
    # tool (visibility gating) and never carries an outbound link
    # (stripped in code, never trusted to the prompt)
    "reddit":   {"allow_image": False, "allow_link": False},
    "facebook": {"allow_image": True,  "allow_link": True},
    "telegram": {"allow_image": True,  "allow_link": True},
}


def tools_for(platform: str, mode: str = "hard") -> list:
    """Hard mode strips red-line tools from the menu (structural safety).
    Soft mode offers the full menu and lets the model decide per policy."""
    if mode == "hard" and not PLATFORM_RULES.get(platform, {}).get("allow_image", False):
        return []
    return [CREATE_IMAGE_TOOL]


_ssl_ctx = ssl_ctx  # legacy internal name kept for imports/tests


def _execute_image_tool(args: dict, settings: Settings) -> dict:
    """Executor: delegates to OUR OWN image gateway when one is
    configured (IMAGE_GATEWAY_URL). Inner contract: request {prompt, n};
    response must be {ok: true, job_id: str}. Without a gateway it
    produces a visibly-marked stub so runs stay reproducible offline."""
    import json
    gateway = settings.image_gateway_url
    prompt = (f"{args.get('style', 'photo_collage')} style China travel post; "
              f"overlay text: {args.get('text_overlay', '')}".strip())
    if not gateway:
        print("  [image] no IMAGE_GATEWAY_URL configured -> marked stub")
        return {"kind": "image", "style": args.get("style"),
                "overlay": args.get("text_overlay", ""),
                "artifact": "img_stub_001.png", "via": "stub"}
    try:
        req = rq.Request(gateway,
                         data=json.dumps({"prompt": prompt[:1000], "n": 1}).encode(),
                         headers={"Content-Type": "application/json"})
        with rq.urlopen(req, timeout=15, context=_ssl_ctx()) as resp:
            payload = json.loads(resp.read())
        if payload.get("ok") is not True or not payload.get("job_id"):
            raise ValueError(f"gateway contract violation: {payload}")
        print(f"  [image] submitted to gateway job={payload['job_id'][:8]}")
        return {"kind": "image", "style": args.get("style"),
                "overlay": args.get("text_overlay", ""),
                "artifact": f"job:{payload['job_id']}", "via": "image-gateway"}
    except Exception as exc:
        print(f"  [image] gateway unavailable ({str(exc)[:50]}) -> marked stub")
        return {"kind": "image", "style": args.get("style"),
                "overlay": args.get("text_overlay", ""),
                "artifact": "img_stub_001.png", "via": "stub"}


TOOL_IMPLS = {"create_image_content": _execute_image_tool}


# ------------------------------------------------------------------
# Storage: sqlite ledger + long-term memory store, built lazily so
# importing this module has no side effects (tests, approval_api).
# ------------------------------------------------------------------
@lru_cache(maxsize=1)
def get_db() -> sqlite3.Connection:
    settings = get_settings()
    db = sqlite3.connect(settings.db_path, check_same_thread=False)
    db.execute("CREATE TABLE IF NOT EXISTS publish_history("
               "task_id TEXT, platform TEXT, post_ref TEXT, verified INT,"
               "PRIMARY KEY(task_id, platform))")
    db.execute("CREATE TABLE IF NOT EXISTS pending_reviews("
               "task_id TEXT PRIMARY KEY, platform TEXT,"
               "created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    db.commit()
    return db


@lru_cache(maxsize=1)
def get_store() -> SqliteStore:
    settings = get_settings()
    store = SqliteStore(sqlite3.connect(settings.store_path,
                                        check_same_thread=False,
                                        isolation_level=None))
    # Seed baseline trust only when absent — re-putting here would wipe
    # incident history on every process start, defeating the memory loop.
    if store.get(("account", "reddit"), "trust") is None:
        store.put(("account", "reddit"), "trust",
                  {"tier": "established", "incidents": 0})
    return store


@lru_cache(maxsize=1)
def get_graph():
    db = get_db()
    checkpointer = SqliteSaver(sqlite3.connect(get_settings().db_path,
                                               check_same_thread=False))
    builder = StateGraph(FlowState)
    builder.add_node("retrieve_context", retrieve_context)
    builder.add_node("platform_draft", platform_draft)
    builder.add_node("approve", approve)
    builder.add_node("platform_publish", platform_publish)
    builder.add_node("escalate", escalate)
    builder.add_node("record_all", record_all)

    builder.add_edge(START, "retrieve_context")
    builder.add_conditional_edges("retrieve_context", route_drafts,
                                  ["platform_draft"])
    builder.add_edge("platform_draft", "approve")
    builder.add_conditional_edges("approve", after_approve,
                                  ["platform_publish", END])
    builder.add_edge("platform_publish", "record_all")
    # record_all writes the ledger, then routes: any failure -> the
    # batch escalation gate; clean sweep -> END
    builder.add_conditional_edges("record_all", after_harvest,
                                  ["escalate", END])
    builder.add_conditional_edges("escalate", after_escalate,
                                  ["platform_publish", END])

    return builder.compile(checkpointer=checkpointer, store=get_store())


# ------------------------------------------------------------------
# Nodes: read full state, return a partial update (the patch)
# ------------------------------------------------------------------
def retrieve_context(state: FlowState, *, store=None) -> dict:
    """Also normalizes legacy single-platform input ("platform": "x")
    into the multi-platform list."""
    """Grounding pack for the generate node. The itinerary facts come
    from the reference site's own AI planner (POST /api/ai/chat) — the
    authoritative source — so the model composes copy from real data
    instead of its imagination.

    Tool health is cross-session memory: the planner's failures are
    tracked in the Store circuit breaker (agentposthub.breaker), so a
    planner that failed THRESHOLD times in a row is skipped entirely
    for the cooldown window — no request is wasted. Outages degrade to
    a visible marker; the run continues on brief-only facts."""
    from .tripplanner import plan_trip
    from .breaker import breaker_open, breaker_record
    from .distill import active_rule_records, stamp_applied
    brief = state["brief"]
    grounding = {
        "facts": [f"{brief['days']} days budget ${brief['budget_usd']}"],
        "policy": "external link goes into the first comment",
    }
    rules = active_rule_records(store)      # distilled, human-approved
    if rules:
        keys = [k for k, _ in rules]
        grounding["lessons"] = [v["rule"] for _, v in rules]
        grounding["lesson_keys"] = keys
        stamp_applied(store, keys)          # usage: applied_count+1
        print(f"  [grounding] {len(rules)} active lesson(s) loaded")
    topic = brief.get("notes") or \
        f"China trip, {brief.get('cities', 1)} cities"
    ask = (f"{topic}: {brief['days']} days, budget ${brief['budget_usd']}, "
           f"1 adult. Give a concrete itinerary with a cost breakdown.")

    open_, info = breaker_open(store, "site_planner")
    if open_:
        grounding["site_plan_error"] = (
            f"circuit_open (last_error={info.get('last_error')})")
        print(f"  [grounding] planner circuit open "
              f"({info.get('failures')} consecutive failures) - skipped")
        platforms = state.get("platforms") or (
            [state["platform"]] if state.get("platform") else [])
        return {"grounding": grounding, "platforms": platforms}

    from .tracing import span as trace_span
    with trace_span("site_planner", as_type="tool",
                    input=ask[:200]):
        # no explicit trace_id: created inside the node, the OTel
        # context makes this a CHILD of retrieve_context's span and
        # inherits the trace - the tree shows true nesting
        plan = plan_trip(ask, locale="zh")
    # no_draft is an input-shape problem (the planner asked a clarifying
    # question), not an outage - it must not trip the health breaker
    err = plan.get("error")
    counts_as_failure = plan["ok"] or err in ("rate_limited", "empty_response") \
        or err.startswith(("http_", "transport:"))
    breaker_record(store, "site_planner", bool(counts_as_failure), err)
    if plan["ok"]:
        grounding["site_plan"] = {
            "total_usd": plan["draft"]["total_usd"] if plan["draft"] else None,
            "route": plan["draft"]["route_names"] if plan["draft"] else [],
            "plan_text": plan["reply"][:2000],
        }
        print(f"  [grounding] site planner ok "
              f"(total_usd={grounding['site_plan']['total_usd']}, "
              f"route={grounding['site_plan']['route']})")
    else:
        grounding["site_plan_error"] = plan["error"]
        print(f"  [grounding] site planner unavailable "
              f"({plan['error']}) - brief-only facts")
    platforms = state.get("platforms") or (
        [state["platform"]] if state.get("platform") else [])
    return {"grounding": grounding, "platforms": platforms}


def _platform_prompt(platform: str, brief: dict, ground: dict) -> tuple[str, list]:
    """Platform-specific prompt assembly. Hard rules are structural:
    the image tool is simply not on reddit's menu (visibility gating)
    and the link policy line matches the platform rule table."""
    tools = tools_for(platform, mode="hard")
    policy_line = ground.get("policy", "")
    rules = PLATFORM_RULES.get(platform, {})
    if rules.get("allow_image") and tools:
        policy_line += (" Images ARE allowed here - call create_image_content "
                        "if (and only if) an image would strengthen the post.")
    if not rules.get("allow_link", True):
        policy_line += (" This platform forbids outbound links: the post text "
                        "must contain NO URLs and NO link mentions.")
    notes = brief.get("notes", "")
    facts = list(ground.get("facts", []))
    site_plan = ground.get("site_plan")
    if site_plan:
        facts.append(
            "AUTHORITATIVE planner output from the reference site - all "
            f"numbers must match this, do not invent: total "
            f"${site_plan['total_usd']}, route {site_plan['route']}. "
            f"Planner report: {site_plan['plan_text']}")
    elif ground.get("site_plan_error"):
        facts.append(f"site planner unavailable ({ground['site_plan_error']}) "
                     "- mark the plan as estimated")
    if notes:
        length_line = ("Produce a concrete itinerary with per-day details and "
                       "costs for attractions, lodging, food and transport, "
                       "plus a total. Stay under 4096 characters. Minimal "
                       "emoji, clear sections.")
        brief_line = notes
    else:
        length_line = "Max 60 words, include the keyword 'China Travel Budget'."
        brief_line = (f"a China trip: {brief.get('cities', 1)} cities, "
                      f"{brief['days']} days, 1 adult, "
                      f"budget ${brief['budget_usd']}")
    lessons = ground.get("lessons", [])
    lesson_line = (f"Operational lessons from past failures "
                   f"(obey strictly): {lessons}. ") if lessons else ""
    prompt = (
        f"Write a {platform} post about {brief_line} "
        f"Facts to respect: {facts}. "
        f"Platform policy: {policy_line} "
        f"{lesson_line}"
        f"Always include the post text itself; the image tool is an optional "
        f"extra on top of it, never a replacement. {length_line}"
    )
    return prompt, tools


def platform_draft(state: FlowState) -> dict:
    """Worker agent: one Send instance per platform. The same brief
    becomes a DIFFERENT draft per platform - reddit gets a text-only
    prompt with no image tool on the menu and a no-link policy line;
    facebook/telegram may earn media via the model's own tool choice.
    Real DeepSeek call with tool_choice=auto (soft rule on top of the
    hard structural rules)."""
    import json
    settings = get_settings()
    platform, brief = state["platform"], state["brief"]
    ground = state.get("grounding", {})

    if not settings.deepseek_api_key:
        raise RuntimeError("DEEPSEEK_API_KEY missing: add it to .env "
                           "(see .env.example) — generation cannot proceed")

    prompt, tools = _platform_prompt(platform, brief, ground)
    body = {"model": settings.deepseek_model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 300}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    req = rq.Request("https://api.deepseek.com/chat/completions",
                     data=json.dumps(body).encode(),
                     headers={"Authorization": f"Bearer {settings.deepseek_api_key}",
                              "Content-Type": "application/json"})
    msg = None
    usage = None
    from .tracing import span
    with span("deepseek_chat", as_type="generation",
              input=prompt[:300]) as obs:   # child of platform_draft's span
        for attempt in range(1, 4):  # transient 5xx/timeout: bounded retry
            try:
                with rq.urlopen(req, timeout=30, context=_ssl_ctx()) as resp:
                    payload = json.loads(resp.read())
                msg = payload["choices"][0]["message"]
                usage = payload.get("usage")
                break
            except rq.HTTPError as exc:
                if exc.code < 500 or attempt == 3:
                    raise
                time.sleep(RETRY_BACKOFF_S * attempt)
        if obs is not None:
            tool_calls = [{"name": c["function"]["name"],
                           "args": c["function"]["arguments"][:120]}
                          for c in (msg.get("tool_calls") or [])]
            obs.update(output={"text": (msg.get("content") or "")[:300],
                               "tool_calls": tool_calls},
                       model=settings.deepseek_model,
                       usage=(None if usage is None else {
                           "input": usage.get("prompt_tokens"),
                           "output": usage.get("completion_tokens"),
                           "total": usage.get("total_tokens")}))
    assert msg is not None

    text = (msg.get("content") or "").strip() or "(tool-only response)"
    media = None
    chase_usage = {}
    if msg.get("tool_calls"):
        call = msg["tool_calls"][0]["function"]
        if call["name"] not in TOOL_IMPLS:
            text = f"(rejected: unknown tool '{call['name']}')"
        else:
            impl = TOOL_IMPLS[call["name"]]
            media = impl(json.loads(call["arguments"] or "{}"), settings)
    # chase a real post text in EITHER placeholder shape: the
    # tool-call announcement (chat models) or the bare announcement
    # with no tool call at all (reasoning models - found by the
    # model-swap benchmark: reasoner placeholder_rate hit 1.0)
    if len(text) < 80 or looks_like_placeholder(text):
        text, chase_usage = _complete_text(prompt, settings)

    # semantic dedup: nearest published draft on THIS platform, real
    # cosine over Qwen embeddings when the vector backend is up;
    # fixed-low stub keeps the path alive otherwise
    sim = _semantic_similarity(text)
    try:
        from . import vectormem
        hits = vectormem.similar(f"published_{platform}", text, k=1)
        if hits:
            sim = max(sim, hits[0]["sim"])
    except Exception:
        pass
    if sim > 0.92:
        text += " [variant]"

    allow_link = PLATFORM_RULES.get(platform, {}).get("allow_link", True)
    draft = {"platform": platform, "text": text,
             "link": "https://1000usdinchina.com/card?x=1" if allow_link else None,
             "media": media, "version": 1,
             "tools_offered": [t["function"]["name"] for t in tools]}
    from .pricing import cost_from_usage
    spend = {"call": f"draft:{platform}", "model": settings.deepseek_model,
             "usd": cost_from_usage(usage),
             "tokens": (usage or {}).get("total_tokens")}
    if chase_usage:
        spend["chase_usd"] = cost_from_usage(chase_usage)
        spend["usd"] = round(spend["usd"] + spend["chase_usd"], 6)
    print(f"  [draft:{platform}] {len(text)} chars, "
          f"media={'yes' if media else 'no'}, "
          f"link={'yes' if draft['link'] else 'no'}, "
          f"spend=${spend['usd']:.5f}")
    return {"drafts": [draft], "spend_usd": [spend]}


_PLACEHOLDER_MARKS = ("(tool-only response", "(rejected:", "I'll create",
                      "I will create")


def looks_like_placeholder(text: str) -> bool:
    """True when the text is an announcement ABOUT the post rather than
    the post itself. Shared by platform_draft (chase a real completion)
    and the eval harness (real_copy check)."""
    return any(m in text for m in _PLACEHOLDER_MARKS)


def _complete_text(prompt: str, settings: Settings) -> tuple[str, dict]:
    """Tool-free follow-up completion: force the actual post text now that
    the tool decision (media) has already been captured. Returns
    (text, usage) so the chase call is billed to the task too."""
    body = {"model": settings.deepseek_model,
            "messages": [{"role": "user",
                          "content": prompt + "\n\nAnswer with the final post "
                          "text ONLY - no tool calls, no commentary."}],
            "max_tokens": 2000}
    req = rq.Request("https://api.deepseek.com/chat/completions",
                     data=json.dumps(body).encode(),
                     headers={"Authorization": f"Bearer {settings.deepseek_api_key}",
                              "Content-Type": "application/json"})
    for attempt in range(1, 4):
        try:
            with rq.urlopen(req, timeout=60, context=_ssl_ctx()) as resp:
                payload = json.loads(resp.read())
            return (payload["choices"][0]["message"]["content"].strip(),
                    payload.get("usage") or {})
        except rq.HTTPError as exc:
            if exc.code < 500 or attempt == 3:
                raise
            time.sleep(RETRY_BACKOFF_S * attempt)
    raise RuntimeError("unreachable")


def _semantic_similarity(text: str) -> float:
    """Production: embed draft, search recent same-platform posts in
    pgvector, return top-1 cosine similarity. Placeholder until then."""
    return 0.30


def approve(state: FlowState) -> dict:
    """Human gate over ALL platform drafts at once. interrupt() suspends
    the whole graph; the state is persisted by the checkpointer and can
    hang for minutes or days. INSERT OR REPLACE keeps the re-execution
    on resume idempotent."""
    db = get_db()
    platforms = ",".join(state.get("platforms") or ["?"])
    db.execute("INSERT OR REPLACE INTO pending_reviews(task_id, platform) "
               "VALUES(?,?)", (state["task_id"], platforms))
    db.commit()
    advisory = None
    if get_settings().judge_enabled:
        from .judge import judge_drafts_aggregate
        notes = str(state.get("brief", {}).get("notes", ""))
        advisory = judge_drafts_aggregate(state.get("drafts", []), notes)
    decision = interrupt({
        "ask": "approve_drafts",
        "drafts": state.get("drafts", []),
        "judge": advisory,          # advisory only - the human decides
        "task_id": state["task_id"],
    })
    db.execute("DELETE FROM pending_reviews WHERE task_id=?", (state["task_id"],))
    db.commit()
    if decision["decision"] == "edited":
        texts = decision.get("texts") or {}
        drafts = [{**d, "text": texts.get(d["platform"], d["text"])}
                  for d in state.get("drafts", [])]
        return {"approval": "approved", "drafts": drafts}
    return {"approval": decision["decision"]}


def _check_output_contract(resp: dict, sent_req_id: str) -> str | None:
    """Output contract: the platform response must match the expected
    shape and echo back the idempotency key we sent. Returns None when
    valid, otherwise a violation description."""
    if not isinstance(resp, dict):
        return f"not a dict: {type(resp)}"
    for field in ("id", "ok", "req_id"):
        if field not in resp:
            return f"missing field: {field}"
    if not isinstance(resp["id"], str) or not resp["id"]:
        return "id must be a non-empty str"
    if not isinstance(resp["ok"], bool):
        return "ok must be bool"
    if resp["req_id"] != sent_req_id:
        return f"req_id mismatch: sent {sent_req_id}, got {resp['req_id']}"
    return None


def platform_publish(state: FlowState, *, store=None) -> dict:
    """Worker agent: one Send instance per platform. Inlines the whole
    per-platform chain - hard-rule strip -> MCP publish (idempotency
    key) -> output contract -> independent read-back -> trust memory.
    Platforms without gateway configuration are SKIPPED with a visible
    reason (skipped is not failed: no incidents, no retry)."""
    platform = state["platform"]
    draft = dict(state["draft"])
    task_id = state["task_id"]

    # kill switch: fail-closed pause of all irreversible actions. A
    # paused branch is NOT a system failure - it routes to the human
    # gate, and once the switch is released a retry publishes normally.
    if store is not None:
        ks = store.get(("system",), "kill_switch")
        if ks and ks.value.get("publishing") is False:
            print(f"  [publish:{platform}] PAUSED by kill switch")
            return {"platform_results": [{"platform": platform,
                                          "paused": True, "ok": False,
                                          "error": "kill_switch_active"}],
                    "verifications": [{"platform": platform, "ok": False,
                                       "reason": "kill_switch_active"}],
                    "failures": [{"platform": platform,
                                  "node": "platform_publish",
                                  "reason": "kill_switch_active",
                                  "attempt": state.get("retry_count", 0)}]}

    # hard rules in code, never trusting model output
    if not PLATFORM_RULES.get(platform, {}).get("allow_link", True):
        draft["link"] = None
        draft["stripped_by"] = "platform_rule:no_link"
    if store is not None:
        rec = store.get(("account", platform), "trust")
        tier = rec.value["tier"] if rec else "established"
        print(f"  [memory] trust tier ({platform}) = {tier}")
        if tier == "building":
            draft["link"] = None
            draft["stripped_by"] = (draft.get("stripped_by", "") +
                                    "+memory:trust=building")

    from . import composio as gw
    if platform not in gw.PUBLISH_ACTIONS:
        print(f"  [publish:{platform}] skipped - gateway action not "
              f"configured for this platform")
        return {"platform_results": [{"platform": platform, "skipped": True,
                                      "ok": True,
                                      "reason": "gateway_not_configured"}],
                "verifications": [{"platform": platform, "ok": True,
                                   "reason": "skipped"}]}

    req_id = f"{task_id}#{draft.get('version', 1)}"
    try:
        resp = gw.publish(platform, draft["text"], req_id)
    except GatewayError as exc:
        print(f"  [publish:{platform}] gateway failure: {exc}")
        return {"platform_results": [{"platform": platform, "ok": False,
                                      "error": str(exc)}],
                "verifications": [{"platform": platform, "ok": False,
                                   "reason": str(exc)}],
                "failures": [{"platform": platform, "node": "platform_publish",
                              "reason": str(exc),
                              "attempt": state.get("retry_count", 0)}]}
    violation = _check_output_contract(resp, req_id)
    if violation:
        print(f"  [contract:{platform}] output contract violation: {violation}")
        return {"platform_results": [{"platform": platform, "ok": False,
                                      "error": f"output_contract: {violation}"}],
                "verifications": [{"platform": platform, "ok": False,
                                   "reason": violation}],
                "failures": [{"platform": platform, "node": "platform_publish",
                              "reason": violation,
                              "attempt": state.get("retry_count", 0)}]}

    # phantom-publish contract: confirm, never assume
    try:
        live = gw.read_back(platform, str(resp["id"]), receipt=resp.get("receipt"),
                            req_id=req_id)
    except GatewayError as exc:
        live = {"visible": False, "reason": f"read-back failed: {exc}"}
    if not live.get("visible") and store is not None:
        rec = store.get(("account", platform), "trust")
        current = rec.value if rec else {"tier": "established", "incidents": 0}
        store.put(("account", platform), "trust",
                  {"tier": "building",
                   "incidents": current.get("incidents", 0) + 1})
        print(f"  [memory] removal detected on {platform} -> trust "
              f"downgraded (incidents={current.get('incidents', 0) + 1})")
    ok = bool(live.get("visible"))
    return {"platform_results": [{"platform": platform, **resp}],
            "verifications": [{"platform": platform, "ok": ok,
                               "comment_id": resp["id"],
                               "reason": live.get("reason", "")}],
            **({} if ok else {"failures": [
                {"platform": platform, "node": "platform_publish",
                 "reason": f"read-back: {live.get('reason')}",
                 "attempt": state.get("retry_count", 0)}]})}


def record_all(state: FlowState) -> dict:
    """Idempotent ledger write per verified platform (PK task+platform),
    plus archival of the published text into vector memory so future
    drafts can be deduped BY MEANING, not just hash."""
    from . import vectormem
    db = get_db()
    drafts = {d["platform"]: d for d in state.get("drafts", [])}
    for v in state.get("verifications", []):
        if v.get("ok") and v.get("comment_id"):
            db.execute(
                "INSERT OR REPLACE INTO publish_history(task_id, platform, "
                "post_ref, verified) VALUES(?,?,?,1)",
                (state["task_id"], v["platform"], v["comment_id"]))
            print(f"  [record] {state['task_id']} -> {v['platform']} "
                  f"({v['comment_id']})")
            d = drafts.get(v["platform"])
            if d and v["platform"] != "reddit":   # reddit skips too
                vectormem.add(f"published_{v['platform']}",
                              state["task_id"], d.get("text", ""),
                              {"task_id": state["task_id"]})
    db.commit()


# ------------------------------------------------------------------
# Fan-out routing: the supervisor decisions (no manual selection)
# ------------------------------------------------------------------
def _over_budget(state: FlowState) -> bool:
    from .config import get_settings
    from .pricing import spent_usd
    budget = state.get("budget_usd") or get_settings().task_budget_usd
    return spent_usd(state.get("spend_usd") or []) >= budget


def route_drafts(state: FlowState):
    """After grounding, fan out one draft worker per target platform.
    Budget guard: a task that already burned its cap does not fan out -
    it routes straight to the escalation gate (the human decides)."""
    if _over_budget(state):
        print("  [budget] spend cap reached before drafting - to gate")
        return "escalate"
    platforms = state.get("platforms") or []
    return [Send("platform_draft", {
        "task_id": state["task_id"], "trace_id": state.get("trace_id", ""),
        "platform": p, "brief": state["brief"],
        "grounding": state.get("grounding", {})})
        for p in platforms]


def after_approve(state: FlowState):
    """Gate #1 decision: rejected ends the task; approved fans the
    per-platform publish workers out in parallel. Budget guard applies
    before the second fan-out too."""
    if state.get("approval") != "approved":
        return END
    if _over_budget(state):
        print("  [budget] spend cap reached before publishing - to gate")
        return "escalate"
    return [Send("platform_publish", {
        "task_id": state["task_id"], "trace_id": state.get("trace_id", ""),
        "platform": d["platform"], "draft": d,
        "retry_count": state.get("retry_count", 0)})
        for d in state.get("drafts", [])]


def after_harvest(state: FlowState) -> Literal["escalate", "__end__"]:
    """Join point after all publish workers (record_all already ran and
    wrote the ledger for every verified platform). failures is an
    append-only audit channel that cannot be cleared on retry, so only
    entries from the CURRENT attempt generation count for routing."""
    rc = state.get("retry_count", 0)
    current = [f for f in state.get("failures", [])
               if f.get("attempt", 0) == rc]
    return "escalate" if current else END


MAX_PUBLISH_RETRIES = 2


def escalate(state: FlowState, *, store=None) -> dict:
    """Gate #2 (design doc section 3.1): failed publishes suspend here
    instead of dying - one BATCH gate for all failed platforms. retry
    re-Sends only the failed platforms with the SAME drafts (same
    idempotency keys, the platforms dedupe, nothing double-posts);
    drop records incidents and distills. Retries bounded; exhaustion
    forces the drop path."""
    attempts = state.get("retry_count", 0)
    from .pricing import spent_usd as _spent
    spend_line = (f"${_spent(state.get('spend_usd') or []):.4f}"
                  f"/${state.get('budget_usd') or 0.10:.2f}")
    if _over_budget(state) and attempts > 0:
        # deterministic stop: budget burned during retries - no more
        # interrupts, drop with incidents so it distills
        print(f"  [budget] cap burned through retries ({spend_line}) - "
              f"forced drop")
        decision = {"action": "drop"}
    elif attempts < MAX_PUBLISH_RETRIES:
        get_db().execute("INSERT OR REPLACE INTO pending_reviews(task_id, platform) "
                         "VALUES(?,?)", (state["task_id"], "escalation"))
        get_db().commit()
        decision = interrupt({
            "ask": "escalation",
            "task_id": state["task_id"],
            "failures": state.get("failures", []),
            "spend": spend_line,
            "attempts": attempts,
            "max": MAX_PUBLISH_RETRIES,
        })
        if decision["action"] == "retry":
            get_db().execute("DELETE FROM pending_reviews WHERE task_id=?",
                             (state["task_id"],))
            get_db().commit()
            failed = sorted({f.get("platform") for f in state.get("failures", [])
                             if f.get("platform")} - {None})
            print(f"  [escalate] re-activated (attempt {attempts + 1}/"
                  f"{MAX_PUBLISH_RETRIES}) - republishing {failed}")
            return {"retry_count": attempts + 1,
                    "retry_platforms": failed,
                    "escalation_action": "retry"}
    # drop (or retries exhausted): terminal bookkeeping
    from .distill import (maybe_distill, record_incident,
                          stamp_failed_while_applied)
    for f in state.get("failures", []):
        if str(f.get("reason", "")).startswith("kill_switch"):
            continue          # a human pause is not an incident
        record_incident(store, state["task_id"], f.get("platform", "?"),
                        f.get("node", "?"), f.get("reason", "?"))
    lesson_keys = state.get("grounding", {}).get("lesson_keys", [])
    if lesson_keys and state.get("failures"):
        stamp_failed_while_applied(store, lesson_keys)
    proposal = maybe_distill(store)
    if proposal:
        print(f"  [escalate] rule proposal distilled: "
              f"{proposal['rule'][:70]}... (pending human approval)")
    print("  [escalate] dropped; incidents recorded")
    return {"escalation_action": "drop"}


def after_escalate(state: FlowState):
    """retry re-Sends ONLY the failed platforms (their drafts unchanged,
    idempotency keys unchanged)."""
    if state.get("escalation_action") != "retry":
        return END
    if _over_budget(state):
        print("  [budget] cap reached - retry refused")
        return END
    failed = state.get("retry_platforms") or []
    drafts = {d["platform"]: d for d in state.get("drafts", [])}
    return [Send("platform_publish", {
        "task_id": state["task_id"], "trace_id": state.get("trace_id", ""),
        "platform": p, "draft": drafts[p],
        "retry_count": state.get("retry_count", 0)})
        for p in failed if p in drafts]

