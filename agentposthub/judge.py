"""LLM-as-judge: quality scoring over drafts, against a narrow rubric.

Deliberately OUT of control flow: judge results are signals (scores,
advisory text for the human gate) - never routing decisions. A judge
failure returns None and the caller carries on; nothing in the graph
ever waits on or branches by a judge verdict.

Used from:
  - the product eval harness (per-draft judge_quality)
  - the approve gate (advisory aggregate shown to the human approver)
Both are opt-in: AGENT_JUDGE=1 in .env.
"""
from __future__ import annotations

import json
import time
import urllib.request as rq

from .config import get_settings, ssl_ctx

RUBRIC = """你是内容质量评审。按以下三个维度给这条{platform}帖子综合打分(1-5):
1) 需求相关性:是否紧扣需求(预算/天数/同行人)
2) 信息可操作性:费用/行程是否具体到能照着做
3) 平台契合度:文体、长度、格式是否适合该平台
只输出一行 JSON,不要其他文字: {{"score": <1-5的整数>, "reasons": "<30字内中文理由>"}}


需求: {notes}
帖子:
{draft}"""


def judge_text(draft_text: str, platform: str, notes: str) -> dict | None:
    """Score one draft. Returns {"score": int, "reasons": str} or None
    on any failure (network, parse, refusal). Never raises."""
    s = get_settings()
    if not s.deepseek_api_key:
        return None
    body = {"model": s.deepseek_model,
            "messages": [{"role": "user",
                          "content": RUBRIC.format(platform=platform,
                                                   notes=notes[:200],
                                                   draft=draft_text[:3500])}],
            "max_tokens": 120, "temperature": 0}
    req = rq.Request("https://api.deepseek.com/chat/completions",
                     data=json.dumps(body).encode(),
                     headers={"Authorization": f"Bearer {s.deepseek_api_key}",
                              "Content-Type": "application/json"})
    for attempt in range(1, 3):
        try:
            with rq.urlopen(req, timeout=30, context=ssl_ctx()) as resp:
                raw = json.loads(resp.read())["choices"][0]["message"]["content"]
            raw = raw.strip()
            start, end = raw.find("{"), raw.rfind("}")
            data = json.loads(raw[start:end + 1])
            score = int(data["score"])
            if not 1 <= score <= 5:
                return None
            return {"score": score, "reasons": str(data.get("reasons", ""))[:60]}
        except Exception:
            if attempt == 2:
                return None
            time.sleep(1.5)
    return None


def judge_drafts_aggregate(drafts: list[dict], notes: str) -> dict | None:
    """One advisory verdict over all platform drafts for the human gate:
    average score + the weakest platform called out."""
    verdicts = []
    for d in drafts:
        v = judge_text(d.get("text", ""), d.get("platform", "?"), notes)
        if v:
            verdicts.append({"platform": d.get("platform"), **v})
    if not verdicts:
        return None
    avg = round(sum(v["score"] for v in verdicts) / len(verdicts), 1)
    weakest = min(verdicts, key=lambda v: v["score"])
    return {"avg_score": avg, "verdicts": verdicts,
            "weakest": f"{weakest['platform']}({weakest['score']}): "
                       f"{weakest['reasons']}"}
