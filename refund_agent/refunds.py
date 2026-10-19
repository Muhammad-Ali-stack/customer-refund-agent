# Copyright 2026 Muhammad Ali
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

"""Deterministic refund operations: mock data, lookups, rules, and ledger.

Everything that decides whether money moves lives here, in plain Python.
The LLM never decides eligibility — it only explains results that this
module has already computed.

No real payment is ever issued. ``simulate_refund`` only records a
simulated refund in an in-memory ledger.
"""

import json
import re
import threading
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

# ---------------------------------------------------------------------------
# Business rules
# ---------------------------------------------------------------------------

REFUND_WINDOW_DAYS = 30
MAX_REFUND_AMOUNT = 1000.00
MAX_REASON_LENGTH = 500

CUSTOMER_ID_PATTERN = re.compile(r"^CUST-\d{4}$")
ORDER_ID_PATTERN = re.compile(r"^ORD-\d{4}$")

# ---------------------------------------------------------------------------
# Mock data. Dates are relative to today so the demo never goes stale.
# ---------------------------------------------------------------------------

_TODAY = date.today()


def _days_ago(days: int) -> str:
    return (_TODAY - timedelta(days=days)).isoformat()


CUSTOMERS: dict[str, dict[str, str]] = {
    "CUST-1001": {
        "customer_id": "CUST-1001",
        "name": "Maria Lopez",
        "email": "maria.lopez@example.com",
        "account_status": "active",
    },
    "CUST-1002": {
        "customer_id": "CUST-1002",
        "name": "James Chen",
        "email": "james.chen@example.com",
        "account_status": "active",
    },
    "CUST-1003": {
        "customer_id": "CUST-1003",
        "name": "Priya Nair",
        "email": "priya.nair@example.com",
        "account_status": "suspended",
    },
}

ORDERS: dict[str, dict[str, Any]] = {
    # Eligible: recent, delivered, under the limit.
    "ORD-5001": {
        "order_id": "ORD-5001",
        "customer_id": "CUST-1001",
        "items": [{"sku": "HP-200", "name": "Wireless headphones", "qty": 1}],
        "amount": 129.99,
        "status": "delivered",
        "purchase_date": _days_ago(12),
        "payment_method": "Visa ending 4242",
    },
    # Ineligible: outside the 30-day window.
    "ORD-5002": {
        "order_id": "ORD-5002",
        "customer_id": "CUST-1001",
        "items": [{"sku": "CM-10", "name": "Coffee maker", "qty": 1}],
        "amount": 89.50,
        "status": "delivered",
        "purchase_date": _days_ago(45),
        "payment_method": "Visa ending 4242",
    },
    # Belongs to CUST-1002 — used for cross-customer access checks.
    "ORD-5003": {
        "order_id": "ORD-5003",
        "customer_id": "CUST-1002",
        "items": [{"sku": "RS-42", "name": "Running shoes", "qty": 1}],
        "amount": 74.00,
        "status": "delivered",
        "purchase_date": _days_ago(5),
        "payment_method": "Mastercard ending 5100",
    },
    # Ineligible: not delivered yet.
    "ORD-5004": {
        "order_id": "ORD-5004",
        "customer_id": "CUST-1002",
        "items": [{"sku": "DL-7", "name": "Desk lamp", "qty": 2}],
        "amount": 39.99,
        "status": "shipped",
        "purchase_date": _days_ago(2),
        "payment_method": "Mastercard ending 5100",
    },
    # Ineligible: above the refund limit.
    "ORD-5005": {
        "order_id": "ORD-5005",
        "customer_id": "CUST-1002",
        "items": [{"sku": "LT-15", "name": "Laptop", "qty": 1}],
        "amount": 1499.00,
        "status": "delivered",
        "purchase_date": _days_ago(8),
        "payment_method": "PayPal",
    },
    # Ineligible: customer account is suspended.
    "ORD-5006": {
        "order_id": "ORD-5006",
        "customer_id": "CUST-1003",
        "items": [{"sku": "BP-3", "name": "Backpack", "qty": 1}],
        "amount": 59.00,
        "status": "delivered",
        "purchase_date": _days_ago(6),
        "payment_method": "Visa ending 1881",
    },
    # Ineligible: already refunded.
    "ORD-5007": {
        "order_id": "ORD-5007",
        "customer_id": "CUST-1001",
        "items": [{"sku": "PC-1", "name": "Phone case", "qty": 1}],
        "amount": 19.99,
        "status": "refunded",
        "purchase_date": _days_ago(20),
        "payment_method": "Visa ending 4242",
    },
    # Eligible: second order for CUST-1002.
    "ORD-5008": {
        "order_id": "ORD-5008",
        "customer_id": "CUST-1002",
        "items": [{"sku": "BS-5", "name": "Bluetooth speaker", "qty": 1}],
        "amount": 54.99,
        "status": "delivered",
        "purchase_date": _days_ago(3),
        "payment_method": "Mastercard ending 5100",
    },
}

# ---------------------------------------------------------------------------
# Audit log and refund ledger (in-memory, per process)
# ---------------------------------------------------------------------------

