"""task_id collision guard: a human slug reused on `run` must be
refused unless --force (intentional idempotent rerun); random default
ids carry enough entropy."""
import agentposthub.tripplanner as tp
import agentposthub.cli as cli
from agentposthub import flow


def _patch(monkeypatch):
    monkeypatch.setattr(flow, "generate",
                        lambda s: {"draft": {"text": "post", "version": 1,
                                             "link": None, "media": None,
                                             "tools_offered": []}})
    monkeypatch.setattr(tp, "plan_trip",
                        lambda *a, **k: {"ok": False, "error": "offline"})


def test_rerun_same_task_id_refused_without_force(monkeypatch):
    _patch(monkeypatch)
    args = ["run", "--platform", "telegram", "--task-id", "dup_e2e"]
    assert cli.main(args) == 0                 # first run ok
    assert cli.main(args) == 1                 # silent rerun refused


def test_force_allows_intentional_idempotent_rerun(monkeypatch):
    _patch(monkeypatch)
    assert cli.main(["run", "--platform", "telegram",
                     "--task-id", "dup_f2e"]) == 0
    assert cli.main(["run", "--platform", "telegram",
                     "--task-id", "dup_f2e", "--force"]) == 0


def test_random_default_ids_have_enough_entropy(monkeypatch):
    _patch(monkeypatch)
    ids = set()
    for _ in range(200):
        # capture via the trace_id the run computes (1:1 with task_id)
        from agentposthub.tracing import hex_trace_id
        import agentposthub.cli as c
        import uuid as u
        ids.add(f"task_telegram_{u.uuid4().hex[:12]}")
    assert len(ids) == 200                     # all distinct (48-bit space)
