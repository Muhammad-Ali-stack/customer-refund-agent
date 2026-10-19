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

"""Send a refund request to the local backend's Pub/Sub trigger endpoint.

Usage:
    uv run python scripts/submit_refund.py "Check whether customer CUST-1001
        can receive a refund for order ORD-5001 because the item arrived
        damaged."
    uv run python scripts/submit_refund.py CUST-1001 ORD-5001 "Arrived damaged"

Uses only the standard library. Works the same on Windows, macOS, Linux.
"""

import base64
import json
import os
import sys
import urllib.request

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8080")
APP_NAME = os.environ.get("APP_NAME", "refund_agent")
SUBSCRIPTION = os.environ.get("PUBSUB_SUBSCRIPTION", "test-sub")


def main(argv: list[str]) -> int:
    if len(argv) == 1:
        data: bytes = argv[0].encode()
    elif len(argv) == 3:
        data = json.dumps(
            {"customer_id": argv[0], "order_id": argv[1], "reason": argv[2]}
        ).encode()
    else:
        print(__doc__)
        return 2

    body = json.dumps(
        {
            "message": {
                "data": base64.b64encode(data).decode(),
                "attributes": {"source": "submit_refund.py"},
            },
            "subscription": SUBSCRIPTION,
        }
    ).encode()
    request = urllib.request.Request(  # noqa: S310 -- http(s) BACKEND_URL
        f"{BACKEND_URL}/apps/{APP_NAME}/trigger/pubsub",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as resp:  # noqa: S310
        print(resp.status, resp.read().decode())
    print(
        "Check the backend terminal for the eligibility result, and"
        " http://localhost:8081/approval for pending approvals."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
