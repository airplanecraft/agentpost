"""Rule distillation: turn incident history into operational knowledge.

Closed learning loop, with a human gate on the policy change (design
doc section 5: "a rule that never activates beats a rule that fires
wrong"):

  1. every failed task records its incidents into the Store
     namespace ("incident", "*") — cross-session by construction
  2. once DISTILL_THRESHOLD fresh incidents accumulate, an LLM pass
     distills them into a PROPOSED rule (never auto-activated)
  3. a human approves or rejects the proposal (CLI)
  4. approved rules are injected into the grounding pack of every
     future task, before content is created
"""
from __future__ import annotations

import json
import time
import urllib.request as rq

from .config import Settings, get_settings, ssl_ctx

DISTILL_THRESHOLD = 3
INCIDENT_NS = ("incident",)
RULE_NS = ("rule",)


# ------------------------------------------------------------------
# Incident recording (called from the escalate node on failed tasks)
# ------------------------------------------------------------------
def record_incident(store, task_id: str, platform: str,
                    node: str, reason: str) -> None:
    if store is None:
        return
    items = store.search(INCIDENT_NS) or []
    key = f"inc_{int(time.time())}_{len(items):03d}"
    store.put(INCIDENT_NS, key, {
        "task_id": task_id, "platform": platform,
        "node": node, "reason": reason[:300],
        "at": int(time.time()), "distilled": False,
    })


def _undistilled(store) -> list:
    return [i for i in (store.search(INCIDENT_NS) or [])
            if not i.value.get("distilled")]


# ------------------------------------------------------------------
# LLM distillation -> proposal
# ------------------------------------------------------------------
def _deepseek_chat(prompt: str, settings: Settings) -> str:
    body = {"model": "deepseek-chat",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 300}
    req = rq.Request("https://api.deepseek.com/chat/completions",
                     data=json.dumps(body).encode(),
                     headers={"Authorization": f"Bearer {settings.deepseek_api_key}",
                              "Content-Type": "application/json"})
    with rq.urlopen(req, timeout=30, context=ssl_ctx()) as resp:
        return json.loads(resp.read())["choices"][0]["message"]["content"].strip()


def maybe_distill(store, settings: Settings | None = None) -> dict | None:
    """If enough fresh incidents exist, distill them into a pending rule
    proposal. Returns the proposal dict, or None below threshold."""
    if store is None:
        return None
    incidents = _undistilled(store)
    if len(incidents) < DISTILL_THRESHOLD:
        return None
    settings = settings or get_settings()
    if not settings.deepseek_api_key:
        return None

    lines = "\n".join(
        f"- task={i.value['task_id']} platform={i.value['platform']} "
        f"node={i.value['node']} reason={i.value['reason']}"
        for i in incidents[:20])
    prompt = (
        "You are distilling operational incidents from a content-publishing "
        "agent into ONE actionable rule.\n"
        f"Incidents:\n{lines}\n\n"
        "Reply with exactly two lines:\n"
        "RULE: <one imperative sentence, max 40 words, that would have "
        "prevented or mitigated these incidents>\n"
        "WHY: <one sentence on the shared root cause>"
    )
    raw = _deepseek_chat(prompt, settings)

    rule = rationale = ""
    for line in raw.splitlines():
        if line.upper().startswith("RULE:"):
            rule = line[5:].strip()
        elif line.upper().startswith("WHY:"):
            rationale = line[4:].strip()
    if not rule:
        return None

    prop = {"rule": rule, "rationale": rationale,
            "status": "pending", "created_at": int(time.time()),
            "incident_keys": [i.key for i in incidents[:20]]}
    store.put(RULE_NS, f"prop_{int(time.time())}", prop)
    for i in incidents[:20]:     # consumed; won't be re-distilled
        store.put(INCIDENT_NS, i.key, {**i.value, "distilled": True})
    return prop


# ------------------------------------------------------------------
# Human gate + active-rule consumption
# ------------------------------------------------------------------
def decide_proposal(store, proposal_key: str, approve: bool) -> bool:
    rec = store.get(RULE_NS, proposal_key) if store else None
    if not rec or rec.value.get("status") != "pending":
        return False
    store.put(RULE_NS, proposal_key,
              {**rec.value, "status": "approved" if approve else "rejected"})
    return True


def list_proposals(store) -> list:
    if not store:
        return []
    return [i for i in (store.search(RULE_NS) or [])
            if i.value.get("status") == "pending"]


def active_rules(store) -> list[str]:
    """Approved rule texts — read before every content creation."""
    if not store:
        return []
    return [i.value["rule"] for i in store.search(RULE_NS) or []
            if i.value.get("status") == "approved"]
