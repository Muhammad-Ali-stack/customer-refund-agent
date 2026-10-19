# Copyright 2026 Google LLC
# Modified 2026 by Muhammad Ali (refund operations agent).
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Customer refund operations agent.

Refund requests arrive as chat messages (ADK web UI) or as Pub/Sub
trigger events and flow through an ADK 2.0 graph-based workflow:

- Customer lookup, order lookup and refund eligibility are deterministic
  Python (see ``refunds.py``). The LLM never decides eligibility.
- Ineligible or invalid requests are closed immediately — they never
  reach the approval step, so an approver cannot bypass the rules.
- Eligible requests are summarised by an LLM review agent, then paused
  for human approval via ADK 2.0's RequestInput (HITL).
- On approval the rules are re-checked and a refund is *simulated*.
  No payment API is ever called.
"""

import base64
import binascii
import json
import re
from datetime import date
from typing import Any

from google.adk import Agent, Context, Event, Workflow
from google.adk.agents.callback_context import CallbackContext
from google.adk.events import RequestInput
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import BaseModel, Field

from . import refunds
from .config import config

# ---------------------------------------------------------------------------
# Pydantic schema for the LLM review agent's input
# ---------------------------------------------------------------------------


class RefundReview(BaseModel):
    """An eligible refund request awaiting human approval."""

    request_id: str = Field(description="Internal refund request ID")
    customer_id: str = Field(description="Customer ID, e.g. CUST-1001")
    customer_name: str = Field(description="Customer full name")
    order_id: str = Field(description="Order ID, e.g. ORD-5001")
    items: str = Field(description="Items in the order")
    refund_amount: float = Field(description="Refund amount in USD")
    reason: str = Field(description="Customer's stated refund reason")
    eligibility_explanation: str = Field(
        description="Deterministic eligibility result and explanation"
    )


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------

_CUSTOMER_RE = re.compile(r"\bCUST-\d+\b", re.IGNORECASE)
_ORDER_RE = re.compile(r"\bORD-\d+\b", re.IGNORECASE)
_REASON_RE = re.compile(
    r"\b(?:because|due to|reason\s*:)\s*(.+)$", re.IGNORECASE | re.DOTALL
)


def _content_to_text(node_input: Any) -> Any:
    """Return plain text for Content input; pass other values through."""
    if isinstance(node_input, types.Content):
        return "".join(p.text or "" for p in node_input.parts or [])
    return node_input


def _parse_text_request(text: str) -> dict:
    """Extract IDs and reason from a free-text request.

    Example: "Check whether customer CUST-1001 can receive a refund for
    order ORD-5001 because the item arrived damaged."
    """
    customer = _CUSTOMER_RE.search(text)
    order = _ORDER_RE.search(text)
    reason_match = _REASON_RE.search(text)
    reason = reason_match.group(1) if reason_match else ""
    return {
        "customer_id": customer.group(0).upper() if customer else "",
        "order_id": order.group(0).upper() if order else "",
        "reason": reason.strip().rstrip(".").strip(),
    }


def parse_refund_request(node_input: Any) -> Event:
    """Normalise any supported input into ``{customer_id, order_id, reason}``.

    Supported inputs:
    - Chat text: "... customer CUST-1001 ... order ORD-5001 because ..."
    - JSON: ``{"customer_id": ..., "order_id": ..., "reason": ...}``
    - Pub/Sub trigger: ``{"data": <JSON object, base64 JSON, or text>}``
    """
    raw = _content_to_text(node_input)
    payload: Any = raw

    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return Event(output={"error": "Empty request."})
        if stripped.startswith("{"):
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                return Event(output={"error": "Malformed JSON request."})
        else:
            return Event(output=_parse_text_request(stripped))

    if isinstance(payload, dict) and "data" in payload:
        payload = payload["data"]
        if isinstance(payload, str):
            try:
                payload = json.loads(base64.b64decode(payload, validate=True))
            except (binascii.Error, ValueError):
                return Event(output=_parse_text_request(payload))

    if not isinstance(payload, dict):
        return Event(output={"error": "Unsupported request format."})

    return Event(
        output={
            "customer_id": str(payload.get("customer_id") or ""),
            "order_id": str(payload.get("order_id") or ""),
            "reason": str(payload.get("reason") or ""),
        }
    )


# ---------------------------------------------------------------------------
# Deterministic eligibility + routing
# ---------------------------------------------------------------------------


def evaluate_refund(node_input: dict, ctx: Context) -> Event:
    """Look up the customer and order, check eligibility, and route.

    ``ELIGIBLE`` requests reserve the order in the refund ledger (which
    blocks duplicate requests) and go to review + human approval.
    Everything else goes to ``NOT_ELIGIBLE`` and is closed without any
    possibility of approval.
    """
    if not isinstance(node_input, dict) or node_input.get("error"):
        error = (
            node_input.get("error")
            if isinstance(node_input, dict)
            else "Unsupported request format."
        )
        refunds.record_audit("invalid_request", severity="WARNING", error=error)
        return Event(
            route="NOT_ELIGIBLE",
            output={"status": "invalid", "explanation": error},
        )

    missing = [k for k in ("customer_id", "order_id") if not node_input.get(k)]
    if missing:
        error = (
            f"Missing {', '.join(missing)}. Example: 'Refund order ORD-5001"
            " for customer CUST-1001 because the item arrived damaged.'"
        )
        refunds.record_audit("invalid_request", severity="WARNING", error=error)
        return Event(
            route="NOT_ELIGIBLE",
            output={"status": "invalid", "explanation": error},
        )

    result = refunds.check_refund_eligibility(
        node_input["customer_id"], node_input["order_id"], node_input["reason"]
    )

    if result["eligible"]:
        request_id = refunds.new_request_id()
        if not refunds.reserve_refund(
            result["order_id"], request_id, result["refund_amount"]
        ):
            # Lost a race with a concurrent request for the same order.
            result.update(
                status="ineligible",
                eligible=False,
                duplicate=True,
                explanation="A refund for this order is already in"
                " progress - duplicate request blocked.",
            )
        else:
            customer = result["customer"]
            order = result["order"]
            ctx.state["refund_request"] = {
                "request_id": request_id,
                "customer": customer,
                "order": order,
                "refund_amount": result["refund_amount"],
                "reason": result["reason"],
                "eligibility": {
                    "status": result["status"],
                    "explanation": result["explanation"],
                    "checks": result["checks"],
                },
            }
            items = ", ".join(
                f"{i['qty']}x {i['name']}" for i in order["items"]
            )
            return Event(
                route="ELIGIBLE",
                output={
                    "request_id": request_id,
                    "customer_id": customer["customer_id"],
                    "customer_name": customer["name"],
                    "order_id": order["order_id"],
                    "items": items,
                    "refund_amount": result["refund_amount"],
                    "reason": result["reason"],
                    "eligibility_explanation": result["explanation"],
                },
            )

    return Event(
        route="NOT_ELIGIBLE",
        output={
            "status": result["status"],
            "customer_id": result["customer_id"],
            "order_id": result["order_id"],
            "explanation": result["explanation"],
        },
    )


def close_ineligible(node_input: dict) -> Event:
    """Close an ineligible or invalid request. No approval is possible."""
    status = node_input.get("status", "invalid")
    label = "not eligible" if status == "ineligible" else "invalid"
    text = f"Refund request {label}: {node_input.get('explanation', '')}"
    return Event(
        output={
            "status": "closed_" + status,
            "message": text,
            **{k: v for k, v in node_input.items() if k != "status"},
        },
        message=text,
    )


# ---------------------------------------------------------------------------
# LLM review agent (invoked only for eligible requests)
# ---------------------------------------------------------------------------


def emit_refund_alert(
    customer_id: str,
    order_id: str,
    refund_amount: float,
    summary: str,
) -> dict:
    """Emit a structured log alerting an approver to a pending refund.

    Cloud Run captures JSON stdout as structured logs in Cloud Logging.
    A log-based metric and alert policy trigger email notifications
    when these logs appear.

    Args:
        customer_id: The customer requesting the refund.
        order_id: The order being refunded.
        refund_amount: The refund amount in USD.
        summary: A short summary for the human approver.

    Returns:
        Confirmation that the alert was emitted.
    """
    log_entry = {
        "severity": "WARNING",
        "message": (
            f"Refund approval needed: ${refund_amount:.2f} for {order_id}"
            f" ({customer_id}) - {summary}"
        ),
        "alert_type": "refund_review",
        "customer_id": customer_id,
        "order_id": order_id,
        "amount": refund_amount,
        "summary": summary,
    }
    print(json.dumps(log_entry), flush=True)
    return {"status": "alert_emitted", "order_id": order_id}


def _review_fallback(
    callback_context: CallbackContext,
    llm_request: LlmRequest,
    error: Exception,
) -> LlmResponse:
    """Keep the workflow moving if the LLM call fails.

    The LLM only writes an explanation; eligibility is already decided in
    code. Without this, a model error would abort the run and leave the
    order reserved in the ledger. Instead the request still goes to human
    approval, with the deterministic eligibility result in the payload.
    """
    refunds.record_audit(
        "llm_review_failed", severity="ERROR", error=str(error)
    )
    return LlmResponse(
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    text="AI summary unavailable (model error). Review the"
                    " rules-engine eligibility result before deciding."
                )
            ],
        ),
        turn_complete=True,
    )


review_agent = Agent(
    name="review_agent",
    model=config.model,
    mode="single_turn",
    instruction="""You are a customer refund operations assistant. You receive
