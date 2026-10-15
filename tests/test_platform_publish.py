"""platform_publish unit coverage at the gateway seam: success,
contract violation, bounded gateway failure, read-back trust downgrade,
skip for unconfigured platforms."""
import agentposthub.composio as gw
from agentposthub import flow

SLICE = {"task_id": "t1", "platform": "telegram",
         "draft": {"text": "post", "version": 1,
                   "link": "https://x.example"}}


def test_publish_success_contract_ok(monkeypatch):
    monkeypatch.setattr(gw, "publish",
                        lambda p, t, r, trace_id=None, settings=None:
                        {"id": "42", "ok": True, "req_id": "t1#1"})
    out = flow.platform_publish(dict(SLICE))
    assert out["platform_results"][0]["id"] == "42"
    assert out["verifications"][0]["ok"] is True
    assert "failures" not in out


def test_publish_contract_violation_records_failure(monkeypatch):
    monkeypatch.setattr(gw, "publish",
                        lambda p, t, r, trace_id=None, settings=None:
                        {"idd": "42", "ok": "true"})
    out = flow.platform_publish(dict(SLICE))
    assert out["platform_results"][0]["ok"] is False
    assert "missing field" in out["failures"][0]["reason"]
    assert out["failures"][0]["attempt"] == 0


def test_publish_gateway_failure_is_bounded(monkeypatch):
    def boom(*a, **k):
        raise gw.GatewayError("failed after 3 attempts: timeout")
    monkeypatch.setattr(gw, "publish", boom)
    out = flow.platform_publish(dict(SLICE))
    assert "3 attempts" in out["failures"][0]["reason"]


def test_unconfigured_platform_is_skipped_not_failed():
    # reddit is deliberately gateway-gated (public-community risk)
    out = flow.platform_publish({"task_id": "t1", "platform": "reddit",
                                 "draft": {"text": "x", "version": 1}})
    assert out["platform_results"][0]["skipped"] is True
    assert out["verifications"][0]["reason"] == "skipped"
    assert "failures" not in out


def test_readback_failure_downgrades_trust(monkeypatch):
    monkeypatch.setattr(gw, "publish",
                        lambda p, t, r, trace_id=None, settings=None:
                        {"id": "42", "ok": True, "req_id": "t1#1"})
    monkeypatch.setattr(gw, "read_back",
                        lambda p, i, receipt=None, req_id=None,
                        trace_id=None, settings=None:
                        {"visible": False, "reason": "removed_by_mod"})
    store = flow.get_store()
    out = flow.platform_publish(dict(SLICE), store=store)
    assert out["verifications"][0]["ok"] is False
    rec = store.get(("account", "telegram"), "trust")
    assert rec.value["tier"] == "building"
    assert rec.value["incidents"] == 1


def test_gateway_dry_run_shapes_pass_contract():
    resp = gw.publish("telegram", "hi", "t1#1")
    assert flow._check_output_contract(resp, "t1#1") is None
    assert resp["via"] == "dry-run"
    assert gw.read_back("telegram", resp["id"])["visible"] is True
