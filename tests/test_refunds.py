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

"""Unit tests for the deterministic refund rules, lookups and ledger."""

import json

import pytest
from google.genai import types

from refund_agent import refunds
from refund_agent.agent import _parse_decision, parse_refund_request

# --- Lookups ----------------------------------------------------------------


def test_lookup_customer_found() -> None:
    result = refunds.lookup_customer("cust-1001")
    assert result["found"] is True
    assert result["customer"] == {
        "customer_id": "CUST-1001",
        "name": "Maria Lopez",
        "email": "maria.lopez@example.com",
        "account_status": "active",
    }


@pytest.mark.parametrize("customer_id", ["CUST-9999", "", "bogus", None])
def test_lookup_customer_unknown_or_invalid(customer_id) -> None:
    result = refunds.lookup_customer(customer_id)
    assert result["found"] is False
    assert "error" in result


def test_lookup_order_owned_by_customer() -> None:
    result = refunds.lookup_order("ORD-5001", "CUST-1001")
    assert result["found"] is True
    order = result["order"]
    for field in (
        "order_id",
        "customer_id",
        "items",
        "amount",
        "status",
        "purchase_date",
        "payment_method",
    ):
        assert field in order


def test_lookup_order_cross_customer_is_hidden() -> None:
    """Another customer's order looks exactly like a missing order."""
    other = refunds.lookup_order("ORD-5003", "CUST-1001")
    missing = refunds.lookup_order("ORD-9999", "CUST-1001")
    assert other["found"] is False
    assert "order" not in other
    assert other["error"].replace("5003", "X") == missing["error"].replace(
        "9999", "X"
    )
    assert (
        refunds.AUDIT_LOG[-1]["audit_event"] == "cross_customer_access_denied"
    )


# --- Eligibility ------------------------------------------------------------


def test_second_customer_eligible_refund() -> None:
    result = refunds.check_refund_eligibility(
        "CUST-1002", "ORD-5008", "Speaker stopped charging"
    )
    assert result["status"] == "eligible"
    assert result["refund_amount"] == 54.99


def test_eligible_refund() -> None:
    result = refunds.check_refund_eligibility(
        "CUST-1001", "ORD-5001", "Item arrived damaged"
    )
    assert result["status"] == "eligible"
    assert result["eligible"] is True
    assert result["refund_amount"] == 129.99
    assert all(c["passed"] for c in result["checks"])
    assert refunds.AUDIT_LOG[-1]["audit_event"] == "eligibility_check"


@pytest.mark.parametrize(
    ("customer_id", "order_id", "expected_fragment"),
    [
        ("CUST-1001", "ORD-5002", "refund window"),
        ("CUST-1002", "ORD-5004", "only delivered orders"),
        ("CUST-1002", "ORD-5005", "limit"),
        ("CUST-1003", "ORD-5006", "suspended"),
        ("CUST-1001", "ORD-5007", "already been refunded"),
    ],
)
def test_ineligible_refunds(customer_id, order_id, expected_fragment) -> None:
    result = refunds.check_refund_eligibility(customer_id, order_id, "Damaged")
    assert result["status"] == "ineligible"
    assert result["eligible"] is False
    assert result["refund_amount"] == 0.0
    assert expected_fragment in result["explanation"]


@pytest.mark.parametrize(
    ("customer_id", "order_id", "reason"),
    [
        ("CUST-1001", "ORD-5003", "Damaged"),  # cross-customer
        ("CUST-9999", "ORD-5001", "Damaged"),  # unknown customer
        ("CUST-1001", "ORD-9999", "Damaged"),  # unknown order
        ("CUST-1001", "not-an-order", "Damaged"),  # malformed ID
        ("CUST-1001", "ORD-5001", "   "),  # missing reason
        ("CUST-1001", "ORD-5001", "x" * 501),  # reason too long
    ],
)
def test_invalid_inputs(customer_id, order_id, reason) -> None:
    result = refunds.check_refund_eligibility(customer_id, order_id, reason)
    assert result["status"] == "invalid"
    assert result["eligible"] is False
    assert "order" not in result or result["order"]["customer_id"] == (
        customer_id
    )


# --- Ledger: duplicates and simulated refunds -------------------------------


def test_duplicate_request_blocked_while_pending() -> None:
    assert refunds.reserve_refund("ORD-5001", "REQ-A", 129.99) is True
    assert refunds.reserve_refund("ORD-5001", "REQ-B", 129.99) is False

    result = refunds.check_refund_eligibility(
        "CUST-1001", "ORD-5001", "Damaged"
    )
    assert result["eligible"] is False
    assert result.get("duplicate") is True


def test_simulated_refund_only_once() -> None:
    refunds.reserve_refund("ORD-5001", "REQ-A", 129.99)

    first = refunds.simulate_refund("ORD-5001", "REQ-A")
    second = refunds.simulate_refund("ORD-5001", "REQ-A")

    assert first["success"] is True
    assert first["refund_id"].startswith("SIM-RF-")
    assert second["success"] is False
    assert second["duplicate"] is True
    simulated = [
        e for e in refunds.AUDIT_LOG if e["audit_event"] == "refund_simulated"
    ]
    assert len(simulated) == 1


def test_simulated_refund_requires_matching_reservation() -> None:
    assert refunds.simulate_refund("ORD-5001", "REQ-A")["success"] is False
    refunds.reserve_refund("ORD-5001", "REQ-A", 129.99)
    assert refunds.simulate_refund("ORD-5001", "REQ-OTHER")["success"] is False


def test_release_allows_new_request_after_rejection() -> None:
    refunds.reserve_refund("ORD-5001", "REQ-A", 129.99)
    refunds.release_refund("ORD-5001", "REQ-A")
    result = refunds.check_refund_eligibility(
        "CUST-1001", "ORD-5001", "Damaged"
    )
    assert result["eligible"] is True


# --- Input parsing ----------------------------------------------------------


def test_parse_chat_message() -> None:
    message = types.Content(
        role="user",
        parts=[
            types.Part(
                text="Check whether customer CUST-1001 can receive a refund"
                " for order ORD-5001 because the item arrived damaged."
            )
        ],
    )
    assert parse_refund_request(message).output == {
        "customer_id": "CUST-1001",
        "order_id": "ORD-5001",
        "reason": "the item arrived damaged",
    }


def test_parse_pubsub_json() -> None:
    event = json.dumps(
        {
            "data": {
                "customer_id": "CUST-1001",
                "order_id": "ORD-5001",
                "reason": "Damaged",
            },
            "attributes": {},
        }
    )
    assert parse_refund_request(event).output["order_id"] == "ORD-5001"


@pytest.mark.parametrize("bad", ["{not json", "", "   ", "[1, 2]"])
def test_parse_malformed_input(bad) -> None:
    output = parse_refund_request(bad).output
    assert "error" in output or not output.get("order_id")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"decision": "approve"}', "approve"),
        ('{"decision": "reject"}', "reject"),
        ("Approve", "approve"),
        ("no", "reject"),
        ({"result": '{"decision": "approve"}'}, "approve"),
        ({"response": "reject"}, "reject"),
        ("disapprove", "unknown"),
        ({"decision": "maybe"}, "unknown"),
    ],
)
def test_parse_decision(raw, expected) -> None:
    assert _parse_decision(raw) == expected
