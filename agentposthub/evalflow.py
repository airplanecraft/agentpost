"""Flow-level benchmark harness: fixed scenario suite, whole-graph runs,
three metric layers, labelled reports for model-swap experiments.

Layers per scenario:
  model        - behaviour rates (tool-call, placeholder), consistency
  orchestration - flow path taken (escalation, retry idempotency, skip)
  system       - completion, safety invariants (reddit strip, idempotency)

The gateway and planner are mocked (same seams the test suite uses) so
scenarios are reproducible; the draft worker stays REAL - measuring the
model's actual behaviour is the point. `draft_fn` can replace it for
offline unit runs.

Reports land in evalruns/<label>_<ts>.json; scenario traces and scores
go to Langfuse under session=<label> when tracing is configured, so
model swaps compare as two sessions in the dashboard.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

REPORT_DIR = Path("evalruns")

BRIEF = {"days": 10, "budget_usd": 1500, "notes": "北京10日亲子游"}


# ------------------------------------------------------------------
# Scenario fixtures: fixed inputs + gateway behaviour + expected path
# ------------------------------------------------------------------
def _good_pub(p, t, r, trace_id=None, link=None, settings=None):
    return {"id": f"{p}_99", "ok": True, "req_id": r}


def _bad_then_good(calls):
    def fn(p, t, r, trace_id=None, link=None, settings=None):
        calls.append((p, r))
        if len(calls) == 1:
            return {"idd": "9", "ok": "true"}       # contract drift
        return {"id": "77", "ok": True, "req_id": r}
    return fn


def _recording_good(calls):
    def fn(p, t, r, trace_id=None, link=None, settings=None):
        calls.append((p, r))
        return {"id": f"{p}_99", "ok": True, "req_id": r}
    return fn


def _rb_ok(p, i, receipt=None, req_id=None, trace_id=None, settings=None):
    return {"visible": True, "reason": "bench"}


PLAN_OK = {"ok": True, "reply": "planner bench reply: total $759, route Beijing",
           "draft": {"total_usd": 759, "route_names": ["Beijing"],
                     "breakdown": {}}}
PLAN_RATE = {"ok": False, "error": "rate_limited"}


SCENARIOS = [
    {"id": "S1", "name": "normal_three_platforms",
     "platforms": ["telegram", "facebook", "reddit"],
     "publish": "recording_good", "read_back": _rb_ok, "plan": PLAN_OK,
     "expect": {"escalation": False, "skip": ["reddit"]}},
    {"id": "S2", "name": "contract_drift_then_retry",
     "platforms": ["telegram"],
     "publish": "bad_then_good", "read_back": _rb_ok, "plan": PLAN_RATE,
     "expect": {"escalation": True, "retry_idempotent": True}},
    {"id": "S3", "name": "planner_outage_degrades",
     "platforms": ["telegram"],
     "publish": "recording_good", "read_back": _rb_ok, "plan": PLAN_RATE,
     "expect": {"escalation": False, "degraded_grounding": True}},
    {"id": "S4", "name": "safety_invariants",
     "platforms": ["telegram", "reddit"],
     "publish": "recording_good", "read_back": _rb_ok, "plan": PLAN_OK,
     "expect": {"escalation": False, "skip": ["reddit"]}},
]


# ------------------------------------------------------------------
# Harness
# ------------------------------------------------------------------
def _publisher(spec, calls):
    if spec == "recording_good":
        return _recording_good(calls)
    if spec == "bad_then_good":
        return _bad_then_good(calls)
    return spec                      # a plain callable


def run_scenario(graph, scenario, label, *, draft_fn=None):
    """One whole-graph run under the scenario's mocks; returns metrics."""
    from _pytest.monkeypatch import MonkeyPatch
    from langgraph.types import Command

    import agentposthub.composio as gw
    import agentposthub.tripplanner as tp
    from . import flow
    from .tracing import hex_trace_id, name_trace, observe_config

    calls: list = []
    mp = MonkeyPatch()
    plan = dict(scenario["plan"])
    mp.setattr(gw, "publish", _publisher(scenario["publish"], calls))
    mp.setattr(gw, "read_back", scenario["read_back"])
    mp.setattr(tp, "plan_trip", lambda *a, **k: plan)
    if draft_fn is not None:
        mp.setattr(flow, "platform_draft", draft_fn)
        # the compiled graph binds node functions at build time - a
        # patched module attr needs a fresh compile to take effect
        flow.get_graph.cache_clear()
        graph = flow.get_graph()

    task_id = f"ef_{label}_{scenario['id']}"
    cfg = {"configurable": {"thread_id": task_id}, "recursion_limit": 24}
    cfg.update(observe_config(hex_trace_id(task_id)))
    try:
        t0 = time.time()
        graph.invoke({"task_id": task_id,
                      "trace_id": hex_trace_id(task_id),
                      "platforms": list(scenario["platforms"]),
                      "brief": dict(BRIEF),
                      "platform_results": [], "failures": []}, cfg)
        graph.invoke(Command(resume={"decision": "approved"}), cfg)
        escalated = bool(graph.get_state(cfg).next
                         and "escalate" in graph.get_state(cfg).next)
        if escalated:
            graph.invoke(Command(resume={"action": "retry"}), cfg)
        wall = round(time.time() - t0, 3)
        name_trace(task_id, session=label)
        v = graph.get_state(cfg).values
        return _metrics(scenario, v, calls, escalated, wall,
                        completed=graph.get_state(cfg).next == ())
    finally:
        mp.undo()
        if draft_fn is not None:
            flow.get_graph.cache_clear()   # restore the real graph


def _double_posted(calls):
    per = {}
    for p, r in calls:
        per.setdefault(p, set()).add(r)
    return any(len(rs) > 1 for rs in per.values())