a refund request that deterministic business rules have ALREADY marked as
eligible. You do not decide eligibility and you must not change the amount.

1. Call the `emit_refund_alert` tool with the customer_id, order_id,
   refund_amount, and a one or two sentence summary for the human approver.
2. Then reply with a short explanation for the operator:
   - Customer and order
   - Refund amount and reason
   - Why it is eligible (use the eligibility explanation)
   - That the refund is now WAITING FOR HUMAN APPROVAL and has NOT been
     processed.""",
    input_schema=RefundReview,
    tools=[emit_refund_alert],
    on_model_error_callback=_review_fallback,
)


# ---------------------------------------------------------------------------
# HITL: pause the workflow for human approval
# ---------------------------------------------------------------------------


def request_approval(node_input, ctx: Context):  # type: ignore[no-untyped-def]
    """Pause the workflow and wait for a human to approve or reject.

    Yields a ``RequestInput`` that the ADK runtime surfaces to the UI.
    The workflow stays paused until someone resumes the session (via the
    approval UI or ``POST /run``). The human's response becomes the
    output of this node and flows into ``process_decision``.
    """
    request = ctx.state.get("refund_request", {})
    yield RequestInput(
        message=(
            f"Refund of ${request.get('refund_amount', 0):.2f} for order"
            f" {request.get('order', {}).get('order_id', '?')} requires"
            " approval. Reply 'approve' or 'reject'."
        ),
        payload=request,
    )


_APPROVE_WORDS = {"approve", "approved", "yes", "y"}
_REJECT_WORDS = {"reject", "rejected", "deny", "denied", "no", "n"}


def _parse_decision(node_input: Any) -> str:
    """Return "approve", "reject", or "unknown" (treated as reject)."""
    value = _content_to_text(node_input)
    # Unwrap JSON strings and {"decision"|"result"|"response": ...} dicts
    # (the approval UI and the ADK web dialog use different shapes).
    for _ in range(3):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                break
        if isinstance(value, dict):
            value = next(
                (
                    value[k]
                    for k in ("decision", "result", "response")
                    if k in value
                ),
                "",
            )
        else:
            break
    word = str(value or "").strip().lower().rstrip(".!")
    if word in _APPROVE_WORDS:
        return "approve"
    if word in _REJECT_WORDS:
        return "reject"
    return "unknown"


def process_decision(node_input, ctx: Context) -> Event:  # type: ignore[no-untyped-def]
    """Apply the human decision. Simulates the refund only on approval."""
    request = ctx.state.get("refund_request")
    if not request:
        text = "No refund request found in this session."
        refunds.record_audit("workflow_error", severity="ERROR", error=text)
        return Event(output={"status": "error", "message": text}, message=text)

    order_id = request["order"]["order_id"]
    customer_id = request["customer"]["customer_id"]
    request_id = request["request_id"]
    amount = request["refund_amount"]
    decision = _parse_decision(node_input)

    refunds.record_audit(
        "approval_decision",
        request_id=request_id,
        order_id=order_id,
        customer_id=customer_id,
        decision=decision,
    )

    try:
        if decision != "approve":
            refunds.release_refund(order_id, request_id)
            status = "rejected"
            text = (
                f"Refund of ${amount:.2f} for order {order_id} was rejected"
                " by the approver. No refund was issued."
            )
            if decision == "unknown":
                text += " (Unrecognised decision; treated as a rejection.)"
        else:
            # Defence in depth: re-check the rules at approval time.
            recheck = refunds.evaluate_policy(
                customer_id, order_id, request["reason"], date.today()
            )
            if not recheck["eligible"]:
                refunds.release_refund(order_id, request_id)
                status = "blocked"
                text = (
                    f"Approval blocked: order {order_id} is no longer"
                    f" eligible. {recheck['explanation']}"
                )
            else:
                outcome = refunds.simulate_refund(order_id, request_id)
                if outcome["success"]:
                    status = "refunded"
                    text = (
                        f"SIMULATED refund {outcome['refund_id']} of"
                        f" ${amount:.2f} recorded for order {order_id}"
                        f" ({request['customer']['name']}). No real payment"
                        " was issued."
                    )
                else:
                    status = (
                        "duplicate" if outcome.get("duplicate") else "failed"
                    )
                    text = f"Refund not processed: {outcome['error']}"
    except Exception as exc:
        refunds.release_refund(order_id, request_id)
        status = "error"
        text = f"Refund processing failed; no refund issued. ({exc})"
        refunds.record_audit(
            "workflow_error",
            severity="ERROR",
            order_id=order_id,
            error=str(exc),
        )

    refunds.record_audit(
        "refund_outcome",
        severity="INFO" if status == "refunded" else "WARNING",
        request_id=request_id,
        order_id=order_id,
        status=status,
    )
    ctx.state["refund_outcome"] = {"status": status, "message": text}
    return Event(
        output={"status": status, "order_id": order_id, "message": text},
        message=text,
    )


# ---------------------------------------------------------------------------
# Graph-based workflow — the root agent
# ---------------------------------------------------------------------------

root_agent = Workflow(
    name="refund_processor",
    edges=[
        ("START", parse_refund_request, evaluate_refund),
        (
            evaluate_refund,
            {
                "ELIGIBLE": review_agent,
                "NOT_ELIGIBLE": close_ineligible,
            },
        ),
        (review_agent, request_approval, process_decision),
    ],
)
