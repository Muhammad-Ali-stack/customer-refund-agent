# Customer Refund Operations Agent

![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)
![Google ADK](https://img.shields.io/badge/Google%20ADK-2.x-4285F4)
![License](https://img.shields.io/badge/license-Apache%202.0-green)

A customer refund operations agent built on an **ADK 2.0 graph-based
workflow**. It looks up the customer and order, checks refund eligibility
with **deterministic Python rules**, explains the result, and **pauses for
human approval** before simulating the refund. Every step is written to an
audit log.

Author: **Muhammad Ali**. Built with [Google ADK](https://adk.dev), starting
from Google's `ambient-expense-agent` recipe (see [NOTICE](NOTICE)).

> **No real money moves.** Refunds are simulated in an in-memory ledger. No
> payment API is called.

| Attribute | Description |
| :--- | :--- |
| **Interaction Type** | Chat text or JSON event, with human approval |
| **Agent Type** | ADK 2.0 graph-based `Workflow` (function nodes + one LLM agent) |
| **Trigger Sources** | Pub/Sub push (local: `scripts/submit_refund.py`), ADK web UI |
| **Data** | Mock customers and orders in `refund_agent/refunds.py` |

## Contents

- [How It Works](#how-it-works)
- [Project Structure](#project-structure)
- [Getting Started](#getting-started-windows--powershell)
- [Demo Walkthrough](#demo-walkthrough)
- [Tests](#tests)
- [Limitations](#limitations)
- [License](#license)

## How It Works

```
 request (chat text or JSON)
          │
 parse_refund_request ── extracts CUST-xxxx, ORD-xxxx, reason
          │
 evaluate_refund ─────── lookup_customer → lookup_order → check_refund_eligibility
          │               (pure Python rules; reserves the order in the ledger)
     ┌────┴──────────┐
 NOT_ELIGIBLE     ELIGIBLE
     │               │
 close_ineligible  review_agent (LLM: explains, emits approver alert)
 (no approval        │
  possible)        request_approval ── RequestInput → workflow PAUSES
                     │
                   human approves / rejects (approval UI)
                     │
                   process_decision ── re-checks rules → simulate_refund
                                       or release the reservation
```

**Design choices**

- **The LLM never decides eligibility.** Rules live in
  [`refund_agent/refunds.py`](refund_agent/refunds.py). The LLM only writes
  the explanation for the approver. If the model call fails, an
  `on_model_error_callback` replaces its output with a fallback note, so the
  request still reaches a human.
- **Ineligible requests can't be approved.** They're routed to
  `close_ineligible` and never reach `RequestInput`. On approval,
  `process_decision` checks the rules again before simulating the refund.
- **Duplicate refunds are prevented.** An eligible request reserves its order
  in a refund ledger. A second request for the same order is blocked while the
  first is pending or after it's refunded. `simulate_refund` succeeds only
  once per reservation.
- **Cross-customer access is blocked.** `lookup_order(order_id, customer_id)`
  reports another customer's order exactly like a missing order, so no details
  leak. The attempt is audited.
- **Unclear decisions count as rejections.** Only `approve` / `approved` /
  `yes` approve a refund. `disapprove`, `maybe` or a malformed decision is
  treated as a rejection.

### Refund rules

Checked in order. The first failure decides the result.

| # | Rule | Failure result |
|---|---|---|
| 1 | Customer ID, order ID and reason are present and well-formed | `invalid` |
| 2 | Customer exists | `invalid` |
| 3 | Account is `active` | `ineligible` |
| 4 | Order exists **and belongs to the customer** | `invalid` |
| 5 | Order status is `delivered` (not shipped and not already refunded) | `ineligible` |
| 6 | Purchased within **30 days** | `ineligible` |
| 7 | Amount ≤ **$1,000** | `ineligible` (escalate to finance) |
| 8 | No refund already pending or completed for the order | `ineligible` (duplicate) |

### Mock data

Order dates are relative to today, so the demo never goes stale.

| Order | Customer | Amount | Scenario |
|---|---|---|---|
| ORD-5001 | CUST-1001 Maria Lopez | $129.99 | ✅ Eligible |
| ORD-5002 | CUST-1001 | $89.50 | ❌ Bought 45 days ago (outside window) |
| ORD-5003 | CUST-1002 James Chen | $74.00 | ✅ Eligible for CUST-1002, 🔒 hidden from CUST-1001 |
| ORD-5004 | CUST-1002 | $39.99 | ❌ Not delivered yet (shipped) |
| ORD-5005 | CUST-1002 | $1,499.00 | ❌ Over the refund limit |
| ORD-5006 | CUST-1003 Priya Nair | $59.00 | ❌ Account suspended |
| ORD-5007 | CUST-1001 | $19.99 | ❌ Already refunded |
| ORD-5008 | CUST-1002 | $54.99 | ✅ Eligible |

### Audit log

Each step prints a structured JSON line to the backend terminal and appends
to `refunds.AUDIT_LOG`. The event types are:

- `eligibility_check`
- `cross_customer_access_denied`
- `invalid_request`
- `llm_review_failed`
- `approval_decision`
- `refund_simulated`
- `refund_outcome`
- `workflow_error`

## Project Structure

```
refund-ops-agent/
├── refund_agent/
│   ├── agent.py          # ADK graph workflow: nodes, routing, review LLM agent
│   ├── refunds.py        # Mock data, eligibility rules, refund ledger, audit log
│   ├── config.py         # Settings read from environment / .env
│   └── fast_api_app.py   # Backend server (port 8080) + Pub/Sub trigger
├── frontend/
│   ├── main.py           # Approval UI server (port 8081)
│   └── static/approval.html
├── scripts/
│   └── submit_refund.py  # Send a Pub/Sub-shaped refund request locally
├── tests/                # Unit, integration and runnability tests
├── .env.example          # Template for your .env
└── pyproject.toml
```

## Getting Started (Windows / PowerShell)

**Prerequisites:** [uv](https://docs.astral.sh/uv/). uv installs a compatible
Python (3.11 or 3.12) for you. A system Python 3.13+ won't work, because the
project requires `<3.13`.

### 1. Install

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"

cd C:\refund-ops-agent
uv sync
cd frontend; uv sync; cd ..
```

### 2. Configure

```powershell
copy .env.example .env
notepad .env   # set GOOGLE_API_KEY=<your key>
```

| Variable | Required | Purpose |
|---|---|---|
| `GOOGLE_API_KEY` | Yes (or Vertex AI below) | [Google AI Studio key](https://aistudio.google.com/apikey) for the review LLM |
| `MODEL_NAME` | Yes | Defaults to `gemini-3.5-flash` in `.env.example` |
| `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION` | Only for Vertex AI | Use instead of an API key, after `gcloud auth application-default login` |
| `ADK_DISABLE_LOCAL_STORAGE=1` | Already set in `.env.example` | Keeps ADK sessions in memory so each restart starts clean |

Never commit `.env`. It's already listed in `.gitignore`.

> Without a valid key, everything still runs. Eligibility, approval and the
> simulated refund all work, and the approval card shows "AI summary
> unavailable".

### 3. Run (two terminals)

```powershell
# Terminal 1: backend agent (http://localhost:8080)
uv run python refund_agent/fast_api_app.py
```

```powershell
# Terminal 2: approval UI (http://localhost:8081/approval)
cd frontend
$env:BACKEND_URL = "http://localhost:8080"
uv run python main.py
```

### 4. Send a request

**In the browser:** open <http://localhost:8081/approval>, type the request
in the **New refund request** box and click **Submit request**. The result
appears right under the box. Eligible requests also show up as an approval
card below it.

**Or from a third terminal:**

```powershell
uv run python scripts/submit_refund.py "Check whether customer CUST-1001 can receive a refund for order ORD-5001 because the item arrived damaged."
# or by IDs:
uv run python scripts/submit_refund.py CUST-1001 ORD-5001 "Item arrived damaged"
```

The script sends a Pub/Sub-shaped request to
`POST /apps/refund_agent/trigger/pubsub`. The eligibility result appears as
a JSON audit line in Terminal 1.

## Demo Walkthrough

1. **Eligible → approve.** Submit the ORD-5001 request above and open
   <http://localhost:8081/approval>. The card shows the customer, order, items,
   refund amount, reason, the rules-engine eligibility result and the AI
   summary. Click **Approve**. The card shows
   `SIMULATED refund SIM-RF-… of $129.99 …`.
2. **Duplicate.** Submit ORD-5001 again. Terminal 1 logs
   `ineligible … already refunded - duplicate request blocked.` No card
   appears.
3. **Ineligible.** Run
   `uv run python scripts/submit_refund.py CUST-1001 ORD-5002 "Stopped working"`.
   It's rejected for being outside the 30-day window, with no approval card.
   Try ORD-5004 (not delivered), CUST-1002/ORD-5005 (over the limit) or
   CUST-1003/ORD-5006 (suspended account).
4. **Cross-customer access.** Run
   `uv run python scripts/submit_refund.py CUST-1001 ORD-5003 "Broken"`.
   The result is `Order ORD-5003 not found for customer CUST-1001` plus a
   `cross_customer_access_denied` audit line.
5. **Human rejection.** Run
   `uv run python scripts/submit_refund.py CUST-1002 ORD-5003 "Wrong size"`,
   then click **Reject**. No refund is issued and the reservation is released,
   so the order can be requested again.

**ADK web playground (optional).** Run `uv run adk web --port 8501`, open
<http://localhost:8501>, choose `refund_agent` and type the sample sentence.
The playground shows each node's output and pauses at `adk_request_input`.
It runs in its own process, so its sessions don't appear in the approval UI
on port 8081.

## Tests

```powershell
uv run python -m pytest tests/ -q
```

The tests use a mocked LLM and need no API key.

- `tests/test_refunds.py`: lookups, every eligibility rule, invalid inputs,
  cross-customer access, the duplicate guard, simulated-refund idempotency,
  and input and decision parsing.
- `tests/test_integration.py`: the full HTTP flow through the trigger, the
  approval UI and `POST /run`. It covers approve, reject, ineligible requests
  never reaching approval, duplicates, LLM failure and malformed input.
- `tests/test_runnability.py`: checks that `agent.py` imports cleanly and
  defines the expected workflow objects.

## Limitations

- **In-memory state.** The ledger and audit log live in the backend process
  and reset on restart. Production would use a database with a unique
  constraint on the order ID.
- **The approval UI has no authentication.** Put it behind SSO (for
  example Google IAP) before using it anywhere but localhost.
- **Mock data only.** Swap the dicts in `refunds.py` for real CRM and order
  APIs. The tool signatures stay the same.

## License

Licensed under the [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0).
See [NOTICE](NOTICE) for attribution to the original Google recipe.

## Disclaimer

This is a demonstration project. It uses mock data and only simulates
refunds; it is not intended for production use without proper persistence,
authentication, and security review.
