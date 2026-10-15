from agentposthub import flow

BASE = {"task_id": "t1", "platform": "telegram", "brief": {},
        "draft": {"text": "China Travel Budget post", "version": 1},
        "regen_count": 0}


def test_low_similarity_passes_through():
    out = flow.dedup_guard(dict(BASE))
    assert out == {}


def test_near_duplicate_bounces_for_regen(monkeypatch):
    monkeypatch.setattr(flow, "_semantic_similarity", lambda t: 0.95)
    out = flow.dedup_guard(dict(BASE))
    assert out["regen_count"] == 1
    assert "[variant" in out["draft"]["text"]


def test_regen_budget_exhaustion_escalates(monkeypatch):
    monkeypatch.setattr(flow, "_semantic_similarity", lambda t: 0.95)
    state = dict(BASE, regen_count=2)
    out = flow.dedup_guard(state)
    assert out["failures"][0]["reason"] == "regeneration budget exhausted"


def test_router_sends_back_to_generate_until_variant_count_catches_up():
    state = dict(BASE, regen_count=1,
                 draft={"text": "post [variant 0.95]", "version": 2})
    # variant marker count (1) >= regen_count (1) -> proceed to approval
    assert flow.after_dedup(state) == "approve"
    state2 = dict(BASE, regen_count=2,
                  draft={"text": "post [variant 0.95]", "version": 2})
    # one variant marker but two regens -> one more generate pass needed
    assert flow.after_dedup(state2) == "generate"