def _metrics(scenario, v, calls, escalated, wall, completed):
    from .evalharness import check_number_consistency
    drafts = {d["platform"]: d for d in v.get("drafts", [])}
    vers = {x["platform"]: x for x in v.get("verifications", [])}
    plan_total = (v.get("grounding", {}).get("site_plan") or {}).get("total_usd")
    expect = scenario["expect"]

    num = [check_number_consistency(d.get("text", ""), plan_total)
           for d in drafts.values()] or [0.5]
    ds = list(drafts.values())
    n = max(len(ds), 1)
    tool_rate = round(sum(1 for d in ds if d.get("media")) / n, 3)
    marks = ("I'll create", "I will create", "(tool-only")
    ph_rate = round(sum(1 for d in ds
                        if any(mk in d.get("text", "") for mk in marks)) / n, 3)
    return {
        "scenario": scenario["id"], "name": scenario["name"],
        "wall_s": wall,
        # ---- system layer ----
        "flow_completion": 1.0 if completed else 0.0,
        "escalation_hit": 1.0 if escalated else 0.0,
        "safety_reddit_link_stripped":
            1.0 if "reddit" not in drafts
            or drafts["reddit"].get("link") is None else 0.0,
        "safety_reddit_no_image":
            1.0 if "reddit" not in drafts
            or drafts["reddit"].get("media") is None else 0.0,
        # a retry may repeat the SAME (platform, req_id) - that is
        # idempotency working. A violation is the same platform sent
        # under DIFFERENT req_ids (double-post risk).
        "idempotency_violation": 1.0 if _double_posted(calls) else 0.0,
        # ---- orchestration layer ----
        "skip_clean": 1.0 if all(
            vers.get(p, {}).get("reason") == "skipped"
            for p in expect.get("skip", [])) else 0.0,
        "retry_idempotent": (
            (1.0 if len(calls) == 2 and calls[0] == calls[1] else 0.0)
            if expect.get("retry_idempotent") else 1.0),
        "degraded_grounding_marked": (
            (1.0 if v.get("grounding", {}).get("site_plan_error") else 0.0)
            if expect.get("degraded_grounding") else 1.0),
        # ---- model layer ----
        "drafts_count": len(drafts),
        "tool_call_rate": tool_rate,
        "placeholder_rate": ph_rate,
        "number_consistency": round(sum(num) / len(num), 3),
        "gateway_calls": len(calls),
    }


def run_all(label, *, draft_fn=None, scenarios=None):
    from .flow import get_graph
    REPORT_DIR.mkdir(exist_ok=True)
    scen = scenarios or SCENARIOS
    rows = [run_scenario(get_graph(), sc, label, draft_fn=draft_fn)
            for sc in scen]
    report = {"label": label, "ts": int(time.time()),
              "model": _current_model(), "scenarios": rows}
    path = REPORT_DIR / f"{label}_{report['ts']}.json"
    path.write_text(json.dumps(report, indent=1, ensure_ascii=False))
    _push_scores(report)
    print(f"\nreport: {path}")
    _print_table(rows)
    return report


def _current_model():
    from .config import get_settings
    return get_settings().deepseek_model


def _push_scores(report):
    """Aggregate metrics as Langfuse trace scores; session=<label> groups
    the experiment so model swaps compare as sessions in the dashboard."""
    from .tracing import hex_trace_id, score, tracing_enabled
    if not tracing_enabled():
        return
    for row in report["scenarios"]:
        tid = f"ef_{report['label']}_{row['scenario']}"
        for k in ("flow_completion", "escalation_hit", "skip_clean",
                  "retry_idempotent", "tool_call_rate", "placeholder_rate",
                  "number_consistency", "safety_reddit_link_stripped"):
            score(tid, f"flow_{k}", row.get(k) or 0.0)
    try:
        import httpx
        from .config import get_settings
        s = get_settings()
        for row in report["scenarios"]:
            httpx.post(
                f"{s.langfuse_host}/api/public/traces",
                json={"id": hex_trace_id(f"ef_{report['label']}_{row['scenario']}"),
                      "name": f"ef_{row['scenario']}",
                      "sessionId": report["label"]},
                auth=(s.langfuse_public_key, s.langfuse_secret_key),
                timeout=10)
    except Exception:
        pass


_COLS = ["flow_completion", "escalation_hit", "skip_clean", "retry_idempotent",
         "tool_call_rate", "placeholder_rate", "number_consistency", "wall_s"]


def _print_table(rows):
    print("scenario   " + "  ".join(f"{c[:13]:>13}" for c in _COLS))
    for r in rows:
        print(f"{r['scenario']:10} " + "  ".join(
            f"{str(r.get(c, '-'))[:13]:>13}" for c in _COLS))


def diff(label_a: str, label_b: str) -> None:
    """Compare the most recent report of each label."""
    def latest(label):
        files = sorted(REPORT_DIR.glob(f"{label}_*.json"))
        if not files:
            raise SystemExit(f"no report for {label!r} (run evalflow first)")
        return json.loads(files[-1].read_text())

    a, b = latest(label_a), latest(label_b)
    print(f"\n== {label_a} ({a['model']})  vs  {label_b} ({b['model']}) ==")
    rb = {r["scenario"]: r for r in b["scenarios"]}
    cols = ["flow_completion", "escalation_hit", "tool_call_rate",
            "placeholder_rate", "number_consistency", "wall_s"]
    print("scenario   " + "  ".join(f"{c[:15]:>15}" for c in cols))
    for ra in a["scenarios"]:
        rr = rb.get(ra["scenario"], {})
        print(f"{ra['scenario']:10} " + "  ".join(
            f"{str(ra.get(c, '-'))[:7]:>7}→{str(rr.get(c, '-'))[:7]:<8}"
            for c in cols))
