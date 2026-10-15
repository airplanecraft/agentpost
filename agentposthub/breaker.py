"""Cross-session tool-health memory: a circuit breaker in the Store.

Tool failures live in per-task state by design (the failures ledger is a
task audit trail). But "the site planner has failed N times in a row"
is knowledge every future task needs — that belongs in long-term
memory, shared across threads, processes and graphs via the Store
namespace ("tool", <name>).

Behaviour (matches the design doc failure table: bounded failure ->
cooldown -> retry):
  closed  -- failures accumulate; at THRESHOLD the circuit opens
  open    -- calls are skipped without hitting the tool, until the
             cooldown elapses (half-open: one real call is allowed)
  success -- resets to closed with zero failures
"""
from __future__ import annotations

import time

THRESHOLD = 3
COOLDOWN_S = 3600.0


def breaker_key(tool: str) -> tuple:
    return ("tool", tool), "breaker"


def breaker_open(store, tool: str) -> tuple[bool, dict | None]:
    """True while the circuit is open and inside the cooldown window.
    Past the cooldown it returns False (half-open): the caller tries the
    real call, and breaker_record decides whether it stays open."""
    if store is None:
        return False, None
    ns, key = breaker_key(tool)
    rec = store.get(ns, key)
    if not rec:
        return False, None
    state = rec.value
    if state.get("state") == "open":
        if time.time() - state.get("opened_at", 0) < COOLDOWN_S:
            return True, state
    return False, state


def breaker_record(store, tool: str, ok: bool,
                   error: str | None = None) -> dict:
    """Record a call outcome; returns the new breaker state."""
    ns, key = breaker_key(tool)
    if store is None:
        return {}
    rec = store.get(ns, key)
    state = rec.value if rec else {"state": "closed", "failures": 0,
                                   "opened_at": 0.0, "last_error": None}
    if ok:
        state = {"state": "closed", "failures": 0, "opened_at": 0.0,
                 "last_error": None}
    else:
        state["failures"] = state.get("failures", 0) + 1
        state["last_error"] = error
        if state["failures"] >= THRESHOLD:
            state["state"] = "open"
            state["opened_at"] = time.time()
    store.put(ns, key, state)
    return state
