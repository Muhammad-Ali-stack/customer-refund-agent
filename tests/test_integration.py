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

"""Integration tests for the refund operations agent.

Spins up both backend (ADK + triggers) and frontend (approval UI proxy)
using ASGI test transports — no real HTTP servers needed. Tests the full
workflow: trigger → eligibility → session query → HITL approval →
simulated refund.
"""

import base64
import json

import httpx
import pytest
import pytest_asyncio

from frontend.main import app as frontend_app
from refund_agent import refunds
from refund_agent.fast_api_app import app as backend_app


@pytest_asyncio.fixture
async def backend():
    """ASGI client for the backend (ADK agent + triggers)."""
    transport = httpx.ASGITransport(app=backend_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://backend"
    ) as c:
        yield c


@pytest_asyncio.fixture
async def frontend(monkeypatch):
    """ASGI client for the frontend (approval UI proxy).

    Patches BACKEND_URL so the frontend proxies to the backend's ASGI
    transport instead of making real HTTP calls.
    """
    # The frontend makes HTTP calls to BACKEND_URL. We need to patch
    # those to go through the backend's ASGI transport instead.
    # Monkeypatch the backend URL used by the frontend.
    import frontend.main as frontend_module

    monkeypatch.setattr(frontend_module, "BACKEND_URL", "http://backend")

    # Create a shared backend transport for the frontend's httpx calls
    backend_transport = httpx.ASGITransport(app=backend_app)

    # Patch httpx.AsyncClient in the frontend to use the ASGI transport
    original_async_client = httpx.AsyncClient

    class PatchedAsyncClient(original_async_client):
        def __init__(self, **kwargs):
            kwargs.setdefault("transport", backend_transport)
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", PatchedAsyncClient)

    transport = httpx.ASGITransport(app=frontend_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://frontend"
    ) as c:
        yield c


def _make_pubsub_payload(request: dict) -> dict:
    """Build a Pub/Sub trigger request body from a refund request dict."""
    encoded = base64.b64encode(json.dumps(request).encode()).decode()
    return {
        "message": {
            "data": encoded,
            "attributes": {"source": "test"},
        },
        "subscription": "test-sub",
    }


async def _submit(backend, customer_id: str, order_id: str, reason: str):
    resp = await backend.post(
        "/apps/refund_agent/trigger/pubsub",
        json=_make_pubsub_payload(
            {
                "customer_id": customer_id,
                "order_id": order_id,
                "reason": reason,
            }
        ),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"


async def _decide(frontend, item: dict, decision: str) -> dict:
    """Resume the paused workflow through the approval UI proxy."""
    body = {
        "appName": "refund_agent",
        "userId": item["user_id"],
        "sessionId": item["session_id"],
        "newMessage": {
            "role": "user",
            "parts": [
                {
                    "functionResponse": {
                        "id": item["interrupt_id"],
                        "name": "adk_request_input",
                        "response": {
                            "result": json.dumps({"decision": decision}),
                        },
                    }
                }
            ],
        },
    }
    resp = await frontend.post("/approve", json=body)
    assert resp.status_code == 200
    return resp.json()[-1]


def _audit_events(event_type: str) -> list[dict]:
    return [e for e in refunds.AUDIT_LOG if e["audit_event"] == event_type]


@pytest.mark.asyncio
async def test_eligible_refund_approved(backend, frontend):
    """Eligible request pauses for approval; approval simulates the refund."""
    await _submit(backend, "CUST-1001", "ORD-5001", "Item arrived damaged")

    resp = await frontend.get("/pending-approvals")
    assert resp.status_code == 200
    pending = resp.json()
    assert len(pending) == 1

    item = pending[0]
    payload = item["payload"]
    assert payload["customer"]["name"] == "Maria Lopez"
    assert payload["order"]["order_id"] == "ORD-5001"
    assert payload["refund_amount"] == 129.99
    assert payload["reason"] == "Item arrived damaged"
    assert payload["eligibility"]["status"] == "eligible"
    assert item["interrupt_id"]

    # Nothing is refunded before the human decides.
    assert _audit_events("refund_simulated") == []

    last_event = await _decide(frontend, item, "approve")
    assert last_event["output"]["status"] == "refunded"
    assert "SIMULATED" in last_event["output"]["message"]
    assert "process_decision" in last_event["nodeInfo"]["path"]
    assert refunds.REFUND_LEDGER["ORD-5001"]["status"] == "refunded"
    assert len(_audit_events("refund_simulated")) == 1

    resp = await frontend.get("/pending-approvals")
    assert resp.json() == []


@pytest.mark.asyncio
async def test_eligible_refund_rejected(backend, frontend):
    """Rejecting an eligible request issues no refund and frees the order."""
    await _submit(backend, "CUST-1002", "ORD-5003", "Wrong size")

    pending = (await frontend.get("/pending-approvals")).json()
    item = next(
        p for p in pending if p["payload"]["order"]["order_id"] == "ORD-5003"
    )

    last_event = await _decide(frontend, item, "reject")
    assert last_event["output"]["status"] == "rejected"
    assert _audit_events("refund_simulated") == []
    assert "ORD-5003" not in refunds.REFUND_LEDGER
    decisions = _audit_events("approval_decision")
    assert decisions[-1]["decision"] == "reject"

    assert (await frontend.get("/pending-approvals")).json() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("customer_id", "order_id"),
    [
        ("CUST-1001", "ORD-5002"),  # outside refund window
        ("CUST-1002", "ORD-5005"),  # over the refund limit
        ("CUST-1003", "ORD-5006"),  # suspended account
        ("CUST-1001", "ORD-5003"),  # another customer's order
        ("CUST-9999", "ORD-5001"),  # unknown customer
    ],
)
async def test_ineligible_requests_never_reach_approval(
    backend, frontend, customer_id, order_id
):
    """Ineligible/invalid requests are closed without an approval step."""
    await _submit(backend, customer_id, order_id, "Damaged")

    assert (await frontend.get("/pending-approvals")).json() == []
    assert refunds.REFUND_LEDGER == {}
    assert _audit_events("refund_simulated") == []
    assert _audit_events("eligibility_check")[-1]["status"] in (
        "ineligible",
        "invalid",
    )


