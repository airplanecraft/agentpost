"""Publish/verify node behaviour with the gateway faked at the seam:
success path, contract-violation path, gateway failure path, and the
memory-driven trust downgrade on read-back mismatch."""
from agentposthub import composio, flow

BASE = {"task_id": "t1", "platform": "telegram", "brief": {},
        "draft": {"text": "post", "version": 1, "link": "https://x.example"}}


def test_publish_success_contract_ok(monkeypatch):
    monkeypatch.setattr(composio, "publish",
                        lambda p, t, r, settings=None: {"id": "42", "ok": True,
                                                        "req_id": "t1#1"})
    out = flow.publish(dict(BASE))
    assert out["platform_results"][0]["id"] == "42"


def test_publish_contract_violation_records_failure(monkeypatch):
    monkeypatch.setattr(composio, "publish",
                        lambda p, t, r, settings=None: {"idd": "42", "ok": "true"})
    out = flow.publish(dict(BASE))
    assert out["platform_results"][0]["ok"] is False
    assert out["failures"][0]["node"] == "publish"
    assert "missing field" in out["failures"][0]["reason"]


def test_publish_gateway_failure_is_bounded(monkeypatch):
    def boom(*a, **k):
        raise composio.GatewayError("failed after 3 attempts: timeout")
    monkeypatch.setattr(composio, "publish", boom)
    out = flow.publish(dict(BASE))
    assert out["platform_results"][0]["ok"] is False
    assert "3 attempts" in out["failures"][0]["reason"]


def _state_after_publish():
    return dict(BASE, platform_results=[{"platform": "telegram", "id": "42",
                                         "ok": True, "req_id": "t1#1"}])


def test_verify_confirms_visible(monkeypatch):
    monkeypatch.setattr(composio, "read_back",
                        lambda p, i, receipt=None, settings=None: {"visible": True, "reason": "ok"})
    out = flow.verify(_state_after_publish())
    assert out["verification"]["ok"] is True
    assert out["verification"]["comment_id"] == "42"


def test_verify_downgrades_trust_on_removal(monkeypatch):
    monkeypatch.setattr(composio, "read_back",
                        lambda p, i, receipt=None, settings=None: {"visible": False,
                                                                  "reason": "removed_by_mod"})
    store = flow.get_store()
    before = store.get(("account", "telegram"), "trust")
    assert before is None  # only reddit is seeded with baseline trust
    out = flow.verify(_state_after_publish(), store=store)
    assert out["verification"]["ok"] is False
    rec = store.get(("account", "telegram"), "trust")
    assert rec.value["tier"] == "building"
    assert rec.value["incidents"] == 1


def test_gateway_dry_run_shapes_pass_contract():
    resp = composio.publish("telegram", "hi", "t1#1")
    assert flow._check_output_contract(resp, "t1#1") is None
    assert resp["via"] == "dry-run"
    assert composio.read_back("telegram", resp["id"])["visible"] is True
