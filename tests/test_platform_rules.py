"""Platform-rule units: the prompt assembly honours the hard-rule table
(image visibility, link policy) and the output contract checker is
unchanged behaviour."""
from agentposthub import flow


def _ground():
    return {"facts": ["x"], "policy": "link in first comment"}


BRIEF = {"days": 3, "budget_usd": 300}


def test_reddit_prompt_has_no_image_tool_and_forbids_links():
    prompt, tools = flow._platform_prompt("reddit", BRIEF, _ground())
    assert tools == []                              # visibility gating
    assert "NO URLs" in prompt or "forbids outbound links" in prompt


def test_telegram_prompt_offers_image_tool_and_allows_links():
    prompt, tools = flow._platform_prompt("telegram", BRIEF, _ground())
    assert [t["function"]["name"] for t in tools] == ["create_image_content"]
    assert "forbids outbound links" not in prompt


def test_authoritative_planner_facts_reach_every_platform():
    ground = {**_ground(), "site_plan": {"total_usd": 759,
                                         "route": ["Beijing"],
                                         "plan_text": "stub"}}
    for p in ("telegram", "facebook", "reddit"):
        prompt, _ = flow._platform_prompt(p, BRIEF, ground)
        assert "AUTHORITATIVE" in prompt and "$759" in prompt


def test_lessons_line_reaches_prompt():
    ground = {**_ground(), "lessons": ["Always recheck the ledger"]}
    prompt, _ = flow._platform_prompt("telegram", BRIEF, ground)
    assert "Always recheck the ledger" in prompt
