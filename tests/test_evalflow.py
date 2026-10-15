"""evalflow offline: deterministic draft stub, mocked gateway/planner —
the harness, metrics and report shape are what's under test here (the
REAL runs live in `python -m agentposthub evalflow --label X`)."""
import json

from agentposthub import evalflow


def _stub_draft(state):
    p = state["platform"]
    from agentposthub.flow import PLATFORM_RULES, tools_for
    allow_link = PLATFORM_RULES[p]["allow_link"]
    return {"drafts": [{
        "platform": p,
        "text": f"{p} post total $759 breakdown" if p != "reddit"
        else f"{p} text-only post, total $759, no links",
        "link": "https://x.example" if allow_link else None,
        "media": {"kind": "image"} if PLATFORM_RULES[p]["allow_image"] else None,
        "version": 1,
        "tools_offered": [t["function"]["name"]
                          for t in tools_for(p, "hard")]}]}


def test_evalflow_report(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)          # reports land in ./evalruns
    report = evalflow.run_all("unit", draft_fn=_stub_draft,
                              scenarios=evalflow.SCENARIOS)
    by = {r["scenario"]: r for r in report["scenarios"]}

    # S1: three drafts, differentiated, telegram+fb published, reddit skipped
    assert by["S1"]["drafts_count"] == 3
    assert by["S1"]["flow_completion"] == 1.0
    assert by["S1"]["skip_clean"] == 1.0
    assert by["S1"]["escalation_hit"] == 0.0
    assert by["S1"]["tool_call_rate"] == round(2 / 3, 3)

    # S2: contract drift -> escalation -> retry same req_id (idempotent)
    assert by["S2"]["escalation_hit"] == 1.0
    assert by["S2"]["retry_idempotent"] == 1.0
    assert by["S2"]["idempotency_violation"] == 0.0

    # S3: planner outage -> grounding marked, flow completes
    assert by["S3"]["degraded_grounding_marked"] == 1.0
    assert by["S3"]["flow_completion"] == 1.0

    # S4: safety invariants - reddit stripped of link AND image
    assert by["S4"]["safety_reddit_link_stripped"] == 1.0
    assert by["S4"]["safety_reddit_no_image"] == 1.0

    # report file written with label+model
    files = list((tmp_path / "evalruns").glob("unit_*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert data["label"] == "unit" and len(data["scenarios"]) == 4
