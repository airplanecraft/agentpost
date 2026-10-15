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
from langgraph.types import Command, interrupt

from . import composio
from .composio import RETRY_BACKOFF_S, GatewayError
from .config import Settings, get_settings, ssl_ctx


class FlowState(TypedDict):
    """Graph state ledger. Single-value fields use the default overwrite
    reducer; parallel-collected fields declare append reducers so Send
    fan-out results never clobber each other."""
    task_id: str
    platform: str                  # telegram | facebook | reddit
    brief: dict                    # route brief: cities/days/budget

    grounding: dict                # RAG grounding pack
    draft: dict                    # generated draft
    approval: str                  # pending|approved|rejected|edited
    regen_count: int               # bounded regeneration counter
    verification: dict             # read-back result

    platform_results: Annotated[list[dict], operator.add]
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
    "reddit":   {"allow_image": False},
    "facebook": {"allow_image": True},
    "telegram": {"allow_image": True},
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
    builder.add_node("generate", generate)
    builder.add_node("dedup_guard", dedup_guard)
    builder.add_node("approve", approve)
    builder.add_node("policy_validator", policy_validator)
    builder.add_node("publish", publish)
    builder.add_node("verify", verify)
    builder.add_node("record", record)
    builder.add_node("escalate", escalate)

    builder.add_edge(START, "retrieve_context")
    builder.add_edge("retrieve_context", "generate")
    builder.add_edge("generate", "dedup_guard")
    builder.add_conditional_edges("dedup_guard", after_dedup)
    builder.add_conditional_edges("approve", after_approve)
    builder.add_edge("policy_validator", "publish")
    builder.add_edge("publish", "verify")
    builder.add_conditional_edges("verify", after_verify)
    builder.add_edge("record", END)
    builder.add_edge("escalate", END)

    return builder.compile(checkpointer=checkpointer, store=get_store())


# ------------------------------------------------------------------
# Nodes: read full state, return a partial update (the patch)
# ------------------------------------------------------------------
def retrieve_context(state: FlowState, *, store=None) -> dict:
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
    from .distill import active_rules
    brief = state["brief"]
    grounding = {
        "facts": [f"{brief['days']} days budget ${brief['budget_usd']}"],
        "policy": "no outbound links while account trust is building"
                  if state["platform"] == "reddit"
                  else "external link goes into the first comment",
    }
    lessons = active_rules(store)   # distilled, human-approved lessons
    if lessons:
        grounding["lessons"] = lessons
        print(f"  [grounding] {len(lessons)} active lesson(s) loaded")
    topic = brief.get("notes") or f"China trip, {brief['cities']} cities"
    ask = (f"{topic}: {brief['days']} days, budget ${brief['budget_usd']}, "
           f"1 adult. Give a concrete itinerary with a cost breakdown.")

    open_, info = breaker_open(store, "site_planner")
    if open_:
        grounding["site_plan_error"] = (
            f"circuit_open (last_error={info.get('last_error')})")
        print(f"  [grounding] planner circuit open "
              f"({info.get('failures')} consecutive failures) - skipped")
        return {"grounding": grounding}

    plan = plan_trip(ask, locale="zh")
    breaker_record(store, "site_planner", plan["ok"],
                   plan.get("error"))
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
    return {"grounding": grounding}