AUDIT_LOG: list[dict[str, Any]] = []

# order_id -> {"status": "pending_approval" | "refunded", "request_id", ...}
REFUND_LEDGER: dict[str, dict[str, Any]] = {}

_ledger_lock = threading.Lock()


def record_audit(
    event_type: str, severity: str = "INFO", **fields: Any
) -> dict:
    """Append an audit record and emit it as a structured JSON log line."""
    entry = {
        "timestamp": datetime.now(UTC).isoformat(),
        "severity": severity,
        "audit_event": event_type,
        "message": f"refund_audit: {event_type}",
        **fields,
    }
    AUDIT_LOG.append(entry)
    print(json.dumps(entry, default=str), flush=True)
    return entry


def reset_state() -> None:
    """Clear the audit log and ledger. Used by tests."""
    with _ledger_lock:
        AUDIT_LOG.clear()
        REFUND_LEDGER.clear()


def _normalize_id(value: Any) -> str:
    return str(value or "").strip().upper()


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def lookup_customer(customer_id: str) -> dict:
    """Look up a customer by ID.

    Args:
        customer_id: Customer ID in the form CUST-1234.

    Returns:
        ``{"found": True, "customer": {...}}`` with ID, name, email and
        account status, or ``{"found": False, "error": "..."}``.
    """
    cid = _normalize_id(customer_id)
    if not CUSTOMER_ID_PATTERN.match(cid):
        return {
            "found": False,
            "error": "Invalid customer ID format. Expected CUST-1234.",
        }
    customer = CUSTOMERS.get(cid)
    if customer is None:
        return {"found": False, "error": f"Customer {cid} not found."}
    return {"found": True, "customer": dict(customer)}


def lookup_order(order_id: str, customer_id: str) -> dict:
    """Look up an order on behalf of a specific customer.

    The requesting customer's ID is required: an order that belongs to a
    different customer is reported exactly like a missing order, so the
    caller learns nothing about other customers' orders.

    Args:
        order_id: Order ID in the form ORD-1234.
        customer_id: The customer the request is made on behalf of.

    Returns:
        ``{"found": True, "order": {...}}`` or
        ``{"found": False, "error": "..."}``.
    """
    oid = _normalize_id(order_id)
    cid = _normalize_id(customer_id)
    if not ORDER_ID_PATTERN.match(oid):
        return {
            "found": False,
            "error": "Invalid order ID format. Expected ORD-1234.",
        }
    order = ORDERS.get(oid)
    if order is None or order["customer_id"] != cid:
        if order is not None:
            record_audit(
                "cross_customer_access_denied",
                severity="WARNING",
                requested_by=cid,
                order_id=oid,
            )
        return {
            "found": False,
            "error": f"Order {oid} not found for customer {cid}.",
        }
    return {
        "found": True,
        "order": {**order, "items": [dict(i) for i in order["items"]]},
    }


def evaluate_policy(
    customer_id: str, order_id: str, reason: str, today: date
) -> dict:
    """Apply the refund rules without touching the ledger."""
    cid = _normalize_id(customer_id)
    oid = _normalize_id(order_id)
    reason = str(reason or "").strip()
    checks: list[dict[str, Any]] = []

    def result(status: str, explanation: str, **extra: Any) -> dict:
        return {
            "status": status,
            "eligible": status == "eligible",
            "customer_id": cid,
            "order_id": oid,
            "reason": reason,
            "refund_amount": extra.pop("refund_amount", 0.0),
            "checks": checks,
            "explanation": explanation,
            **extra,
        }

    # 1. Input validation
    if not reason:
        return result("invalid", "A refund reason is required.")
    if len(reason) > MAX_REASON_LENGTH:
        return result(
            "invalid",
            f"Refund reason exceeds {MAX_REASON_LENGTH} characters.",
        )

    # 2. Customer
    customer_lookup = lookup_customer(cid)
    if not customer_lookup["found"]:
        return result("invalid", customer_lookup["error"])
    customer = customer_lookup["customer"]
    active = customer["account_status"] == "active"
    checks.append({"check": "account_active", "passed": active})
    if not active:
        return result(
            "ineligible",
            f"Customer account is {customer['account_status']}; refunds"
            " require an active account.",
            customer=customer,
        )

    # 3. Order ownership (lookup_order enforces it)
    order_lookup = lookup_order(oid, cid)
    checks.append(
        {"check": "order_owned_by_customer", "passed": order_lookup["found"]}
    )
    if not order_lookup["found"]:
        return result("invalid", order_lookup["error"], customer=customer)
    order = order_lookup["order"]
    amount = float(order["amount"])

    # 4. Order status
    delivered = order["status"] == "delivered"
    checks.append({"check": "order_delivered", "passed": delivered})
    if order["status"] == "refunded":
        return result(
            "ineligible",
            f"Order {oid} has already been refunded.",
            customer=customer,
            order=order,
        )
    if not delivered:
        return result(
            "ineligible",
            f"Order {oid} is '{order['status']}'; only delivered orders"
            " can be refunded.",
            customer=customer,
            order=order,
        )

    # 5. Refund window
    purchased = date.fromisoformat(order["purchase_date"])
    age_days = (today - purchased).days
    in_window = 0 <= age_days <= REFUND_WINDOW_DAYS
    checks.append({"check": "within_refund_window", "passed": in_window})
    if not in_window:
        return result(
            "ineligible",
            f"Order was purchased {age_days} days ago; the refund window is"
            f" {REFUND_WINDOW_DAYS} days.",
            customer=customer,
            order=order,
        )

    # 6. Amount
    under_limit = 0 < amount <= MAX_REFUND_AMOUNT
    checks.append({"check": "amount_within_limit", "passed": under_limit})
    if not under_limit:
        return result(
            "ineligible",
            f"Order amount ${amount:.2f} exceeds the ${MAX_REFUND_AMOUNT:.2f}"
            " self-service refund limit; escalate to finance.",
            customer=customer,
            order=order,
        )

    return result(
        "eligible",
        f"Eligible for a full refund of ${amount:.2f}: active account,"
        f" order owned by customer, delivered, purchased {age_days} days"
        f" ago (within {REFUND_WINDOW_DAYS}), and under the"
        f" ${MAX_REFUND_AMOUNT:.2f} limit. Human approval is required"
        " before the refund is processed.",
        refund_amount=amount,
        customer=customer,
        order=order,
    )


