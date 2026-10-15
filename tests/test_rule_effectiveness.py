"""Rule effectiveness loop: injection stamps applied_count, failure
with lessons active stamps failures_while_applied — retirement
decisions become data-driven."""
from agentposthub import distill, flow
from agentposthub.flow import get_store


def _approved(key="prop_e", rule="RE"):
    store = get_store()
    store.put(distill.RULE_NS, key,
              {"rule": rule, "rationale": "", "status": "approved",
               "created_at": 0, "incident_keys": []})
    return store


def test_injection_stamps_applied_count(monkeypatch):
    import agentposthub.tripplanner as tp
    monkeypatch.setattr(tp, "plan_trip",
                        lambda *a, **k: {"ok": False, "error": "x"})
    store = _approved()
    out = flow.retrieve_context(
        {"task_id": "t", "platform": "telegram",
         "brief": {"cities": 1, "days": 3, "budget_usd": 300}},
        store=store)
    assert out["grounding"]["lesson_keys"] == ["prop_e"]
    rec = store.get(distill.RULE_NS, "prop_e")
    assert rec.value["applied_count"] == 1
    assert "last_applied_at" in rec.value


def test_failure_with_lesson_active_bumps_effectiveness_counter(monkeypatch):
    monkeypatch.setattr(distill, "_deepseek_chat", lambda p, s: "RULE: x\nWHY: y")
    store = _approved()
    state = {"task_id": "t9", "platform": "telegram",
             "retry_count": flow.MAX_PUBLISH_RETRIES,
             "grounding": {"lesson_keys": ["prop_e"]},
             "failures": [{"node": "publish", "reason": "boom"}]}
    flow.escalate(state, store=store)
    rec = store.get(distill.RULE_NS, "prop_e")
    assert rec.value["failures_while_applied"] == 1


def test_success_with_lesson_does_not_bump_failure_counter():
    store = _approved()
    state = {"task_id": "t10", "platform": "telegram",
             "retry_count": flow.MAX_PUBLISH_RETRIES,   # no interrupt offer
             "grounding": {"lesson_keys": ["prop_e"]},
             "failures": []}                 # task succeeded -> no stamp
    flow.escalate(state, store=store)
    rec = store.get(distill.RULE_NS, "prop_e")
    assert rec.value.get("failures_while_applied", 0) == 0


def test_stamp_ignores_retired_and_unknown():
    store = _approved("prop_g")
    store.put(distill.RULE_NS, "prop_h",
              {"rule": "RH", "rationale": "", "status": "retired",
               "created_at": 0, "incident_keys": []})
    distill.stamp_applied(store, ["prop_g", "prop_h", "nope"])
    assert store.get(distill.RULE_NS, "prop_g").value["applied_count"] == 1
    retired = store.get(distill.RULE_NS, "prop_h").value
    assert "applied_count" not in retired    # retired rule not stamped
