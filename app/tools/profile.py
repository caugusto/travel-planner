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

"""Traveler-profile and saved-trip tools (the agent's structured memory).

Two complementary memory layers are used by the agent:

1. **Structured profile** (this module) - explicit preferences stored in
   ``user:``-scoped session state. The ``user:`` prefix makes ADK persist the
   value per user across every session, so a returning traveler's diet,
   home currency, pace, etc. are injected into prompts automatically.
2. **Semantic long-term memory** (Vertex AI Memory Bank) - free-form facts
   extracted from past conversations, retrieved with ``PreloadMemoryTool``.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Literal

from google.adk.tools import ToolContext
from google.genai import types

PROFILE_KEY = "user:traveler_profile"
TRIPS_KEY = "user:saved_trips"
MAX_SAVED_TRIPS = 20

PreferenceCategory = Literal[
    "home_city",
    "home_currency",
    "travel_style",
    "pace",
    "dietary",
    "accessibility",
    "interests",
    "dislikes",
    "accommodation",
    "travel_companions",
]


def save_traveler_preference(
    category: PreferenceCategory, value: str, tool_context: ToolContext
) -> dict:
    """Remembers a durable traveler preference for all future trips.

    Call this whenever the traveler states a lasting preference (e.g.
    "I'm vegetarian", "I prefer a slow pace", "my home currency is CAD").
    Do NOT store one-off trip details like dates or a single destination.

    Args:
        category: Preference category.
        value: Short preference value, e.g. "vegetarian" or "slow".

    Returns:
        dict with status and the full updated profile.
    """
    clean = re.sub(r"\s+", " ", (value or "")).strip()[:200]
    if not clean:
        return {"status": "error", "error_message": "Preference value is empty."}
    profile = dict(tool_context.state.get(PROFILE_KEY) or {})
    if category in ("interests", "dislikes", "dietary", "accessibility"):
        existing = set(profile.get(category, []))
        existing.update(
            v.strip().lower() for v in re.split(r",|;|\band\b", clean) if v.strip()
        )
        profile[category] = sorted(existing)
    else:
        profile[category] = clean
    profile["updated_at"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    tool_context.state[PROFILE_KEY] = profile
    return {"status": "success", "profile": profile}


def get_traveler_profile(tool_context: ToolContext) -> dict:
    """Returns the traveler's stored preferences and saved-trip summaries.

    Returns:
        dict with status, profile (may be empty for new travelers) and
        saved_trips (title/destination/dates/cost for each saved plan).
    """
    return {
        "status": "success",
        "profile": tool_context.state.get(PROFILE_KEY) or {},
        "saved_trips": tool_context.state.get(TRIPS_KEY) or [],
    }


async def save_trip_plan(
    title: str,
    destination: str,
    start_date: str,
    num_days: int,
    total_cost_usd: float,
    itinerary_markdown: str,
    tool_context: ToolContext,
) -> dict:
    """Saves the final itinerary as a downloadable Markdown artifact.

    Call exactly once, after the itinerary has passed the budget audit.

    Args:
        title: Short trip title, e.g. "5 Foodie Days in Lisbon".
        destination: Destination name.
        start_date: Trip start date YYYY-MM-DD.
        num_days: Trip length in days.
        total_cost_usd: Audited total cost in USD.
        itinerary_markdown: The complete, final itinerary in Markdown.

    Returns:
        dict with status, artifact filename and version.
    """
    if len(itinerary_markdown or "") < 50:
        return {"status": "error", "error_message": "Itinerary is too short to save."}
    slug = re.sub(r"[^a-z0-9]+", "-", f"{destination}-{start_date}".lower()).strip("-")
    filename = f"itinerary-{slug}.md"
    try:
        version = await tool_context.save_artifact(
            filename,
            types.Part.from_bytes(
                data=itinerary_markdown.encode("utf-8"), mime_type="text/markdown"
            ),
        )
    except Exception as exc:
        return {"status": "error", "error_message": f"Could not save artifact: {exc}"}

    trips = list(tool_context.state.get(TRIPS_KEY) or [])
    trips = [t for t in trips if t.get("artifact") != filename]
    trips.append(
        {
            "title": title,
            "destination": destination,
            "start_date": start_date,
            "num_days": int(num_days),
            "total_cost_usd": round(float(total_cost_usd), 2),
            "artifact": filename,
            "saved_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        }
    )
    tool_context.state[TRIPS_KEY] = trips[-MAX_SAVED_TRIPS:]
    return {"status": "success", "artifact": filename, "version": version}
