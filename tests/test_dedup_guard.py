"""Dedup now runs inline inside the platform_draft worker; the semantic
layer stays a stub until pgvector. Watermark + stub contract."""
from agentposthub import flow


def test_semantic_similarity_stub_is_pass_through():
    # placeholder until pgvector: must never trigger a regen on its own
    assert flow._semantic_similarity("any text") < 0.92
