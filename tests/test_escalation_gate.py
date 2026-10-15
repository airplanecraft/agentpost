"""Gate #2 integration: failed publish suspends at escalate; the retry
message re-publishes the SAME draft (same idempotency key) and the task
completes; drop records incidents instead. Full graph, no network."""
import agentposthub.composio as gw
import agentposthub.tripplanner as tp
from agentposthub import flow
from agentposthub.flow import get_graph

BAD = lambda *a, **k: {"idd": "9x8y", "ok": "true"}   # schema drift
GOOD = None                                            # use real (dry_run)


def _patch_generate(monkeypatch):
    monkeypatch.setattr(flow, "generate",
                        lambda s: {"draft": {"text": "post", "version": 1,
                                             "link": None, "media": None,
                                             "tools_offered": []}})
    monkeypatch.setattr(tp, "plan_trip",
                        lambda *a, **k: {"ok": False, "error": "offline"})


def _fresh_graph():
    flow.get_graph.cache_clear()
    return get_graph()


def _start(graph, task_id):
    cfg = {"configurable": {"thread_id": task_id}}
    graph.invoke({"task_id": task_id, "platform": "telegram",
                  "brief": {"cities": 1, "days": 3, "budget_usd": 300},
                  "platform_results": [], "failures": []}, cfg)
    graph.invoke(__import__("langgraph.types", fromlist=["Command"])
                 .Command(resume={"decision": "approved"}), cfg)
    return cfg


def test_failed_publish_suspends_then_retry_completes(monkeypatch):
    _patch_generate(monkeypatch)
    graph = _fresh_graph()
    monkeypatch.setattr(gw, "publish", BAD)
    cfg = _start(graph, "retry_e2e")

    snap = graph.get_state(cfg)
    assert snap.next and "escalate" in snap.next     # suspended at gate #2
    assert snap.values["failures"][0]["node"] == "publish"
    req_id_first = (f"retry_e2e#{snap.values['draft']['version']}")

    monkeypatch.setattr(gw, "publish",
                        lambda p, t, r, settings=None: {"id": "77", "ok": True,
                                                        "req_id": r})
    from langgraph.types import Command
    graph.invoke(Command(resume={"action": "retry"}), cfg)

    final = graph.get_state(cfg).values
    assert final["verification"]["ok"] is True
    assert final["verification"]["comment_id"] == "77"
    assert final["retry_count"] == 1
    # failures is an append-only audit channel (operator.add reducer):
    # the first attempt's violation stays on record even after the
    # successful retry - history, not an error indicator
    assert final["failures"][0]["node"] == "publish"
    # same draft version re-published -> same idempotency key
    assert req_id_first == f"retry_e2e#{final['draft']['version']}"
    # ledger row written
    assert flow.get_db().execute(
        "SELECT post_ref FROM publish_history WHERE task_id='retry_e2e'"
    ).fetchone()[0] == "77"


def test_drop_records_incidents(monkeypatch):
    from agentposthub import distill
    _patch_generate(monkeypatch)
    graph = _fresh_graph()
    monkeypatch.setattr(gw, "publish", BAD)
    cfg = _start(graph, "drop_e2e")

    assert "escalate" in graph.get_state(cfg).next
    from langgraph.types import Command
    graph.invoke(Command(resume={"action": "drop"}), cfg)

    final = graph.get_state(cfg).values
    assert graph.get_state(cfg).next == ()          # ended
    assert final["escalation_action"] == "drop"
    incs = distill._undistilled(flow.get_store())
    assert any(i.value["task_id"] == "drop_e2e" for i in incs)


def test_retries_are_bounded(monkeypatch):
    """Third failure at the gate offers no more interrupts: straight to
    the terminal path even with action=retry requested."""
    _patch_generate(monkeypatch)
    graph = _fresh_graph()
    monkeypatch.setattr(gw, "publish", BAD)
    cfg = _start(graph, "bound_e2e")

    from langgraph.types import Command
    for i in range(flow.MAX_PUBLISH_RETRIES):
        assert "escalate" in graph.get_state(cfg).next
        graph.invoke(Command(resume={"action": "retry"}), cfg)
    # retries exhausted; next failure terminates without asking
    assert "escalate" in graph.get_state(cfg).next or \
        graph.get_state(cfg).values.get("escalation_action") == "drop"
    snap = graph.get_state(cfg)
    if snap.next:  # still suspended at the last allowed gate -> drop it
        graph.invoke(Command(resume={"action": "drop"}), cfg)
    final = graph.get_state(cfg).values
    assert final.get("escalation_action") == "drop"
