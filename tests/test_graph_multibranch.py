"""Multi-platform orchestration: one brief fans out to differentiated
per-platform draft workers, one approval gate covers all drafts, publish
workers run in parallel with per-platform skip/failure isolation, and
the batch escalation gate retries only failed platforms."""
import agentposthub.composio as gw
import agentposthub.tripplanner as tp
from agentposthub import flow
from agentposthub.flow import get_graph

GOOD = lambda p, t, r, trace_id=None, settings=None: {"id": f"{p}_99",
                                                      "ok": True, "req_id": r}


def _patch_workers(monkeypatch):
    """Deterministic platform_draft: reddit has no image/no link; the
    others keep both. No network."""
    def fake_draft(state):
        p = state["platform"]
        allow_link = flow.PLATFORM_RULES[p]["allow_link"]
        return {"drafts": [{
            "platform": p,
            "text": f"{p} post about the trip, total $759",
            "link": "https://x.example" if allow_link else None,
            "media": {"kind": "image"} if flow.PLATFORM_RULES[p]["allow_image"] else None,
            "version": 1,
            "tools_offered": [t["function"]["name"]
                              for t in flow.tools_for(p, "hard")]}]}
    monkeypatch.setattr(flow, "platform_draft", fake_draft)
    monkeypatch.setattr(tp, "plan_trip",
                        lambda *a, **k: {"ok": False, "error": "offline"})


def _fresh():
    flow.get_graph.cache_clear()
    return get_graph()


def _start(graph, task_id, platforms=("telegram", "reddit")):
    cfg = {"configurable": {"thread_id": task_id}}
    graph.invoke({"task_id": task_id, "trace_id": "ab" * 16,
                  "platforms": list(platforms),
                  "brief": {"days": 3, "budget_usd": 300},
                  "platform_results": [], "failures": []}, cfg)
    return cfg


def test_reddit_draft_is_structurally_different(monkeypatch):
    _patch_workers(monkeypatch)
    graph = _fresh()
    cfg = _start(graph, "diff_e2e")
    snap = graph.get_state(cfg)
    assert snap.next and "approve" in snap.next
    by_p = {d["platform"]: d for d in snap.values["drafts"]}
    assert by_p["reddit"]["link"] is None            # hard rule in code
    assert by_p["reddit"]["tools_offered"] == []     # image tool never offered
    assert by_p["telegram"]["link"] is not None
    assert "create_image_content" in by_p["telegram"]["tools_offered"]


def test_parallel_publish_skip_and_success(monkeypatch):
    """telegram publishes (mocked gateway), reddit is skipped by the
    gateway (no action configured) - skipped is not failed: no
    escalation, ledger row only for telegram."""
    _patch_workers(monkeypatch)
    monkeypatch.setattr(gw, "publish", GOOD)
    graph = _fresh()
    cfg = _start(graph, "skip_e2e")
    from langgraph.types import Command
    graph.invoke(Command(resume={"decision": "approved"}), cfg)

    v = graph.get_state(cfg).values
    assert graph.get_state(cfg).next == ()            # ended, no escalation
    skipped = [r for r in v["platform_results"] if r.get("skipped")]
    assert {r["platform"] for r in skipped} == {"reddit"}
    ok = [r for r in v["platform_results"] if not r.get("skipped")]
    assert {r["platform"] for r in ok} == {"telegram"}
    assert v["failures"] == []
    rows = flow.get_db().execute(
        "SELECT platform, post_ref FROM publish_history "
        "WHERE task_id='skip_e2e'").fetchall()
    assert rows == [("telegram", "telegram_99")]


def test_failed_platform_goes_to_batch_gate_and_retry_is_targeted(monkeypatch):
    """telegram fails (contract violation), reddit skips cleanly; the
    batch gate suspends once, retry re-publishes ONLY telegram with the
    same idempotency key."""
    _patch_workers(monkeypatch)
    graph = _fresh()

    calls = []
    def bad_then_good(p, t, r, trace_id=None, settings=None):
        calls.append((p, r))
        if len(calls) == 1:
            return {"idd": "9", "ok": "true"}         # schema drift
        return {"id": "77", "ok": True, "req_id": r}
    monkeypatch.setattr(gw, "publish", bad_then_good)

    cfg = _start(graph, "retry_e2e")
    from langgraph.types import Command
    graph.invoke(Command(resume={"decision": "approved"}), cfg)

    snap = graph.get_state(cfg)
    assert snap.next and "escalate" in snap.next      # batch gate suspended
    failed_plats = {f["platform"] for f in snap.values["failures"]}
    assert failed_plats == {"telegram"}               # reddit skipped, not failed

    graph.invoke(Command(resume={"action": "retry"}), cfg)
    v = graph.get_state(cfg).values
    assert graph.get_state(cfg).next == ()
    assert v["retry_count"] == 1
    # retry re-published ONLY telegram: second call is telegram again,
    # with the SAME req_id (idempotency - platform dedupes)
    assert calls[0][0] == "telegram" and calls[1][0] == "telegram"
    assert calls[0][1] == calls[1][1] == "retry_e2e#1"
    assert any(r.get("comment_id") == "77"
               for r in v["verifications"] if r["platform"] == "telegram")


def test_rejected_ends_without_publish(monkeypatch):
    _patch_workers(monkeypatch)
    monkeypatch.setattr(gw, "publish", GOOD)
    graph = _fresh()
    cfg = _start(graph, "rej_e2e")
    from langgraph.types import Command
    graph.invoke(Command(resume={"decision": "rejected"}), cfg)
    assert graph.get_state(cfg).next == ()
    assert graph.get_state(cfg).values["platform_results"] == []
