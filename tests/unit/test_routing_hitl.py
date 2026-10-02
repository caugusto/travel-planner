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

"""Unit tests for model routing and human-in-the-loop checkpoints."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from google.adk.models.llm_request import LlmRequest
from google.genai import types

from app import routing
from app.routing import Tier, decide
from app.tools import booking


# ------------------------------------------------------------------ routing
def _req(text: str = "") -> LlmRequest:
    return LlmRequest(
        contents=[types.Content(role="user", parts=[types.Part.from_text(text=text)])]
    )


def test_static_tiers_match_task_complexity() -> None:
    assert routing.AGENT_TIERS["trip_request_extractor"] == Tier.LITE
    assert routing.AGENT_TIERS["destination_scout"] == Tier.LITE
    assert routing.AGENT_TIERS["travel_planner"] == Tier.FLASH
    assert routing.AGENT_TIERS["itinerary_drafter"] == Tier.PRO
    # Three distinct models are actually used.
    assert len(set(routing.MODELS.values())) == 3


def test_model_for_uses_tier_model() -> None:
    assert routing.model_for("itinerary_drafter").model == routing.MODELS[Tier.PRO]
    assert routing.model_for("destination_scout").model == routing.MODELS[Tier.LITE]
    assert routing.model_for("unknown_agent").model == routing.MODELS[Tier.FLASH]


def test_refiner_escalates_to_pro_when_far_over_budget() -> None:
    state = {"budget_check": {"remaining_usd": -400, "budget_usd": 1000}}
    d = decide("itinerary_refiner", state, _req())
    assert d.tier == Tier.PRO and d.tier > d.baseline
    small = {"budget_check": {"remaining_usd": -50, "budget_usd": 1000}}
    assert decide("itinerary_refiner", small, _req()).tier == Tier.FLASH


def test_complex_trip_escalates_auditor() -> None:
    state = {
        "trip_request": {
            "num_days": 10,
            "travelers": 6,
            "budget_usd": 5000,
            "constraints": ["vegan", "wheelchair", "slow pace"],
        },
        "budget_estimate": {"total_usd": 4900},
    }
    score, reasons = routing.trip_complexity(state)
    assert score == 4 and "tight_budget" in reasons
    assert decide("budget_auditor", state, _req()).tier == Tier.PRO


def test_simple_short_trip_deescalates_drafter() -> None:
    state = {"trip_request": {"num_days": 2, "travelers": 1, "budget_usd": 600}}
    d = decide("itinerary_drafter", state, _req())
    assert d.tier == Tier.FLASH and "simple_short_trip" in d.reasons


def test_small_talk_routes_concierge_to_lite() -> None:
    assert decide("travel_planner", {}, _req("Thanks!")).tier == Tier.LITE
    assert decide("travel_planner", {}, _req("Plan 3 days in Rome")).tier == Tier.FLASH


# --------------------------------------------------------------------- HITL
class HitlContext:
    """Fake ToolContext supporting ADK's confirmation protocol."""

    def __init__(self, state: dict[str, Any], confirmation: Any = None) -> None:
        self.state = state
        self.tool_confirmation = confirmation
        self.requested: dict[str, Any] | None = None

    def request_confirmation(
        self, *, hint: str | None = None, payload: Any = None
    ) -> None:
        self.requested = {"hint": hint, "payload": payload}


_TRIP = {
    "title": "Lisbon",
    "artifact": "itinerary-lisbon.md",
    "start_date": "2026-10-20",
    "num_days": 3,
    "total_cost_usd": 950.0,
}


def _state() -> dict[str, Any]:
    return {"user:saved_trips": [dict(_TRIP)]}


def test_booking_first_call_pauses_for_human() -> None:
    ctx = HitlContext(_state())
    out = booking.request_booking_hold(
        "itinerary-lisbon.md", "ana@example.com", 1200, ctx
    )
    assert out["status"] == "pending_human_approval"
    assert ctx.requested and ctx.requested["payload"] == {"max_total_usd": 1200.0}
    assert "a***@example.com" in ctx.requested["hint"]  # email masked in hint
    assert booking.BOOKINGS_KEY not in ctx.state  # nothing submitted yet


def test_booking_rejected_by_human_submits_nothing() -> None:
    ctx = HitlContext(_state(), SimpleNamespace(confirmed=False, payload=None))
    out = booking.request_booking_hold(
        "itinerary-lisbon.md", "ana@example.com", 1200, ctx
    )
    assert out["status"] == "rejected_by_human"
    assert booking.BOOKINGS_KEY not in ctx.state


def test_booking_approved_with_edited_cap() -> None:
    ctx = HitlContext(
        _state(), SimpleNamespace(confirmed=True, payload={"max_total_usd": 1000})
    )
    out = booking.request_booking_hold(
        "itinerary-lisbon.md", "ana@example.com", 1200, ctx
    )
    assert out["status"] == "submitted"
    assert out["approved_cap_usd"] == 1000.0
    assert out["reference"].startswith("WW-")
    assert ctx.state[booking.BOOKINGS_KEY][0]["contact"] == "a***@example.com"


def test_booking_cap_below_cost_is_refused() -> None:
    ctx = HitlContext(
        _state(), SimpleNamespace(confirmed=True, payload={"max_total_usd": 500})
    )
    out = booking.request_booking_hold(
        "itinerary-lisbon.md", "ana@example.com", 1200, ctx
    )
    assert out["status"] == "error"
    assert booking.BOOKINGS_KEY not in ctx.state


def test_booking_validates_inputs() -> None:
    ctx = HitlContext(_state())
    assert (
        booking.request_booking_hold("nope.md", "a@b.com", 10, ctx)["status"] == "error"
    )
    assert (
        booking.request_booking_hold("itinerary-lisbon.md", "bad", 10, ctx)["status"]
        == "error"
    )
    assert ctx.requested is None  # invalid calls never reach the human


def test_needs_budget_override_only_when_audit_failed() -> None:
    assert booking.needs_budget_override(HitlContext({})) is False
    ok = HitlContext({"budget_check": {"within_budget": True}})
    assert booking.needs_budget_override(ok) is False
    over = HitlContext(
        {"budget_check": {"within_budget": False, "remaining_usd": -120}}
    )
    assert booking.needs_budget_override(over) is True
    # ADK forwards every tool argument to the predicate as keywords.
    assert (
        booking.needs_budget_override(
            num_days=2, destination="Porto", total_cost_usd=344, tool_context=over
        )
        is True
    )
