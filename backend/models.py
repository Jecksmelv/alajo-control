"""Structured models shared by agents, tools and the API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Specialist outputs (validated by PydanticAI; invalid output is retried)
# ---------------------------------------------------------------------------


class ReconciliationDecision(BaseModel):
    """Money Match judgment on one ambiguous inflow. A recommendation only: it never mutates the ledger."""

    case_id: str
    decision: Literal["recommend_match", "needs_clarification", "no_match"]
    customer_id: str | None = Field(None, description="Recommended customer, only when decision=recommend_match")
    expected_contribution_id: str | None = None
    candidate_transaction_id: str
    evidence_used: list[str] = Field(default_factory=list, description="Short factual bullets citing record ids")
    conflicts: list[str] = Field(default_factory=list, description="Evidence against, or 'none found'")
    confidence: float = Field(ge=0, le=1)
    reasoning_summary: str = Field(max_length=600, description="Concise decision summary, no private reasoning")
    recommended_action: str = Field(max_length=300)


class TriageItem(BaseModel):
    case_id: str
    why_it_matters: str = Field(max_length=300)
    next_step: str = Field(max_length=200)


class TriageReport(BaseModel):
    summary: str = Field(max_length=800)
    top_items: list[TriageItem] = Field(max_length=8)


class LiquidityBrief(BaseModel):
    headline: str = Field(max_length=200)
    explanation: str = Field(max_length=1200, description="Uses only figures returned by the liquidity tools")
    key_case_ids: list[str] = Field(default_factory=list, max_length=6)
    recommended_actions: list[str] = Field(default_factory=list, max_length=5)
    figures_verified: bool = Field(description="False if a required tool failed and figures could not be verified")


class ActionPlanResult(BaseModel):
    summary: str = Field(max_length=800)
    outbox_message_ids: list[str] = Field(default_factory=list)
    task_ids: list[str] = Field(default_factory=list)
    approval_ids: list[str] = Field(default_factory=list)
    blocked_actions: list[str] = Field(default_factory=list)


class SupervisorAnswer(BaseModel):
    answer: str = Field(description="Markdown answer for the operations manager. Cite case ids like RC-1007.")
    referenced_case_ids: list[str] = Field(default_factory=list, max_length=12)
    referenced_customer_ids: list[str] = Field(default_factory=list, max_length=12)
    specialists_consulted: list[str] = Field(default_factory=list)
    figures_verified: bool = True


# ---------------------------------------------------------------------------
# API payloads
# ---------------------------------------------------------------------------


class AskRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str | None = Field(None, max_length=64)
    language: Literal["en", "pcm"] | None = None


class ApprovalDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    note: str | None = Field(None, max_length=500)
    decided_by: str = Field("ops.manager", max_length=60)


class ReminderRequest(BaseModel):
    style: Literal["en", "pcm", "auto"] = "auto"