@pytest.mark.asyncio
async def test_cross_customer_access_is_audited(backend):
    await _submit(backend, "CUST-1001", "ORD-5003", "Damaged")
    denied = _audit_events("cross_customer_access_denied")
    assert denied and denied[-1]["requested_by"] == "CUST-1001"


@pytest.mark.asyncio
async def test_duplicate_requests_blocked(backend, frontend):
    """A second request for the same order is blocked while pending and
    after the refund has been processed."""
    await _submit(backend, "CUST-1001", "ORD-5001", "Damaged")
    await _submit(backend, "CUST-1001", "ORD-5001", "Damaged again")

    pending = (await frontend.get("/pending-approvals")).json()
    assert len(pending) == 1
    assert _audit_events("eligibility_check")[-1]["status"] == "ineligible"

    await _decide(frontend, pending[0], "approve")

    await _submit(backend, "CUST-1001", "ORD-5001", "Damaged a third time")
    assert (await frontend.get("/pending-approvals")).json() == []
    assert len(_audit_events("refund_simulated")) == 1


@pytest.mark.asyncio
async def test_llm_failure_still_reaches_approval(
    backend, frontend, monkeypatch
):
    """A model error must not abort the run or strand the ledger lock."""
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.google_llm import Gemini

    async def _failing_llm(self, llm_request, stream=False):
        raise RuntimeError("simulated model outage")
        yield  # pragma: no cover -- makes this an async generator

    monkeypatch.setattr(Gemini, "generate_content_async", _failing_llm)
    monkeypatch.setattr(BaseLlm, "generate_content_async", _failing_llm)

    await _submit(backend, "CUST-1001", "ORD-5001", "Damaged")

    assert _audit_events("llm_review_failed")
    pending = (await frontend.get("/pending-approvals")).json()
    assert len(pending) == 1
    last_event = await _decide(frontend, pending[0], "approve")
    assert last_event["output"]["status"] == "refunded"


@pytest.mark.asyncio
async def test_malformed_input_is_rejected(backend, frontend):
    """Non-JSON garbage is handled without crashing or pausing."""
    resp = await backend.post(
        "/apps/refund_agent/trigger/pubsub",
        json={
            "message": {
                "data": base64.b64encode(b"not a refund").decode(),
                "attributes": {},
            },
            "subscription": "test-sub",
        },
    )
    assert resp.status_code == 200
    assert (await frontend.get("/pending-approvals")).json() == []
    assert _audit_events("invalid_request")


@pytest.mark.asyncio
async def test_subscription_normalization(backend):
    """Full Pub/Sub subscription paths are normalized to short names."""
    payload = _make_pubsub_payload(
        {"customer_id": "CUST-1001", "order_id": "ORD-5002", "reason": "Late"}
    )
    # Use a full subscription path like real Pub/Sub sends
    payload["subscription"] = "projects/my-project/subscriptions/test-sub"

    resp = await backend.post(
        "/apps/refund_agent/trigger/pubsub",
        json=payload,
    )
    assert resp.status_code == 200

    # Session should be queryable with the short name
    resp = await backend.get("/apps/refund_agent/users/test-sub/sessions")
    assert resp.status_code == 200
    sessions = resp.json()
    assert len(sessions) >= 1


@pytest.mark.asyncio
async def test_ui_submit_eligible_then_approve(frontend):
    """A request typed in the UI pauses for approval and can be approved."""
    resp = await frontend.post(
        "/submit",
        json={
            "text": "Check whether customer CUST-1001 can receive a refund"
            " for order ORD-5001 because the item arrived damaged."
        },
    )
    assert resp.status_code == 200
    result = resp.json()
    assert result["status"] == "pending_approval"
    assert "Eligible" in result["message"]

    pending = (await frontend.get("/pending-approvals")).json()
    assert len(pending) == 1
    last_event = await _decide(frontend, pending[0], "approve")
    assert last_event["output"]["status"] == "refunded"


@pytest.mark.asyncio
async def test_ui_submit_ineligible(frontend):
    resp = await frontend.post(
        "/submit",
        json={
            "text": "Refund order ORD-5002 for customer CUST-1001 because"
            " it stopped working."
        },
    )
    result = resp.json()
    assert result["status"] == "closed"
    assert "refund window" in result["message"]
    assert (await frontend.get("/pending-approvals")).json() == []


@pytest.mark.asyncio
async def test_ui_submit_empty(frontend):
    result = (await frontend.post("/submit", json={"text": "  "})).json()
    assert result["status"] == "error"
