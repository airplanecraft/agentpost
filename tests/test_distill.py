"""Rule distillation loop: incidents -> LLM proposal -> human gate ->
active rule injected into grounding."""
from agentposthub import breaker, distill, flow
from agentposthub.flow import get_store

LLM_STUB = "RULE: Validate the publish response shape before treating a\nWHY: silent schema drift caused every incident"


def _record(store, n, reason="output_contract: missing field: id"):
    for i in range(n):
        distill.record_incident(store, f"t{i}", "telegram",
                                "publish", reason)


def test_incidents_accumulate_cross_session():
    _record(get_store(), 2)
    # "new session": fresh handle on the same db
    flow.get_store.cache_clear()
    assert len(distill._undistilled(get_store())) == 2


def test_below_threshold_no_distillation(monkeypatch):
    store = get_store()
    _record(store, distill.DISTILL_THRESHOLD - 1)
    called = []
    monkeypatch.setattr(distill, "_deepseek_chat",
                        lambda p, s: called.append(1) or LLM_STUB)
    assert distill.maybe_distill(store) is None
    assert not called


def test_threshold_distills_pending_proposal(monkeypatch):
    store = get_store()
    _record(store, distill.DISTILL_THRESHOLD)
    monkeypatch.setattr(distill, "_deepseek_chat", lambda p, s: LLM_STUB)
    prop = distill.maybe_distill(store)
    assert prop and prop["status"] == "pending"
    assert prop["rule"].startswith("Validate")
    # incidents consumed - a fourth incident alone won't re-distill
    assert len(distill._undistilled(store)) == 0


def test_proposal_inactive_until_human_gate():
    store = get_store()
    store.put(distill.RULE_NS, "prop_x",
              {"rule": "R", "rationale": "", "status": "pending",
               "created_at": 0, "incident_keys": []})
    assert distill.active_rules(store) == []          # pending != active
    assert distill.decide_proposal(store, "prop_x", approve=True)
    assert distill.active_rules(store) == ["R"]


def test_rejected_rule_never_activates():
    store = get_store()
    store.put(distill.RULE_NS, "prop_y",
              {"rule": "R2", "rationale": "", "status": "pending",
               "created_at": 0, "incident_keys": []})
    distill.decide_proposal(store, "prop_y", approve=False)
    assert distill.active_rules(store) == []


def test_retire_lifecycle_after_bugfix():
    """Root cause fixed -> retire -> lesson disappears from active set,
    record kept as audit history with retired_at."""
    store = get_store()
    store.put(distill.RULE_NS, "prop_r",
              {"rule": "R3", "rationale": "", "status": "approved",
               "created_at": 0, "incident_keys": []})
    assert distill.active_rules(store) == ["R3"]
    assert distill.retire_rule(store, "prop_r") is True
    assert distill.active_rules(store) == []          # lesson 停止注入
    rec = store.get(distill.RULE_NS, "prop_r")
    assert rec.value["status"] == "retired"           # 记录保留可审计
    assert "retired_at" in rec.value
    # already retired -> second retire refused (idempotent guard)
    assert distill.retire_rule(store, "prop_r") is False
    # pending proposal cannot be retired - reject it instead
    store.put(distill.RULE_NS, "prop_s",
              {"rule": "R4", "rationale": "", "status": "pending",
               "created_at": 0, "incident_keys": []})
    assert distill.retire_rule(store, "prop_s") is False


def test_grounding_carries_approved_lessons(monkeypatch):
    import agentposthub.tripplanner as tp
    monkeypatch.setattr(tp, "plan_trip",
                        lambda msg, locale="zh", settings=None: {"ok": False,
                                                                "error": "x"})
    store = get_store()
    store.put(distill.RULE_NS, "prop_z",
              {"rule": "Always recheck the ledger before republishing",
               "rationale": "", "status": "approved",
               "created_at": 0, "incident_keys": []})
    out = flow.retrieve_context(
        {"task_id": "t", "platform": "telegram",
         "brief": {"cities": 1, "days": 3, "budget_usd": 300}},
        store=store)
    assert out["grounding"]["lessons"] == \
        ["Always recheck the ledger before republishing"]


def test_escalate_node_records_and_distills(monkeypatch):
    store = get_store()
    monkeypatch.setattr(distill, "_deepseek_chat", lambda p, s: LLM_STUB)
    flow.escalate({"task_id": "t9", "platform": "telegram",
                   "failures": [{"node": "publish", "reason": "boom"}]},
                  store=store)
    assert len(distill._undistilled(store)) == 1
