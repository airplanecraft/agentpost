"""Command-line surface for AgentPostHub (no UI by design).

The approval gate is operated from the CLI: `run` suspends at the
interrupt and prints the draft; a second command resumes the graph with
the human decision. Because state lives in the sqlite checkpointer, the
resume can happen in a different process, hours or days later.

Usage:
  python -m agentposthub run --platform telegram --cities 3 --days 10 --budget 1500
  python -m agentposthub list
  python -m agentposthub show <task_id>
  python -m agentposthub approve <task_id>
  python -m agentposthub reject <task_id>
  python -m agentposthub edit <task_id> --text "new text"
"""
from __future__ import annotations

import argparse
import uuid

from langgraph.types import Command

from .config import get_settings
from .flow import get_db, get_graph


def _cfg(task_id: str) -> dict:
    return {"configurable": {"thread_id": task_id}}


def cmd_run(args) -> int:
    settings = get_settings()
    for w in settings.warnings:
        print(f"warning: {w}")
    task_id = args.task_id or f"task_{args.platform}_{uuid.uuid4().hex[:8]}"
    graph = get_graph()
    print(f"running {task_id} on {args.platform} "
          f"(publish_mode={settings.publish_mode})")
    graph.invoke({"task_id": task_id, "platform": args.platform,
                  "brief": {"cities": args.cities, "days": args.days,
                            "budget_usd": args.budget,
                            **({"notes": args.notes} if args.notes else {})},
                  "platform_results": [], "failures": []}, _cfg(task_id))

    snap = graph.get_state(_cfg(task_id))
    if snap.next and "approve" in snap.next:
        draft = snap.values["draft"]
        print(f"\nsuspended at the approval gate (next node: {snap.next[0]})")
        print(f"  task_id:   {task_id}")
        print(f"  platform:  {args.platform}")
        print(f"  tools:     {draft.get('tools_offered')}")
        print(f"  media:     {draft.get('media') or 'text only, no tool call'}")
        print(f"  draft:     {draft['text']}")
        print(f"\nreview, then: python -m agentposthub approve {task_id}")
    else:
        print(f"\nfinished without suspension; state: {snap.values.get('verification')}")
    return 0


def cmd_list(args) -> int:  # noqa: ARG001
    db = get_db()
    graph = get_graph()
    rows = db.execute("SELECT task_id, platform FROM pending_reviews").fetchall()
    if not rows:
        print("no pending approvals")
        return 0
    for task_id, platform in rows:
        snap = graph.get_state(_cfg(task_id))
        if snap.next:  # still suspended
            draft = snap.values["draft"]
            print(f"{task_id}  [{platform}]  next={snap.next[0]}")
            print(f"  draft: {draft['text'][:100]}")
        else:  # ghost index row: clean up
            db.execute("DELETE FROM pending_reviews WHERE task_id=?", (task_id,))
            db.commit()
    return 0


def cmd_show(args) -> int:
    graph = get_graph()
    snap = graph.get_state(_cfg(args.task_id))
    if not snap.values:
        print(f"unknown task {args.task_id}")
        return 1
    print(f"next:    {snap.next}")
    print(f"values:  {snap.values}")
    return 0


def _resume(task_id: str, payload: dict) -> int:
    graph = get_graph()
    snap = graph.get_state(_cfg(task_id))
    if not snap.next:
        print(f"task {task_id} is not suspended (next={snap.next or 'done'})")
        return 1
    graph.invoke(Command(resume=payload), _cfg(task_id))
    final = graph.get_state(_cfg(task_id)).values
    print(f"resumed {task_id} with {payload['decision']}")
    print(f"verification: {final.get('verification')}")
    print(f"failures:     {final.get('failures')}")
    return 0


def cmd_approve(args) -> int:
    return _resume(args.task_id, {"decision": "approved"})


def cmd_reject(args) -> int:
    return _resume(args.task_id, {"decision": "rejected"})


def cmd_edit(args) -> int:
    if not args.text:
        print("edit requires --text")
        return 1
    return _resume(args.task_id, {"decision": "edited", "text": args.text})


