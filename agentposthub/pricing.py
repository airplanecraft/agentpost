"""Local call-time cost accounting: usage x price table.

Every provider response carries usage (prompt/completion tokens), so
cost is known the moment a call returns - no waiting for invoices.
Prices are approximate defaults, env-overridable; BOTH tokens and usd
are recorded so history can be re-rated when prices change.
"""
from __future__ import annotations

from .config import get_settings

# usd per 1M tokens (input, output) - approximate, override via env
PRICE_TABLE = {
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
}


def cost_from_usage(usage: dict | None, model: str | None = None) -> float:
    """usage {"prompt_tokens": n, "completion_tokens": m} -> usd."""
    if not usage:
        return 0.0
    s = get_settings()
    model = model or s.deepseek_model
    default = PRICE_TABLE.get(model, (0.27, 1.10))
    pin, pout = (s.price_in_per_m, s.price_out_per_m) if s.price_in_per_m \
        else default
    return round((usage.get("prompt_tokens", 0) * pin
                  + usage.get("completion_tokens", 0) * pout) / 1e6, 6)


def spent_usd(spend_entries: list) -> float:
    return round(sum(e.get("usd", 0) for e in spend_entries), 6)
