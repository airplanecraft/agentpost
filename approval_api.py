"""
Approval API bridge (FastAPI) — optional HTTP surface for the approval
gate. The CLI (python -m agentposthub) covers the same operations; this
module exists for external integrations.

GET  /api/approvals                 list pending items (suspended snapshots)
POST /api/approvals/{id}/decision   human decision -> resume the graph

Run: uvicorn approval_api:app --port 8899
Deps: pip install fastapi uvicorn
"""
from fastapi import FastAPI
from langgraph.types import Command
from pydantic import BaseModel

from agentposthub.flow import get_db, get_graph

db = get_db()  # pending_reviews table is created lazily on first access
graph = get_graph()

app = FastAPI()


@app.get("/api/approvals")
def list_approvals():
    """Pending list: query the index table, then open each suspended
    snapshot via get_state to read the full draft. Ghost rows (already
    resumed but not cleaned) are self-healed here."""
    rows = db.execute("SELECT task_id, platform FROM pending_reviews").fetchall()
    items = []
    for task_id, platform in rows:
        snap = graph.get_state({"configurable": {"thread_id": task_id}})
        if snap.next:  # still suspended
            items.append({
                "task_id": task_id,
                "platform": platform,
                "draft": snap.values["draft"],
                "regen_count": snap.values.get("regen_count", 0),
            })
        else:  # ghost index row: clean up
            db.execute("DELETE FROM pending_reviews WHERE task_id=?", (task_id,))
            db.commit()
    return {"items": items}


class Decision(BaseModel):
    decision: str            # approved | rejected | edited
    text: str | None = None  # new draft text, only for edited


@app.post("/api/approvals/{task_id}/decision")
def decide(task_id: str, body: Decision):
    """The click becomes a resume: the payload rides Command(resume=...)
    into the interrupted approve node and the graph continues to
    completion (publish -> verify -> record). The index row is deleted
    here as well - idempotent, defence in depth alongside the delete
    inside the node."""
    payload = {"decision": body.decision}
    if body.decision == "edited" and body.text:
        payload["text"] = body.text
    graph.invoke(Command(resume=payload),
                 {"configurable": {"thread_id": task_id}})
    db.execute("DELETE FROM pending_reviews WHERE task_id=?", (task_id,))
    db.commit()
    return {"ok": True, "task_id": task_id, "resumed_with": payload}


# Frontend contract (existing Next.js approval page):
#   open page:    fetch('/api/approvals').items -> render cards
#   approve:      POST /api/approvals/{task_id}/decision {decision:'approved'}
#   reject:       same with decision:'rejected'
#   edit+approve: decision:'edited', text:'new draft'
#   after click:  refresh list (card disappears, graph continues in background)