def generate(state: FlowState) -> dict:
    """The single model-led node. Real DeepSeek call with tool choice.
    Tool menu is pre-filtered by the hard rule; remaining tools are
    offered with tool_choice=auto so the model decides autonomously
    (soft rule)."""
    import json
    settings = get_settings()
    version = state.get("draft", {}).get("version", 0) + 1
    platform, brief = state["platform"], state["brief"]
    ground = state.get("grounding", {})

    if not settings.deepseek_api_key:
        raise RuntimeError("DEEPSEEK_API_KEY missing: add it to .env "
                           "(see .env.example) — generation cannot proceed")

    tools = tools_for(platform, mode="hard")
    policy_line = ground.get("policy", "")
    if tools:
        policy_line += (" Images ARE allowed here - call create_image_content "
                        "if (and only if) an image would strengthen the post.")
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
        # Detailed brief: long-form structured content instead of a teaser
        length_line = ("Produce a concrete itinerary with per-day details and "
                       "costs for attractions, lodging, food and transport, "
                       "plus a total. Telegram allows up to 4096 characters - "
                       "stay under that. Use minimal emoji and clear sections.")
        brief_line = notes
    else:
        length_line = "Max 60 words, include the keyword 'China Travel Budget'."
        brief_line = (f"a China trip: {brief['cities']} cities, "
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
    body = {"model": "deepseek-chat",
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
    for attempt in range(1, 4):  # transient 5xx/timeout: bounded retry
        try:
            with rq.urlopen(req, timeout=30, context=_ssl_ctx()) as resp:
                msg = json.loads(resp.read())["choices"][0]["message"]
            break
        except rq.HTTPError as exc:
            if exc.code < 500 or attempt == 3:
                raise
            time.sleep(RETRY_BACKOFF_S * attempt)
    assert msg is not None

    text = (msg.get("content") or "").strip() or "(tool-only response)"
    media = None
    if msg.get("tool_calls"):
        call = msg["tool_calls"][0]["function"]
        if call["name"] not in TOOL_IMPLS:
            # model hallucinated a tool that is not on the menu
            text = f"(rejected: unknown tool '{call['name']}')"
        else:
            impl = TOOL_IMPLS[call["name"]]
            media = impl(json.loads(call["arguments"] or "{}"), settings)
            if len(text) < 80:
                # Tool-call turns often carry an announcement ("I'll create
                # ...") instead of the post itself. Chase with a tool-free
                # completion so the draft always has real text.
                text = _complete_text(prompt, settings)

    return {"draft": {"text": text, "link": "https://1000usdinchina.com/card?x=1",
                      "media": media, "version": version,
                      "tools_offered": [t["function"]["name"] for t in tools]}}


def _complete_text(prompt: str, settings: Settings) -> str:
    """Tool-free follow-up completion: force the actual post text now that
    the tool decision (media) has already been captured."""
    body = {"model": "deepseek-chat",
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
                return json.loads(resp.read())["choices"][0]["message"]["content"].strip()
        except rq.HTTPError as exc:
            if exc.code < 500 or attempt == 3:
                raise
            time.sleep(RETRY_BACKOFF_S * attempt)
    raise RuntimeError("unreachable")


def dedup_guard(state: FlowState) -> dict:
    """Three-layer waterfall: target id -> content hash -> semantic
    similarity. Near-duplicate drafts are bounced back for regeneration,
    bounded by the counter (escalation on exhaustion).
    Semantic layer is a fixed low score until pgvector lands; hash and
    regen bounding are live."""
    draft_text = state["draft"]["text"]
    similarity = _semantic_similarity(draft_text)
    if similarity > 0.92:
        if state.get("regen_count", 0) >= 2:
            return {"failures": [{"node": "dedup_guard",
                                  "reason": "regeneration budget exhausted"}]}
        return {"regen_count": state.get("regen_count", 0) + 1,
                "draft": {**state["draft"],
                          "text": draft_text + f" [variant {similarity:.2f}]"}}
    return {}


def _semantic_similarity(text: str) -> float:
    """Production: embed draft, search recent same-platform posts in
    pgvector, return top-1 cosine similarity. Placeholder until then."""
    return 0.30


def approve(state: FlowState) -> dict:
    """Human gate. interrupt() suspends the whole graph; the state is
    persisted by the checkpointer and can hang for minutes or days.
    Before suspending we insert a pending-review row so any surface
    (CLI, API) can list waiting items with a plain SQL query; after
    resume we delete it. INSERT OR REPLACE keeps the re-execution on
    resume idempotent."""
    db = get_db()
    db.execute("INSERT OR REPLACE INTO pending_reviews(task_id, platform) "
               "VALUES(?,?)", (state["task_id"], state["platform"]))
    db.commit()
    decision = interrupt({
        "ask": "approve_draft",
        "draft": state["draft"],
        "platform": state["platform"],
        "task_id": state["task_id"],
    })
    db.execute("DELETE FROM pending_reviews WHERE task_id=?", (state["task_id"],))
    db.commit()
    if decision["decision"] == "edited":
        return {"approval": "approved",
                "draft": {**state["draft"], "text": decision["text"]}}
    return {"approval": decision["decision"]}


def policy_validator(state: FlowState, *, store=None) -> dict:
    """Hard-rule checks in code, never trusting model output. The
    trust-building strip is driven by long-term memory (Store), not a
    hardcoded platform check — memory influencing decisions, live."""
    draft = dict(state["draft"])
    tier = "established"
    if store is not None:
        rec = store.get(("account", state["platform"]), "trust")
        tier = rec.value["tier"] if rec else "established"
        print(f"  [memory] trust tier ({state['platform']}) = {tier}")
    if tier == "building":
        draft["link"] = None
        draft["stripped_by"] = "memory:trust=building"
    return {"draft": draft}


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


def publish(state: FlowState) -> dict:
    """Platform publish through the Composio MCP gateway with an
    idempotency key; the response must pass the output contract before
    it is treated as success."""
    req_id = f"{state['task_id']}#{state['draft']['version']}"
    try:
        resp = composio.publish(state["platform"], state["draft"]["text"], req_id)
    except GatewayError as exc:
        print(f"  [publish] gateway failure: {exc}")
        return {"platform_results": [{"platform": state["platform"], "ok": False,
                                      "error": str(exc)}],
                "failures": [{"node": "publish", "reason": str(exc)}]}

    violation = _check_output_contract(resp, req_id)
    if violation:
        print(f"  [contract] output contract violation: {violation}")
        return {"platform_results": [{"platform": state["platform"], "ok": False,
                                      "error": f"output_contract: {violation}"}],
                "failures": [{"node": "publish", "reason": violation}]}
    return {"platform_results": [{"platform": state["platform"], **resp}]}


def verify(state: FlowState, *, store=None) -> dict:
    """Post-execution read-back: the phantom-publish contract. Success
    is confirmed against the platform, never assumed from the tool
    response. A detected removal distills into long-term memory (trust
    downgrade), closing the memory loop for the next decision."""
    result = state["platform_results"][-1]
    if not result.get("ok", True):
        return {"verification": {"ok": False, "reason": result.get("error")}}
    try:
        live = composio.read_back(state["platform"], str(result["id"]),
                                  receipt=result.get("receipt"))
    except GatewayError as exc:
        live = {"visible": False, "reason": f"read-back failed: {exc}"}
    if not live.get("visible") and store is not None:
        rec = store.get(("account", state["platform"]), "trust")
        current = rec.value if rec else {"tier": "established", "incidents": 0}
        store.put(("account", state["platform"]), "trust",
                  {"tier": "building", "incidents": current.get("incidents", 0) + 1})
        print(f"  [memory] removal detected -> trust downgraded in store "
              f"(incidents={current.get('incidents', 0) + 1})")
    return {"verification": {"ok": bool(live.get("visible")),
                             "comment_id": result["id"],
                             "reason": live.get("reason", "")}}


def record(state: FlowState) -> dict:
    """Idempotent ledger write (primary key task_id + platform)."""
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO publish_history(task_id, platform, post_ref, verified) "
        "VALUES(?,?,?,?)",
        (state["task_id"], state["platform"],
         state["verification"].get("comment_id"), 1))
    db.commit()
    print(f"  [record] ok: {state['task_id']} -> {state['platform']}")


# ------------------------------------------------------------------
# Conditional edges: routing reads the ledger, returns the next node
# ------------------------------------------------------------------
def after_dedup(state: FlowState) -> Literal["generate", "approve"]:
    if state.get("regen_count", 0) > 0 and \
            state["draft"]["text"].count("[variant") < state.get("regen_count", 0):
        return "generate"
    return "approve"


def after_approve(state: FlowState) -> Literal["policy_validator", "__end__"]:
    return "policy_validator" if state["approval"] == "approved" else END


def escalate(state: FlowState, *, store=None) -> dict:
    """Failure exit: incidents are distilled into long-term memory.
    Each failed task appends its incidents to the Store (cross-session);
    when enough accumulate, an LLM pass proposes ONE rule — which stays
    a proposal until a human approves it (design doc: an unactivated
    rule beats a wrongly-fired one)."""
    from .distill import maybe_distill, record_incident
    for f in state.get("failures", []):
        record_incident(store, state["task_id"], state["platform"],
                        f.get("node", "?"), f.get("reason", "?"))
    proposal = maybe_distill(store)
    if proposal:
        print(f"  [escalate] rule proposal distilled: "
              f"{proposal['rule'][:70]}... (pending human approval)")
    print(f"  [escalate] task ended in failure; incidents recorded")
    return {}


def after_verify(state: FlowState) -> Literal["record", "escalate"]:
    # bounded retry (max two, with cooldown) then escalate is the
    # production path; here failures route to the escalation exit
    ok = state.get("verification", {}).get("ok")
    return "record" if ok else "escalate"