def check_refund_eligibility(
    customer_id: str, order_id: str, reason: str
) -> dict:
    """Check whether an order is eligible for a refund.

    Validates the customer, order ownership, order status, the refund
    window, the refund amount, and whether a refund for this order is
    already pending or completed.

    Args:
        customer_id: Customer ID in the form CUST-1234.
        order_id: Order ID in the form ORD-1234.
        reason: Why the customer wants a refund.

    Returns:
        A dict with ``status`` ("eligible", "ineligible" or "invalid"),
        ``eligible``, ``refund_amount``, ``checks`` and ``explanation``.
    """
    try:
        outcome = evaluate_policy(customer_id, order_id, reason, date.today())
    except Exception as exc:
        outcome = {
            "status": "invalid",
            "eligible": False,
            "customer_id": _normalize_id(customer_id),
            "order_id": _normalize_id(order_id),
            "reason": str(reason or ""),
            "refund_amount": 0.0,
            "checks": [],
            "explanation": f"Eligibility check failed: {exc}",
        }

    existing = REFUND_LEDGER.get(outcome["order_id"])
    if outcome["eligible"] and existing:
        outcome.update(
            status="ineligible",
            eligible=False,
            refund_amount=0.0,
            duplicate=True,
            explanation=(
                f"A refund for order {outcome['order_id']} is already"
                f" {existing['status'].replace('_', ' ')}"
                " - duplicate request blocked."
            ),
        )

    record_audit(
        "eligibility_check",
        customer_id=outcome["customer_id"],
        order_id=outcome["order_id"],
        status=outcome["status"],
        refund_amount=outcome["refund_amount"],
        explanation=outcome["explanation"],
    )
    return outcome


# ---------------------------------------------------------------------------
# Ledger operations (duplicate prevention + simulated refund)
# ---------------------------------------------------------------------------


def new_request_id() -> str:
    return f"REQ-{uuid.uuid4().hex[:12].upper()}"


def reserve_refund(order_id: str, request_id: str, amount: float) -> bool:
    """Atomically claim an order for a refund request.

    Returns False if another request already holds or completed it.
    """
    with _ledger_lock:
        if order_id in REFUND_LEDGER:
            return False
        REFUND_LEDGER[order_id] = {
            "status": "pending_approval",
            "request_id": request_id,
            "amount": amount,
        }
    return True


def release_refund(order_id: str, request_id: str) -> None:
    """Release a pending reservation (e.g. after a rejection)."""
    with _ledger_lock:
        entry = REFUND_LEDGER.get(order_id)
        if (
            entry
            and entry["request_id"] == request_id
            and (entry["status"] == "pending_approval")
        ):
            del REFUND_LEDGER[order_id]


def simulate_refund(order_id: str, request_id: str) -> dict:
    """Simulate issuing a refund. No payment API is ever called.

    Only succeeds once, and only for the request that reserved the order.
    """
    with _ledger_lock:
        entry = REFUND_LEDGER.get(order_id)
        if entry is None or entry["request_id"] != request_id:
            return {
                "success": False,
                "error": "No pending reservation for this request.",
            }
        if entry["status"] == "refunded":
            return {
                "success": False,
                "duplicate": True,
                "error": f"Order {order_id} was already refunded"
                f" ({entry['refund_id']}).",
            }
        refund_id = f"SIM-RF-{uuid.uuid4().hex[:8].upper()}"
        entry.update(status="refunded", refund_id=refund_id)
        amount = entry["amount"]

    record_audit(
        "refund_simulated",
        order_id=order_id,
        request_id=request_id,
        refund_id=refund_id,
        amount=amount,
        note="SIMULATED - no payment was issued",
    )
    return {"success": True, "refund_id": refund_id, "amount": amount}
