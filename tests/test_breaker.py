"""Cross-session tool-health memory: three failures open the circuit,
a fresh session (new store handle on the same db) still sees it open,
success after cooldown resets it."""
from agentposthub import breaker
from agentposthub.flow import get_store


def _fresh_store():
    """A NEW store instance on the same sqlite file = a new process /
    session, proving the memory is durable, not process-local."""
    get_store.cache_clear()
    return get_store()


def test_three_failures_open_the_circuit():
    store = _fresh_store()
    for i in range(breaker.THRESHOLD):
        st = breaker.breaker_record(store, "site_planner", ok=False,
                                    error=f"timeout_{i}")
    assert st["state"] == "open"
    assert st["failures"] == breaker.THRESHOLD


def test_open_circuit_visible_from_a_new_session():
    _fresh_store()  # accumulate failures in "session 1"
    store1 = get_store()
    for i in range(breaker.THRESHOLD):
        breaker.breaker_record(store1, "site_planner", ok=False, error="x")

    get_store.cache_clear()          # "session 2": new handle, same db
    store2 = get_store()
    open_, info = breaker.breaker_open(store2, "site_planner")
    assert open_ is True
    assert info["failures"] == breaker.THRESHOLD


def test_success_resets_the_circuit():
    store = _fresh_store()
    breaker.breaker_record(store, "site_planner", ok=False, error="x")
    st = breaker.breaker_record(store, "site_planner", ok=True)
    assert st["state"] == "closed" and st["failures"] == 0


def test_cooldown_expiry_allows_a_retry_that_reopens():
    store = _fresh_store()
    for i in range(breaker.THRESHOLD):
        breaker.breaker_record(store, "site_planner", ok=False, error="x")
    # force the open timestamp into the past
    ns, key = breaker.breaker_key("site_planner")
    rec = store.get(ns, key)
    stale = dict(rec.value, opened_at=0)
    store.put(ns, key, stale)
    open_, _ = breaker.breaker_open(store, "site_planner")
    assert open_ is False            # half-open: retry allowed
    st = breaker.breaker_record(store, "site_planner", ok=False,
                                error="still_down")
    assert st["state"] == "open"     # failed again -> reopens immediately


def test_below_threshold_keeps_circuit_closed():
    store = _fresh_store()
    breaker.breaker_record(store, "site_planner", ok=False, error="x")
    breaker.breaker_record(store, "site_planner", ok=False, error="x")
    open_, _ = breaker.breaker_open(store, "site_planner")
    assert open_ is False
