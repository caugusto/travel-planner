# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Human-in-the-loop (HITL) checkpoints for high-stakes actions.

Two actions in Wanderwise have real-world consequences and therefore pause
the agent until a human explicitly approves them, using ADK's tool
confirmation protocol (the agent emits an ``adk_request_confirmation``
function call; the client answers with a ``ToolConfirmation`` - approve,
reject, or approve-with-edits):

1. ``request_booking_hold`` - commits money and shares the traveler's
   contact details with a third party (the travel desk). It ALWAYS requires
   approval, and the approver can *edit* the spending cap in the payload.
2. ``save_trip_plan`` when the itinerary FAILED the budget audit - saving an
   over-budget plan needs an explicit human override
   (``needs_budget_override`` below is wired as ``require_confirmation``).

Every request / approval / rejection is written to the structured audit
log so there is a complete record of who approved what.

Note: the travel-desk hand-off is a queue of requests stored in user-scoped
state and the audit log; no live booking API is called.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import uuid
from typing import Any

from google.adk.tools import ToolContext

from app.observability import log_event

BOOKINGS_KEY = "user:booking_requests"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


def _mask_email(email: str) -> str:
    user, _, domain = email.partition("@")
    return f"{user[:1]}***@{domain}"


def request_booking_hold(
    trip_artifact: str,
    contact_email: str,
    max_total_usd: float,
    tool_context: ToolContext,
) -> dict:
    """Sends a saved itinerary to the human travel desk to place booking holds.

    HIGH-STAKES: this commits up to ``max_total_usd`` of the traveler's money
    and shares their email with the travel desk, so it pauses for explicit
    human approval before anything is submitted. Only call it when the
    traveler explicitly asks to book / reserve / hold a saved trip.

    Args:
        trip_artifact: Artifact filename of a saved trip (from
            get_traveler_profile -> saved_trips[].artifact).
        contact_email: Email the travel desk should use to contact the traveler.
        max_total_usd: Maximum total spend the traveler authorises, in USD.

    Returns:
        dict with status "pending_human_approval", "submitted",
        "rejected_by_human" or "error".
    """
    trips = {
        t.get("artifact"): t for t in tool_context.state.get("user:saved_trips") or []
    }
    trip = trips.get(trip_artifact)
    if not trip:
        return {
            "status": "error",
            "error_message": "Unknown trip_artifact. Use get_traveler_profile to list saved trips.",
        }
    if not _EMAIL_RE.match(contact_email or ""):
        return {
            "status": "error",
            "error_message": "contact_email is not a valid email address.",
        }
    if max_total_usd is None or float(max_total_usd) <= 0:
        return {"status": "error", "error_message": "max_total_usd must be positive."}

    confirmation = tool_context.tool_confirmation
    if confirmation is None:
        # 1st pass: pause and ask a human. The payload is editable by the approver.
        tool_context.request_confirmation(
            hint=(
                f"Approve booking hold for '{trip['title']}' "
                f"({trip['start_date']}, {trip['num_days']} days)? "
                f"Audited cost ${trip['total_cost_usd']:.2f}; spending cap "
                f"${float(max_total_usd):.2f}; contact {_mask_email(contact_email)}. "
                "Reply confirmed=true to approve (you may edit max_total_usd in the "
                "payload) or confirmed=false to reject."
            ),
            payload={"max_total_usd": float(max_total_usd)},
        )
        log_event(
            "hitl_requested",
            action="request_booking_hold",
            trip=trip_artifact,
            max_total_usd=float(max_total_usd),
        )
        return {
            "status": "pending_human_approval",
            "message": "Waiting for the traveler to approve the booking hold.",
        }

    if not confirmation.confirmed:
        log_event(
            "hitl_rejected",
            logging.WARNING,
            action="request_booking_hold",
            trip=trip_artifact,
        )
        return {
            "status": "rejected_by_human",
            "message": "The traveler declined. Nothing was submitted.",
        }

    approved_cap = float(
        (confirmation.payload or {}).get("max_total_usd", max_total_usd)
    )
    if approved_cap < float(trip["total_cost_usd"]):
        log_event(
            "hitl_rejected",
            logging.WARNING,
            action="request_booking_hold",
            reason="cap_below_cost",
        )
        return {
            "status": "error",
            "error_message": (
                f"Approved cap ${approved_cap:.2f} is below the audited trip cost "
                f"${trip['total_cost_usd']:.2f}. Ask the traveler to raise the cap or trim the plan."
            ),
        }

    reference = f"WW-{uuid.uuid4().hex[:8].upper()}"
    record = {
        "reference": reference,
        "trip_artifact": trip_artifact,
        "title": trip["title"],
        "approved_cap_usd": approved_cap,
        "contact": _mask_email(contact_email),
        "booking_status": "submitted_to_travel_desk",
        "approved_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    }
    bookings = list(tool_context.state.get(BOOKINGS_KEY) or [])
    bookings.append(record)
    tool_context.state[BOOKINGS_KEY] = bookings[-20:]
    log_event(
        "hitl_approved",
        action="request_booking_hold",
        reference=reference,
        approved_cap_usd=approved_cap,
        cap_edited=approved_cap != float(max_total_usd),
    )
    return {"status": "submitted", **record}


def needs_budget_override(tool_context: ToolContext, **_tool_args: Any) -> bool:
    """``require_confirmation`` predicate for save_trip_plan.

    Returns True (pause for a human) when the latest budget audit failed, so
    an over-budget itinerary is only persisted after explicit approval.
    """
    check = tool_context.state.get("budget_check") or {}
    needs = bool(check) and not check.get("within_budget", False)
    if needs:
        log_event(
            "hitl_requested",
            action="save_trip_plan",
            reason="over_budget",
            remaining_usd=check.get("remaining_usd"),
        )
    return needs
