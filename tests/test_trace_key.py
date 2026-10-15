"""The trace id as a first-class constraint key: written into FlowState
at creation, persisted by the checkpointer, read back on resume — so
spans from any process/day join the same observability tree."""
from agentposthub import tracing
from agentposthub.flow import get_graph


def test_resolve_prefers_persisted_state_over_derivation():
    derived = tracing.hex_trace_id("task_x")
    # state carries a DIFFERENT id (e.g. migrated/assigned) -> it wins
    state = {"trace_id": "f" * 32}
    assert tracing.resolve_trace_id("task_x", state) == "f" * 32
    # no state / no field -> deterministic fallback
    assert tracing.resolve_trace_id("task_x", None) == derived
    assert tracing.resolve_trace_id("task_x", {}) == derived


def test_trace_id_survives_into_checkpoint(monkeypatch):
    import agentposthub.tripplanner as tp
    from agentposthub import flow
    monkeypatch.setattr(flow, "platform_draft",
                        lambda s: {"drafts": [{
                            "platform": s["platform"], "text": "post",
                            "version": 1, "link": None, "media": None,
                            "tools_offered": []}]})
    monkeypatch.setattr(tp, "plan_trip",
                        lambda *a, **k: {"ok": False, "error": "offline"})
    flow.get_graph.cache_clear()
    graph = get_graph()

    tid = tracing.hex_trace_id("mech_e2e")
    cfg = {"configurable": {"thread_id": "mech_e2e"}}
    graph.invoke({"task_id": "mech_e2e", "trace_id": tid,
                  "platform": "telegram",
                  "brief": {"cities": 1, "days": 3, "budget_usd": 300},
                  "platform_results": [], "failures": []}, cfg)

    snap = graph.get_state(cfg)
    assert snap.values["trace_id"] == tid            # persisted in checkpoint
    # a resuming process reads it from state, not from re-derivation
    assert tracing.resolve_trace_id("mech_e2e", snap.values) == tid
    # and it is NOT what blind derivation of a different key would give
    assert tid != tracing.hex_trace_id("something_else")
