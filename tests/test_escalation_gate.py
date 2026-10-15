"""Gate #2 batch semantics on the multi-platform graph (suspension and
targeted retry live in test_graph_multibranch.py; here: bounded retries
and drop bookkeeping)."""
import agentposthub.composio as gw
import agentposthub.tripplanner as tp
from agentposthub import distill, flow
from agentposthub.flow import get_graph
from langgraph.types import Command


def _patch(monkeypatch, publish_fn):
    def fake_draft(state):
        return {"drafts": [{"platform": state["platform"], "text": "post",
                            "version": 1, "link": None, "media": None,
                            "tools_offered": []}]}
    monkeypatch.setattr(flow, "platform_draft", fake_draft)
    monkeypatch.setattr(tp, "plan_trip",
                        lambda *a, **k: {"ok": False, "error": "offline"})
    monkeypatch.setattr(gw, "publish", publish_fn)


def _fresh():
    flow.get_graph.cache_clear()
    return get_graph()


BAD = lambda *a, **k: {"idd": "9", "ok": "true"}


def _start_failed(graph, task_id):
    cfg = {"configurable": {"thread_id": task_id}}
    graph.invoke({"task_id": task_id, "trace_id": "cd" * 16,
                  "platforms": ["telegram"], "retry_count": 0,
                  "brief": {"days": 3, "budget_usd": 300},
                  "platform_results": [], "failures": []}, cfg)
    graph.invoke(Command(resume={"decision": "approved"}), cfg)
    return cfg


def test_drop_records_incidents(monkeypatch):
    _patch(monkeypatch, BAD)
    graph = _fresh()
    cfg = _start_failed(graph, "drop_m2")
    assert "escalate" in graph.get_state(cfg).next
    graph.invoke(Command(resume={"action": "drop"}), cfg)

    v = graph.get_state(cfg).values
    assert graph.get_state(cfg).next == ()
    assert v["escalation_action"] == "drop"
    incs = distill._undistilled(flow.get_store())
    assert any(i.value["task_id"] == "drop_m2" for i in incs)


def test_retries_are_bounded_then_forced_drop(monkeypatch):
    _patch(monkeypatch, BAD)
    graph = _fresh()
    cfg = _start_failed(graph, "bound_m2")
    for _ in range(flow.MAX_PUBLISH_RETRIES):
        assert "escalate" in graph.get_state(cfg).next
        graph.invoke(Command(resume={"action": "retry"}), cfg)
    # exhausted: even if another retry is requested, the gate drops
    snap = graph.get_state(cfg)
    if snap.next and "escalate" in snap.next:
        graph.invoke(Command(resume={"action": "retry"}), cfg)  # ignored
    v = graph.get_state(cfg).values
    assert graph.get_state(cfg).next == ()
    assert v["escalation_action"] == "drop"
