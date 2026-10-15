"""Generation evaluation: deterministic checks over drafts, scored and
(optionally) pushed to Langfuse as trace scores.

The headline check is number consistency: the grounding pack carries the
reference planner's total_usd, and the draft must not invent a
different one. This is the eval that catches a model quietly overriding
its authoritative facts.
"""
from __future__ import annotations

import re

TELEGRAM_MAX = 4096


def extract_usd_totals(text: str) -> list[int]:
    """All $ amounts (no cents) mentioned in a text; comma grouping ok."""
    found = re.findall(r"\$\s?([0-9]{1,3}(?:,[0-9]{3})+|[0-9]{3,5})(?![0-9])",
                       text)
    return [int(m.replace(",", "")) for m in found]


def check_number_consistency(draft_text: str, planner_total) -> float:
    """1.0 when the planner's total appears verbatim in the draft
    (± rounding to tens), 0.0 otherwise. Planner missing -> 0.5 (the
    draft must then label numbers as estimated; not enforced here)."""
    if planner_total is None:
        return 0.5
    want = {planner_total,
            int(round(planner_total / 10.0) * 10)}
    found = set(extract_usd_totals(draft_text))
    return 1.0 if found & want else 0.0


def check_length(draft_text: str, limit: int = TELEGRAM_MAX) -> float:
    return 1.0 if len(draft_text) <= limit else 0.0


def check_placeholder_text(draft_text: str) -> float:
    """The draft must be real post copy, not an announcement ("I'll
    create...") or a rejected-tool note. Shares the predicate with the
    generate-time chase so eval and generation agree on the definition."""
    from .flow import looks_like_placeholder
    return 0.0 if looks_like_placeholder(draft_text) else 1.0


def evaluate_draft(draft_text: str, grounding: dict) -> dict:
    plan = grounding.get("site_plan") or {}
    checks = {
        "number_consistency": check_number_consistency(
            draft_text, plan.get("total_usd")),
        "length_limit": check_length(draft_text),
        "real_copy": check_placeholder_text(draft_text),
    }
    checks["score"] = round(sum(checks.values()) / len(checks), 3)
    return checks


def eval_run(graph, platform: str, briefs: list[dict],
             session_id: str) -> list[dict]:
    """Run N tasks up to the approval gate (generation + grounding only;
    nothing publishes), score each draft, push scores to Langfuse when
    configured. Returns the per-task score rows."""
    from . import tracing
    from .flow import get_graph  # noqa: F401  (graph passed in for tests)
    rows = []
    for brief in briefs:
        task_id = f"eval_{session_id}_{len(rows):02d}"
        cfg = {"configurable": {"thread_id": task_id}}
        cfg.update(tracing.observe_config(tracing.hex_trace_id(task_id)))
        graph.invoke({"task_id": task_id,
                      "trace_id": tracing.hex_trace_id(task_id),
                      "platforms": [platform],
                      "brief": brief, "platform_results": [],
                      "failures": []}, cfg)
        tracing.name_trace(task_id)
        snap = graph.get_state(cfg)
        grounding = snap.values.get("grounding", {})
        from .config import get_settings as _gs
        use_judge = _gs().judge_enabled
        for d in snap.values.get("drafts", []) or [{}]:
            draft = d.get("text", "")
            result = evaluate_draft(draft, grounding)
            if use_judge:
                from .judge import judge_text
                v = judge_text(draft, d.get("platform", "?"),
                               str(brief.get("notes", "")))
                if v:
                    result["judge_quality"] = v["score"] / 5.0
                    result["judge_reason"] = v["reasons"]
            for name, value in result.items():
                if name not in ("score", "judge_reason"):
                    tracing.score(task_id, name, value)
            tracing.score(task_id, "overall", result["score"])
            row = {"task_id": task_id, "platform": d.get("platform", "?"),
                   **result,
                   "planner_total": (grounding.get("site_plan") or {}
                                     ).get("total_usd"),
                   "draft_head": draft[:60]}
            rows.append(row)
            print(f"  {row['task_id']}[{row['platform']}]: "
                  f"overall={row['score']} "
                  f"num={row['number_consistency']} "
                  f"len={row['length_limit']} copy={row['real_copy']} "
                  f"judge={row.get('judge_quality', '-')} "
                  f"(planner ${row['planner_total']})")
    tracing.flush()
    return rows
