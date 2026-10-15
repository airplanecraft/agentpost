"""Tracing no-op semantics (keys absent -> nothing breaks) and the
deterministic draft scorers."""
from agentposthub import tracing
from agentposthub.evalharness import (check_number_consistency,
                                      check_placeholder_text,
                                      evaluate_draft, extract_usd_totals)


# ---- tracing: strict no-op when unconfigured ----

def test_tracing_disabled_by_default(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    from agentposthub.config import reset_settings_cache
    reset_settings_cache()
    assert tracing.tracing_enabled() is False
    assert tracing.observe_config("t1") == {}
    with tracing.span("x") as obs:
        assert obs is None
        worked = True
    assert worked
    tracing.score("t1", "m", 1.0)      # must not raise
    tracing.flush()


# ---- eval scorers ----

def test_extract_totals():
    text = "total $759, hotels $172 and maybe $1,500 budget"
    assert extract_usd_totals(text) == [759, 172, 1500]


def test_number_consistency_passes_on_exact_and_rounded():
    assert check_number_consistency("总计 $759", 759) == 1.0
    assert check_number_consistency("约 $760", 759) == 1.0   # tens rounding


def test_number_consistency_fails_on_invented_total():
    assert check_number_consistency("total $922!", 759) == 0.0


def test_number_consistency_half_when_no_planner():
    assert check_number_consistency("total $922", None) == 0.5


def test_placeholder_copy_rejected():
    assert check_placeholder_text("I'll create a detailed post") == 0.0
    assert check_placeholder_text("(tool-only response)") == 0.0
    assert check_placeholder_text("北京10天，总花费 $759") == 1.0


def test_evaluate_draft_shape():
    g = {"site_plan": {"total_usd": 759}}
    r = evaluate_draft("10天北京行程，总计 $759，明细如下", g)
    assert r["number_consistency"] == 1.0
    assert r["length_limit"] == 1.0
    assert r["real_copy"] == 1.0
    assert r["score"] == 1.0