def cmd_approve_rule(args) -> int:
    from .distill import decide_proposal
    ok = decide_proposal(get_graph().store, args.key, approve=True)
    print("rule approved and now active" if ok else f"no pending proposal {args.key}")
    return 0 if ok else 1


def cmd_reject_rule(args) -> int:
    from .distill import decide_proposal
    ok = decide_proposal(get_graph().store, args.key, approve=False)
    print("rule rejected" if ok else f"no pending proposal {args.key}")
    return 0 if ok else 1


def cmd_retire_rule(args) -> int:
    from .distill import retire_rule
    ok = retire_rule(get_graph().store, args.key)
    print(f"rule {args.key} retired (kept as audit history)" if ok
          else f"no ACTIVE rule {args.key}")
    return 0 if ok else 1


def cmd_rules(args) -> int:
    from .distill import list_proposals, RULE_NS
    store = get_graph().store
    if args.all:
        for r in (store.search(RULE_NS) or []):
            v = r.value
            stats = (f"applied={v.get('applied_count', 0)} "
                     f"failed_while={v.get('failures_while_applied', 0)} "
                     if v.get("status") == "approved" else "")
            print(f"{r.key} [{v.get('status')}] {stats} {v['rule'][:60]}")
        return 0
    proposals = list_proposals(store)
    if not proposals:
        print("no pending rule proposals")
        return 0
    for p in proposals:
        print(f"{p.key}\n  RULE: {p.value['rule']}\n  WHY:  {p.value.get('rationale','')}\n")
    print("approve with: python -m agentposthub approve-rule <key>")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentposthub",
                                     description="AgentPostHub content distribution agent")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="start a publish task (suspends at approval)")
    p_run.add_argument("--platform", required=True,
                       choices=["telegram", "facebook", "reddit"])
    p_run.add_argument("--task-id", default=None,
                       help="stable id for idempotent reruns (default: random)")
    p_run.add_argument("--cities", type=int, default=3)
    p_run.add_argument("--days", type=int, default=10)
    p_run.add_argument("--budget", type=int, default=1500)
    p_run.add_argument("--notes", default=None,
                       help="free-text brief for long-form content "
                            "(overrides the short teaser format)")
    p_run.set_defaults(fn=cmd_run)

    sub.add_parser("list", help="list tasks waiting at the approval gate").set_defaults(fn=cmd_list)

    p_show = sub.add_parser("show", help="dump checkpointed state for a task")
    p_show.add_argument("task_id")
    p_show.set_defaults(fn=cmd_show)

    p_ap = sub.add_parser("approve", help="approve the suspended draft")
    p_ap.add_argument("task_id")
    p_ap.set_defaults(fn=cmd_approve)

    p_rj = sub.add_parser("reject", help="reject the suspended draft")
    p_rj.add_argument("task_id")
    p_rj.set_defaults(fn=cmd_reject)

    p_ed = sub.add_parser("edit", help="edit then approve the suspended draft")
    p_ed.add_argument("task_id")
    p_ed.add_argument("--text", default=None)
    p_ed.set_defaults(fn=cmd_edit)

    p_ru = sub.add_parser("rules", help="distilled rule proposals")
    p_ru.add_argument("--all", action="store_true",
                      help="show every rule incl. approved/rejected/retired")
    p_ru.set_defaults(fn=cmd_rules)
    p_ar = sub.add_parser("approve-rule", help="human gate: approve a distilled rule")
    p_ar.add_argument("key")
    p_ar.set_defaults(fn=cmd_approve_rule)
    p_rr = sub.add_parser("reject-rule", help="human gate: reject a distilled rule")
    p_rr.add_argument("key")
    p_rr.set_defaults(fn=cmd_reject_rule)
    p_rt = sub.add_parser("retire-rule", help="retire an active rule (root cause fixed)")
    p_rt.add_argument("key")
    p_rt.set_defaults(fn=cmd_retire_rule)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
