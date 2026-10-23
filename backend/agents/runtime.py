"""Shared agent runtime: model wiring, run context, traced tools, usage capture, limits."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

from openai import AsyncOpenAI  # noqa: E402
from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior, UsageLimitExceeded  # noqa: E402
from pydantic_ai.models.openai import OpenAIResponsesModel, OpenAIResponsesModelSettings  # noqa: E402
from pydantic_ai.providers.openai import OpenAIProvider  # noqa: E402
from pydantic_ai.usage import UsageLimits  # noqa: E402

from .. import audit, config  # noqa: E402
from ..database import db  # noqa: E402
from ..safety import SafetyBlock  # noqa: E402
from ..usage import cost_usd  # noqa: E402


class AIUnavailable(RuntimeError):
    pass


@lru_cache(maxsize=4)
def get_model(model_name: str | None = None) -> OpenAIResponsesModel:
    key = config.portkey_key()
    if not key:
        raise AIUnavailable("PORTKEY_API_KEY is not set. Put it in alajo-control/.env or the parent folder's .env.")
    client = AsyncOpenAI(
        api_key="not-used-directly",          # Portkey authenticates with the header below
        base_url=config.PORTKEY_BASE_URL,
        default_headers={
            "x-portkey-api-key": key,
            "x-portkey-provider": config.PORTKEY_PROVIDER,
            # Bypass Portkey's response replay cache so every measured call is a real model call.
            # (Provider-side prompt caching of identical prefixes still applies and is measured.)
            "x-portkey-cache-force-refresh": "true",
        },
        max_retries=2,
        timeout=120,
    )
    return OpenAIResponsesModel(model_name or config.MODEL_NAME, provider=OpenAIProvider(openai_client=client))


def load_prompt(name: str) -> str:
    return (config.PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def compact(obj: Any, limit: int = 600) -> Any:
    s = json.dumps(obj, default=str, ensure_ascii=False)
    if len(s) <= limit:
        return obj
    return s[:limit] + "…"


@dataclass
class Trace:
    run_id: str
    t0: float = field(default_factory=time.perf_counter)
    steps: list[dict[str, Any]] = field(default_factory=list)

    def add(self, kind: str, agent: str, **kw: Any) -> None:
        self.steps.append({"t_ms": int((time.perf_counter() - self.t0) * 1000), "kind": kind, "agent": agent, **kw})


@dataclass
class Totals:
    model_requests: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    delegations: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


@dataclass
class Deps:
    run_id: str
    trace: Trace
    totals: Totals
    mode: str = "optimized"                # optimized | naive | weak
    agent_name: str = "supervisor"
    scope_customer_id: str | None = None   # privacy scope for customer-conversation runs
    fault_tools: set[str] = field(default_factory=set)  # fault injection for the Safety Lab
    tool_outputs: list[Any] = field(default_factory=list)
    allow_outreach: bool = True
    language_style: str = "auto"
    sandbox: dict[str, Any] = field(default_factory=dict)  # weak-config test sandbox (never the real DB)

    def child(self, agent_name: str) -> "Deps":
        return Deps(run_id=self.run_id, trace=self.trace, totals=self.totals, mode=self.mode, agent_name=agent_name,
                    scope_customer_id=self.scope_customer_id, fault_tools=self.fault_tools,
                    tool_outputs=self.tool_outputs, allow_outreach=self.allow_outreach,
                    language_style=self.language_style, sandbox=self.sandbox)


def run_tool(deps: Deps, name: str, args: dict[str, Any], fn: Callable[[Any], Any], *, write: bool = False) -> Any:
    """Execute a deterministic tool against SQLite with tracing, auditing, fault injection and safety handling."""
    t0 = time.perf_counter()
    deps.totals.tool_calls += 1
    if deps.totals.tool_calls > config.MAX_TOOL_CALLS_PER_AGENT_RUN * 4:
        result = {"error": "TOOL_BUDGET_EXHAUSTED", "message": "Tool-call budget for this request is exhausted. Stop and answer."}
        deps.trace.add("tool_call", deps.agent_name, tool=name, args=compact(args, 300), status="limit", result=result)
        return result
    if name in deps.fault_tools:
        result = {"error": "TOOL_UNAVAILABLE", "tool": name,
                  "message": f"{name} is unavailable. The figures it provides cannot be verified right now."}
        deps.trace.add("tool_call", deps.agent_name, tool=name, args=compact(args, 300), status="error", result=result,
                       ms=int((time.perf_counter() - t0) * 1000))
        audit.log("tool_call", f"agent:{deps.agent_name}", {"tool": name, "args": args, "status": "fault_injected"},
                  run_id=deps.run_id)
        deps.tool_outputs.append(result)
        return result
    try:
        with db() as conn:
            result = fn(conn)
        status = "ok"
    except SafetyBlock as e:
        result = {"blocked": True, "control": e.control, "message": str(e)}
        status = "blocked"
        audit.log("safety_block", f"agent:{deps.agent_name}", {"tool": name, "args": args, "control": e.control,
                                                               "message": str(e)}, run_id=deps.run_id)
        deps.trace.add("safety_block", deps.agent_name, tool=name, control=e.control, message=str(e))
    except ValueError as e:
        result = {"error": "INVALID_ARGUMENT", "message": str(e)}
        status = "error"
    ms = int((time.perf_counter() - t0) * 1000)
    deps.trace.add("tool_call", deps.agent_name, tool=name, args=compact(args, 300), status=status,
                   result=compact(result, 700), ms=ms)
    if status != "blocked":
        audit.log("tool_call", f"agent:{deps.agent_name}", {"tool": name, "args": args, "status": status, "ms": ms,
                                                            "write": write}, run_id=deps.run_id)
    deps.tool_outputs.append(result)
    return result


def model_settings(agent_name: str, mode: str) -> OpenAIResponsesModelSettings:
    # Same reasoning setting in every mode so the naive-vs-optimized benchmark isolates context design.
    s: dict[str, Any] = {"max_tokens": config.MAX_OUTPUT_TOKENS, "parallel_tool_calls": False,
                         "openai_reasoning_effort": "low"}
    if mode == "optimized":
        s["openai_prompt_cache_key"] = f"alajo-{agent_name}-v1"
    return OpenAIResponsesModelSettings(**s)


async def run_agent(agent: Agent, agent_name: str, prompt: str, deps: Deps, *, request_limit: int | None = None,
                    label: str | None = None) -> tuple[Any, dict[str, Any]]:
    """Run one specialist loop with limits; record usage + trace. Returns (output or None, info)."""
    remaining = config.MAX_MODEL_REQUESTS_PER_QUERY - deps.totals.model_requests
    if remaining <= 0:
        deps.trace.add("limit", agent_name, message="Model-request budget for this query exhausted")
        return None, {"stop_reason": "request_budget_exhausted"}
    limit = min(request_limit or config.MAX_REQUESTS_PER_AGENT_RUN, remaining)
    deps.trace.add("agent_start", agent_name, prompt=compact(prompt, 400))
    t0 = time.perf_counter()
    stop_reason = "completed"
    output = None
    usage = None
    try:
        result = await agent.run(prompt, deps=deps, model=get_model(),
                                 usage_limits=UsageLimits(request_limit=limit,
                                                          tool_calls_limit=config.MAX_TOOL_CALLS_PER_AGENT_RUN),
                                 model_settings=model_settings(agent_name, deps.mode))
        output = result.output
        usage = result.usage
    except UsageLimitExceeded as e:
        stop_reason = f"usage_limit: {e}"
        usage = getattr(e, "usage", None)
    except ModelHTTPError as e:
        body = str(getattr(e, "body", "") or e)[:300]
        stop_reason = "provider_content_filter" if "content" in body.lower() and "filter" in body.lower() else f"model_http_error {e.status_code}"
        deps.trace.add("error", agent_name, message=body)
    except UnexpectedModelBehavior as e:
        stop_reason = f"unexpected_model_behavior: {str(e)[:200]}"
    latency = int((time.perf_counter() - t0) * 1000)
    req = inp = cached = cwrite = outp = 0
    if usage is not None:
        req, inp, outp = usage.requests, usage.input_tokens, usage.output_tokens
        cached, cwrite = usage.cache_read_tokens, usage.cache_write_tokens
    c = cost_usd(config.MODEL_NAME, inp, cached, outp, cwrite)
    t = deps.totals
    t.model_requests += req
    t.input_tokens += inp
    t.cached_input_tokens += cached
    t.cache_write_tokens += cwrite
    t.output_tokens += outp
    t.cost_usd += c
    audit.record_usage(run_id=deps.run_id, agent=agent_name, mode=deps.mode, model=config.MODEL_NAME, model_requests=req,
                       input_tokens=inp, cached_input_tokens=cached, output_tokens=outp, latency_ms=latency, cost_usd=c,
                       label=label)
    info = {"stop_reason": stop_reason, "latency_ms": latency, "model_requests": req, "input_tokens": inp,
            "cached_input_tokens": cached, "output_tokens": outp, "cost_usd": c}
    deps.trace.add("agent_end", agent_name, **info,
                   output=compact(output.model_dump() if hasattr(output, "model_dump") else output, 900))
    return output, info
