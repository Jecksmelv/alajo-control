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
    # input, cached input (cache read), cache write, output
    # gpt-6-luna is the course model. Its cache-write rate was not captured from the pricing page, so
    # cache-write tokens are priced at the normal input rate (stated as an assumption in the report).
    "gpt-6-luna": {"input": 0.10, "cached_input": 0.01, "cache_write": 0.10, "output": 0.50},
}


def price_for(model: str) -> dict[str, float]:
    return PRICING.get(model, PRICING["gpt-6-luna"])


def cost_usd(model: str, input_tokens: int, cached_tokens: int, output_tokens: int, cache_write_tokens: int = 0) -> float:
    p = price_for(model)
    uncached = max(0, input_tokens - cached_tokens - cache_write_tokens)
    return round((uncached * p["input"] + cached_tokens * p["cached_input"] + cache_write_tokens * p["cache_write"]
                  + output_tokens * p["output"]) / 1_000_000, 8)


def pricing_table() -> dict[str, Any]:
    return {"source": PRICING_SOURCE, "accessed": PRICING_ACCESSED, "unit": "USD per 1M tokens", "models": PRICING,
            "note": "Standard tier, short-context rates. Cache-read tokens are billed at the cached-input rate; cache-write "
                    "tokens (reported inside input tokens by the gateway) are billed at the listed cache-write rate, which "
                    "for gpt-6-luna was not captured and is assumed equal to the input rate."}
