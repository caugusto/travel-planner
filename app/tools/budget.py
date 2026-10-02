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

"""Deterministic budget tools.

LLMs are bad at arithmetic and at being consistent about prices. These tools
keep money math out of the model: estimates come from a transparent cost
model and every itinerary is audited by code before it reaches the traveler.
"""

from __future__ import annotations

from typing import Literal

from google.adk.tools import ToolContext
from pydantic import BaseModel, Field

TravelStyle = Literal["budget", "moderate", "luxury"]
Category = Literal[
    "lodging", "food", "activities", "local_transport", "flights", "other"
]

# Daily per-person USD cost bands by destination cost tier and travel style.
# Lodging assumes double occupancy (so it is per person).
_DAILY_COSTS: dict[int, dict[str, dict[str, float]]] = {
    1: {  # low-cost destinations (e.g. VN, IN, MX, EG, ID)
        "budget": {"lodging": 15, "food": 12, "activities": 8, "local_transport": 4},
        "moderate": {
            "lodging": 45,
            "food": 25,
            "activities": 20,
            "local_transport": 10,
        },
        "luxury": {"lodging": 150, "food": 70, "activities": 60, "local_transport": 35},
    },
    2: {  # mid-cost (e.g. PT, ES, GR, BR, TR, TH)
        "budget": {"lodging": 30, "food": 25, "activities": 15, "local_transport": 8},
        "moderate": {
            "lodging": 80,
            "food": 50,
            "activities": 35,
            "local_transport": 15,
        },
        "luxury": {
            "lodging": 250,
            "food": 120,
            "activities": 100,
            "local_transport": 50,
        },
    },
    3: {  # high-cost (e.g. US, JP, FR, UK, AU)
        "budget": {"lodging": 55, "food": 40, "activities": 25, "local_transport": 12},
        "moderate": {
            "lodging": 140,
            "food": 80,
            "activities": 50,
            "local_transport": 25,
        },
        "luxury": {
            "lodging": 400,
            "food": 180,
            "activities": 150,
            "local_transport": 80,
        },
    },
    4: {  # very high-cost (e.g. CH, NO, IS, SG, MC)
        "budget": {"lodging": 80, "food": 60, "activities": 35, "local_transport": 18},
        "moderate": {
            "lodging": 200,
            "food": 110,
            "activities": 70,
            "local_transport": 35,
        },
        "luxury": {
            "lodging": 550,
            "food": 250,
            "activities": 200,
            "local_transport": 100,
        },
    },
}
_TIER_BY_COUNTRY = {
    **dict.fromkeys(["VN", "IN", "MX", "EG", "ID", "KH", "LA", "NP", "BO", "CO", "PE", "MA", "LK", "PH"], 1),
    **dict.fromkeys(["PT", "ES", "GR", "BR", "TR", "TH", "AR", "CL", "HR", "CZ", "HU", "PL", "ZA", "MY", "CR"], 2),
    **dict.fromkeys(["US", "JP", "FR", "GB", "AU", "IT", "DE", "NL", "CA", "NZ", "AT", "BE", "IE", "KR", "AE"], 3),
    **dict.fromkeys(["CH", "NO", "IS", "SG", "MC", "DK", "LU", "BS"], 4),
}  # fmt: skip


def estimate_trip_budget(
    country_code: str,
    num_days: int,
    travelers: int,
    travel_style: TravelStyle,
    tool_context: ToolContext,
) -> dict:
    """Estimates on-the-ground trip costs (excluding flights) in USD.

    Use this BEFORE drafting an itinerary so the plan is grounded in a
    realistic per-day spend for the destination and the traveler's style.

    Args:
        country_code: ISO-3166 alpha-2 code of the destination, e.g. "PT".
        num_days: Trip length in days (1-30).
        travelers: Number of travelers (1-12).
        travel_style: One of "budget", "moderate" or "luxury".

    Returns:
        dict with status, cost_tier, per_person_daily breakdown, and
        total_usd for the whole party and trip.
    """
    if not 1 <= int(num_days) <= 30:
        return {"status": "error", "error_message": "num_days must be 1-30."}
    if not 1 <= int(travelers) <= 12:
        return {"status": "error", "error_message": "travelers must be 1-12."}
    if travel_style not in ("budget", "moderate", "luxury"):
        return {
            "status": "error",
            "error_message": "travel_style must be budget, moderate or luxury.",
        }

    tier = _TIER_BY_COUNTRY.get((country_code or "").upper(), 2)
    daily = _DAILY_COSTS[tier][travel_style]
    per_person_day = sum(daily.values())
    total = per_person_day * int(num_days) * int(travelers)
    estimate = {
        "status": "success",
        "cost_tier": tier,
        "travel_style": travel_style,
        "per_person_daily_usd": daily,
        "per_person_daily_total_usd": per_person_day,
        "total_usd": round(total, 2),
        "excludes": ["flights", "travel insurance", "visas"],
    }
    tool_context.state["budget_estimate"] = estimate
    return estimate


class CostItem(BaseModel):
    """One priced line of an itinerary."""

    day: int = Field(description="Trip day number (1-based); 0 for whole-trip items.")
    category: Category = Field(description="Cost category.")
    description: str = Field(description="What the cost is for.")
    amount_usd: float = Field(ge=0, description="Cost for the whole party in USD.")


def validate_itinerary_budget(
    cost_items: list[CostItem], budget_usd: float, tool_context: ToolContext
) -> dict:
    """Audits an itinerary's priced line items against the traveler's budget.

    Always call this with EVERY priced item from the draft itinerary. The
    result is the single source of truth on whether the plan is affordable.

    Args:
        cost_items: All priced line items in the itinerary.
        budget_usd: The traveler's total budget in USD for the party.

    Returns:
        dict with status, total_usd, budget_usd, remaining_usd,
        within_budget (bool), utilisation_pct, per-category totals and the
        three most expensive items (useful for targeted cuts).
    """
    if budget_usd is None or float(budget_usd) <= 0:
        return {"status": "error", "error_message": "budget_usd must be positive."}
    if not cost_items:
        return {
            "status": "error",
            "error_message": "No cost items supplied; price the itinerary first.",
        }

    items = [
        CostItem.model_validate(i) if isinstance(i, dict) else i for i in cost_items
    ]
    by_cat: dict[str, float] = {}
    for it in items:
        by_cat[it.category] = round(by_cat.get(it.category, 0.0) + it.amount_usd, 2)
    total = round(sum(it.amount_usd for it in items), 2)
    remaining = round(float(budget_usd) - total, 2)
    top = sorted(items, key=lambda i: i.amount_usd, reverse=True)[:3]
    result = {
        "status": "success",
        "total_usd": total,
        "budget_usd": float(budget_usd),
        "remaining_usd": remaining,
        "within_budget": remaining >= 0,
        "utilisation_pct": round(100 * total / float(budget_usd), 1),
        "by_category_usd": by_cat,
        "most_expensive": [i.model_dump() for i in top],
    }
    tool_context.state["budget_check"] = result
    return result
