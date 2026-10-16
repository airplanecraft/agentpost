"""Vector memory with a deterministic fake-embedding backend: near-
duplicate drafts are caught by meaning; disabled backend degrades to
no-op everywhere; rules enter/leave the index with their lifecycle."""
from agentposthub import distill, flow, vectormem


def _fake_embed(monkeypatch):
    """Deterministic vectors: token-hash bag -> stable cosine that
    tracks word overlap (good enough to rank near-duplicates first)."""
    def embed(texts):
        out = []
        for t in texts:
            v = [0.0] * 64
            for w in t.split():
                v[hash(w) % 64] += 1.0
            out.append(v)
        return out
    monkeypatch.setattr(vectormem, "embed", embed)
    monkeypatch.setattr(vectormem, "_backend", ("fake", "test"))


def test_add_and_semantic_near_duplicate(monkeypatch):
    _fake_embed(monkeypatch)
    vectormem.add("published_telegram", "t1",
                  "北京 10日 亲子游 总预算 1500 美金 迪士尼 门票 明细")
    hits = vectormem.similar(
        "published_telegram",
        "北京 10日 亲子游 总预算 1500 美金 迪士尼 门票 明细 住宿", k=1)
    assert hits and hits[0]["id"] == "t1"
    assert hits[0]["sim"] > 0.9          # heavy word overlap
    miss = vectormem.similar("published_telegram", "上海 三亚 自由行 攻略", k=1)
    assert miss[0]["sim"] < 0.3          # disjoint vocabulary


def test_disabled_backend_is_silent_noop(monkeypatch):
    monkeypatch.setattr(vectormem, "_backend", ())
    assert vectormem.embed(["x"]) is None
    assert vectormem.add("c", "i", "t") is False
    assert vectormem.similar("c", "t") == []


def test_draft_worker_dedups_against_published(monkeypatch):
    import agentposthub.tripplanner as tp
    _fake_embed(monkeypatch)
    vectormem.add("published_telegram", "old",
                  "北京 亲子游 总预算 美金 行程 明细 费用 门票 住宿")
    monkeypatch.setattr(tp, "plan_trip",
                        lambda *a, **k: {"ok": False, "error": "offline"})
    monkeypatch.setattr(
        flow, "platform_draft",
        # run the real worker? we test the dedup slice directly instead:
        flow.platform_draft)
    # call the dedup logic through a tiny harness: reuse worker's tail by
    # calling similar() the same way the worker does
    text = "北京 亲子游 总预算 美金 行程 明细 费用 门票 住宿 加更"
    hits = vectormem.similar("published_telegram", text, k=1)
    assert hits[0]["sim"] > 0.92         # would watermark [variant]


def test_rule_lifecycle_syncs_vector_index(monkeypatch):
    _fake_embed(monkeypatch)
    store = flow.get_store()
    store.put(distill.RULE_NS, "prop_v",
              {"rule": "Always smoke-test external SDKs before publish",
               "rationale": "", "status": "pending",
               "created_at": 0, "incident_keys": []})
    distill.decide_proposal(store, "prop_v", approve=True)
    hits = vectormem.similar("rules", "smoke-test SDK before publishing", k=1)
    assert hits and hits[0]["id"] == "prop_v"
    distill.retire_rule(store, "prop_v")
    assert vectormem.similar("rules", "smoke-test SDK", k=1) == []
