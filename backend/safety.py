"""Code-level safety controls.

These controls do not depend on any prompt being obeyed:
  * ActionPolicy: every action type is AUTO_ALLOWED, APPROVAL_REQUIRED or FORBIDDEN.
  * Untrusted text (customer messages, field-agent notes) is sanitized, length
    limited, scanned for embedded instructions and wrapped as data.
  * Sensitive fields (phone, BVN digits) are never placed in agent-visible data.
  * Customer scoping: a run handling one customer's conversation cannot read
    another customer's records.
  * Output guard: secrets and private prompt text are redacted from replies.
  * Numeric grounding: naira figures in an AI answer must appear in tool output.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Iterable

from . import config


class SafetyBlock(Exception):
    """Raised when a control blocks an action. The message is shown to the agent and logged."""

    def __init__(self, control: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.control = control
        self.details = details or {}


class Gate(str, Enum):
    AUTO_ALLOWED = "auto_allowed"
    APPROVAL_REQUIRED = "approval_required"
    FORBIDDEN = "forbidden"


# Action types the system knows about. Anything not listed is FORBIDDEN.
ACTION_POLICY: dict[str, tuple[Gate, str]] = {
    # low-risk administrative actions an agent may complete on its own
    "routine_overdue_reminder": (Gate.AUTO_ALLOWED, "Payment status verified as genuinely overdue"),
    "payment_acknowledgment": (Gate.AUTO_ALLOWED, "Confirms money already recorded by deterministic rules"),
    "request_transfer_reference": (Gate.AUTO_ALLOWED, "Asks for information; changes no records"),
    "request_documentation": (Gate.AUTO_ALLOWED, "Asks for a receipt or document; changes no records"),
    "policy_reply": (Gate.AUTO_ALLOWED, "General reply that contains no account data"),
    "internal_followup_task": (Gate.AUTO_ALLOWED, "Internal task for operations staff"),
    "field_agent_info_request": (Gate.AUTO_ALLOWED, "Asks a field agent for information"),
    # material financial or account-changing actions: human approval in code
    "match_payment_to_contribution": (Gate.APPROVAL_REQUIRED, "Changes a customer's recorded balance"),
    "clarify_ambiguous_payment": (Gate.APPROVAL_REQUIRED, "Ambiguous evidence: a human must decide who paid"),
    "mark_contribution_satisfied": (Gate.APPROVAL_REQUIRED, "Changes a contribution's financial status"),
    "adjust_customer_balance": (Gate.APPROVAL_REQUIRED, "Changes a customer's financial balance"),
    "resolve_agent_discrepancy": (Gate.APPROVAL_REQUIRED, "Field-agent cash discrepancy needs investigation"),
    "resolve_customer_dispute": (Gate.APPROVAL_REQUIRED, "Conflicting evidence about money received"),
    "quarantine_duplicate_record": (Gate.APPROVAL_REQUIRED, "Excludes a source record from the books"),
    "waive_charge": (Gate.APPROVAL_REQUIRED, "Financial concession"),
    "delay_or_cancel_payout": (Gate.APPROVAL_REQUIRED, "Affects money a customer is relying on"),
    "suspend_customer": (Gate.APPROVAL_REQUIRED, "Account-changing action"),
    "fraud_related_communication": (Gate.APPROVAL_REQUIRED, "Sensitive customer communication"),
    # never permitted through this system
    "move_real_money": (Gate.FORBIDDEN, "No real money movement is possible from this system"),
    "access_bank_account": (Gate.FORBIDDEN, "No live bank access"),
    "delete_evidence": (Gate.FORBIDDEN, "Historical evidence is append-only"),
    "delete_audit_log": (Gate.FORBIDDEN, "Audit history is append-only"),
    "disclose_other_customer_data": (Gate.FORBIDDEN, "Privacy"),
    "bypass_approval": (Gate.FORBIDDEN, "Approval controls cannot be bypassed"),
}

OUTBOX_ACTIONS = {"routine_overdue_reminder", "payment_acknowledgment", "request_transfer_reference",
                  "request_documentation", "policy_reply", "field_agent_info_request"}


def gate_for(action_type: str) -> Gate:
    return ACTION_POLICY.get(action_type, (Gate.FORBIDDEN, ""))[0]


def require_gate(action_type: str, expected: Gate) -> None:
    g = gate_for(action_type)
    if g != expected:
        raise SafetyBlock("action_policy", f"Action '{action_type}' is {g.value}; this path only allows {expected.value}.",
                          {"action_type": action_type, "gate": g.value})


# ---------------------------------------------------------------------------
# Untrusted content
# ---------------------------------------------------------------------------
INJECTION_PATTERNS = [
    r"ignore (all |any )?(the )?(previous|prior|above|earlier) (instructions|rules|prompts?)",
    r"disregard (all |any )?(the )?(previous|prior|above) (instructions|rules)",
    r"\bsystem (note|prompt|message|instruction)s?\b",
    r"\bnote to (the )?ai\b",
    r"\byou are now\b",
    r"\bact as (an? )?(admin|administrator|system)\b",
    r"\b(approve|close|resolve) (this|it|the \w+)? ?(automatically|without (any )?review|without approval)",
    r"\bwithout (any )?(review|approval)\b",
    r"\bapprove (this|it) automatically\b",
    r"\b(change|set|reset) (my|the|his|her) balance\b",
    r"\bmark (my|the|this) (account|contribution|batch)s? (as )?(paid|reconciled)\b",
    r"\breveal (your|the) (system )?(prompt|instructions|api key)\b",
]
_INJ = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]
MAX_UNTRUSTED_CHARS = 600


def detect_injection(text: str | None) -> list[str]:
    if not text:
        return []
    return [p.pattern for p in _INJ if p.search(text)]


def sanitize_untrusted(text: str | None, source: str) -> dict[str, Any]:
    """Return customer/agent text as labelled data. Instruction-like segments are
    replaced so they never reach a model as an imperative."""
    if not text:
        return {"source": source, "trust": "untrusted", "text": "", "instruction_like_content_removed": False}
    t = text.replace("<", "(").replace(">", ")")[:MAX_UNTRUSTED_CHARS]
    hits = detect_injection(t)
    if hits:
        # Keep only sentences that contain no instruction-like pattern.
        sentences = re.split(r"(?<=[.!?])\s+", t)
        kept = [s for s in sentences if not detect_injection(s)]
        t = " ".join(kept).strip()
        t = (t + " " if t else "") + "[REDACTED: instruction-like text addressed to the system was removed by the safety filter]"
    return {"source": source, "trust": "untrusted", "text": t, "instruction_like_content_removed": bool(hits)}


# ---------------------------------------------------------------------------
# Sensitive data
# ---------------------------------------------------------------------------
SENSITIVE_CUSTOMER_FIELDS = {"phone", "bvn_last4"}


def strip_sensitive(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k not in SENSITIVE_CUSTOMER_FIELDS}


def mask_phone(phone: str) -> str:
    return phone[:4] + "*****" + phone[-2:] if phone and len(phone) > 6 else "***"


def check_customer_scope(scope_customer_id: str | None, requested_customer_id: str | None) -> None:
    if scope_customer_id and requested_customer_id and requested_customer_id != scope_customer_id:
        raise SafetyBlock("privacy_scope",
                          "This run is scoped to one customer's conversation; other customers' records are not accessible.",
                          {"scope": scope_customer_id, "requested": requested_customer_id})


# ---------------------------------------------------------------------------
# Output guard
# ---------------------------------------------------------------------------
_SECRET_PATTERNS = [re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), re.compile(r"pk-[A-Za-z0-9_\-]{16,}")]


def redact_output(text: str, prompt_fragments: Iterable[str] = ()) -> tuple[str, list[str]]:
    """Remove secrets and verbatim private prompt text from model output."""
    findings: list[str] = []
    key = config.portkey_key()
    if key and key in text:
        text = text.replace(key, "[REDACTED SECRET]")
        findings.append("api_key")
    for p in _SECRET_PATTERNS:
        if p.search(text):
            text = p.sub("[REDACTED SECRET]", text)
            findings.append("secret_pattern")
    for frag in prompt_fragments:
        if frag and len(frag) > 40 and frag in text:
            text = text.replace(frag, "[REDACTED: internal instructions]")
            findings.append("system_prompt_fragment")
    return text, findings


# ---------------------------------------------------------------------------
# Numeric grounding: every naira amount in an answer must come from a tool
# ---------------------------------------------------------------------------
_NAIRA = re.compile(r"(?:₦|NGN\s?|N(?=\d))\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(\s?(?:k|m|million|thousand)\b)?", re.IGNORECASE)


def _collect_numbers(obj: Any, out: set[int]) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        out.add(int(round(obj)))
        out.add(abs(int(round(obj))))
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_numbers(v, out)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            _collect_numbers(v, out)
    elif isinstance(obj, str):
        for m in re.finditer(r"\d{1,3}(?:,\d{3})+|\d+", obj):
            out.add(int(m.group(0).replace(",", "")))


def grounding_check(answer: str, tool_outputs: list[Any]) -> dict[str, Any]:
    allowed: set[int] = set()
    for o in tool_outputs:
        _collect_numbers(o, allowed)
    found, ungrounded = [], []
    for m in _NAIRA.finditer(answer or ""):
        raw = m.group(1).replace(",", "")
        val = int(raw)
        suffix = (m.group(2) or "").strip().lower()
        if suffix in {"k", "thousand"}:
            val *= 1_000
        elif suffix in {"m", "million"}:
            # "₦1.2m" style: the decimal part was dropped by the regex; compare by magnitude window
            val *= 1_000_000
        found.append(val)
        if val in allowed:
            continue
        if suffix in {"m", "million"} and any(abs(a - val) < 1_000_000 for a in allowed if a >= 1_000_000):
            continue
        ungrounded.append(m.group(0))
    return {"amounts_found": len(found), "ungrounded": ungrounded, "grounded": not ungrounded}
