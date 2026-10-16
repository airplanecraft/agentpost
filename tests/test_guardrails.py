"""Guardrails: pricing math, kill switch (fail-closed, not an
incident), budget guards on both fan-outs + forced stop, explicit
recursion cap."""
import pytest

from agentposthub import flow
from agentposthub.config import reset_settings_cache
from agentposthub.pricing import cost_from_usage, spent_usd
from agentposthub.flow import get_graph, get_store

BUDGET_STATE = {"task_id": "t1", "trace_id": "ab" * 16,
                "platforms": ["telegram"],
                "brief": {"days": 3, "budget_usd": 300},
                "budget_usd": 0.01,
                "spend_usd": [{"call": "draft:telegram", "usd": 0.05}],
                "drafts": [{"platform": "telegram", "text": "x",
                            "version": 1}],
                "approval": "approved", "retry_count": 0}


# ---- pricing ----

def test_cost_from_usage_math(monkeypatch):
    monkeypatch.delenv("PRICE_IN_PER_M", raising=False)
    monkeypatch.delenv("PRICE_OUT_PER_M", raising=False)
    reset_settings_cache()
    # deepseek-chat default table: 0.27/1.10 per 1M
    c = cost_from_usage({"prompt_tokens": 1_000_000,
                         "completion_tokens": 1_000_000})
    assert abs(c - 1.37) < 1e-6
    assert spent_usd([{"usd": 0.05}, {"usd": 0.02}]) == 0.07


def test_price_env_override(monkeypatch):
    monkeypatch.setenv("PRICE_IN_PER_M", "1.0")
    monkeypatch.setenv("PRICE_OUT_PER_M", "2.0")
    reset_settings_cache()
    c = cost_from_usage({"prompt_tokens": 1_000_000,
                         "completion_tokens": 1_000_000})
    assert abs(c - 3.0) < 1e-6


# ---- budget guards ----

def test_over_budget_blocks_draft_fan_out():
    assert flow.route_drafts(dict(BUDGET_STATE)) == "escalate"


def test_over_budget_blocks_publish_fan_out():
    assert flow.after_approve(dict(BUDGET_STATE)) == "escalate"


def test_within_budget_fans_out_normally():
    s = dict(BUDGET_STATE, spend_usd=[{"usd": 0.001}])
    sends = flow.route_drafts(s)
    assert not isinstance(sends, str) and len(sends) == 1


def test_escalate_forces_drop_when_budget_burned(monkeypatch):
    from agentposthub import distill
    monkeypatch.setattr(distill, "_deepseek_chat",
                        lambda p, s: "RULE: x\nWHY: y")
    s = dict(BUDGET_STATE, retry_count=1,
             failures=[{"platform": "telegram", "node": "platform_publish",
                        "reason": "boom", "attempt": 1}])
    out = flow.escalate(s, store=get_store())   # over budget + retries
    assert out["escalation_action"] == "drop"   # no interrupt offered


# ---- kill switch ----

def test_kill_switch_pauses_publish_fail_closed():
    import agentposthub.composio as gw
    store = get_store()
    store.put(("system",), "kill_switch",
              {"publishing": False, "at": 0, "reason": "test"})
    called = []
    orig = gw.publish
    gw.publish = lambda *a, **k: called.append(1) or {"id": "x"}
    try:
        out = flow.platform_publish(
            {"task_id": "t1", "platform": "telegram",
             "draft": {"text": "x", "version": 1}}, store=store)
    finally:
        gw.publish = orig
    assert called == []                          # gateway never touched
    assert out["platform_results"][0]["paused"] is True
    assert out["failures"][0]["reason"] == "kill_switch_active"


def test_kill_switch_failure_is_not_an_incident(monkeypatch):
    from agentposthub import distill
    monkeypatch.setattr(distill, "_deepseek_chat",
                        lambda p, s: "RULE: x\nWHY: y")
    store = get_store()
    store.put(("system",), "kill_switch", {"publishing": False})
    flow.escalate({"task_id": "ks1", "retry_count": 2,
                   "grounding": {},
                   "failures": [{"platform": "telegram",
                                 "node": "platform_publish",
                                 "reason": "kill_switch_active",
                                 "attempt": 2}]}, store=store)
    incs = [i for i in (store.search(("incident",)) or [])
            if i.value["task_id"] == "ks1"]
    assert incs == []                            # human pause != incident
    store.put(("system",), "kill_switch", {"publishing": True})


# ---- recursion cap ----

def test_recursion_limit_is_explicit_and_enforced():
    import agentposthub.tripplanner as tp
    import agentposthub.flow as f
    orig = f.platform_draft
    f.platform_draft = lambda s: {"drafts": [
        {"platform": s["platform"], "text": "x", "version": 1,
         "link": None, "media": None, "tools_offered": []}]}
    tp.plan_trip = lambda *a, **k: {"ok": False, "error": "offline"}
    f.get_graph.cache_clear()
    from langgraph.errors import GraphRecursionError
    with pytest.raises(GraphRecursionError):
        get_graph().invoke(
            {"task_id": "rl1", "trace_id": "cd" * 16,
             "platforms": ["telegram"],
             "brief": {"days": 3, "budget_usd": 300},
             "platform_results": [], "failures": []},
            {"configurable": {"thread_id": "rl1"}, "recursion_limit": 2})
    f.platform_draft = orig
    f.get_graph.cache_clear()
