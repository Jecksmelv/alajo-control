"""Token pricing and cost calculation from measured usage.

Prices are OpenAI list prices (Standard tier, short context, USD per 1M tokens) from
https://developers.openai.com/api/docs/pricing, accessed 2026-10-23. Requests are
routed by the course Portkey gateway to an Azure OpenAI "global" deployment of the
same model; Azure's page did not display numeric rates, so the OpenAI list price is
used as the cost basis (assumption stated in the report).
"""

from __future__ import annotations

from typing import Any

PRICING_SOURCE = "https://developers.openai.com/api/docs/pricing"
PRICING_ACCESSED = "2026-10-23"

PRICING: dict[str, dict[str, float]] = {
    # input, cached input (cache read), cache write (30-minute), output
    "gpt-5.6-luna": {"input": 0.20, "cached_input": 0.02, "cache_write": 0.25, "output": 1.20},
    # gpt-6-luna cache-write rate was not captured; it is priced at the input rate (alternate model only).
    "gpt-6-luna": {"input": 0.10, "cached_input": 0.01, "cache_write": 0.10, "output": 0.50},
}


def price_for(model: str) -> dict[str, float]:
    return PRICING.get(model, PRICING["gpt-5.6-luna"])


def cost_usd(model: str, input_tokens: int, cached_tokens: int, output_tokens: int, cache_write_tokens: int = 0) -> float:
    p = price_for(model)
    uncached = max(0, input_tokens - cached_tokens - cache_write_tokens)
    return round((uncached * p["input"] + cached_tokens * p["cached_input"] + cache_write_tokens * p["cache_write"]
                  + output_tokens * p["output"]) / 1_000_000, 8)


def pricing_table() -> dict[str, Any]:
    return {"source": PRICING_SOURCE, "accessed": PRICING_ACCESSED, "unit": "USD per 1M tokens", "models": PRICING,
            "note": "Cache-write tokens are reported inside input tokens by the gateway and priced at the 30-minute "
                    "cache-write rate; cache-read tokens at the cached-input rate."}
