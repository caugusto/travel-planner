# ruff: noqa
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

"""Wanderwise - a multi-agent travel planner built with Google ADK.

Agent graph
-----------
travel_planner (LlmAgent, concierge/router)
 |  tools: profile memory, quick-answer tools, Memory Bank preload,
 |         request_booking_hold (human-in-the-loop approval)
 └─ trip_planning_pipeline (SequentialAgent)
     1. trip_request_extractor   LlmAgent -> structured TripRequest (output_schema)
     2. research_team            ParallelAgent (fan-out, ~3x faster)
          ├─ destination_scout     geocode + weather + holidays
          ├─ local_insights_scout  Google Search grounding
          └─ budget_analyst        deterministic cost model + FX
     3. itinerary_drafter        LlmAgent -> itinerary_draft
     4. budget_review_loop       LoopAgent (max 3) - critic/refiner pattern
          ├─ budget_auditor        code-based audit; exit_loop when approved
          └─ itinerary_refiner     rewrites draft from auditor feedback
     5. itinerary_presenter      policy-gated save_trip_plan (+ HITL override
                                 when over budget) + final answer

Cross-cutting: ModelRouterPlugin (LITE/FLASH/PRO routing + failover),
SafetyGuardrailPlugin, TravelTelemetryPlugin, BigQuery analytics.
"""

import datetime
import logging
import os
from typing import Literal

import google.auth
from google.adk.agents import Agent, LoopAgent, ParallelAgent, SequentialAgent
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.apps import App
from google.adk.apps.app import EventsCompactionConfig
from google.adk.plugins.bigquery_agent_analytics_plugin import (
    BigQueryAgentAnalyticsPlugin,
    BigQueryLoggerConfig,
)
from google.adk.tools.exit_loop_tool import exit_loop
from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.google_search_tool import google_search
from google.adk.tools.load_memory_tool import load_memory_tool
from google.adk.tools.preload_memory_tool import PreloadMemoryTool
from google.cloud import bigquery
from google.genai import types
from pydantic import BaseModel, Field

from app import prompts
from app.guardrails import (
    SafetyGuardrailPlugin,
    persist_to_memory_bank,
    require_budget_approval,
)
from app.observability import TravelTelemetryPlugin
from app.routing import ModelRouterPlugin, model_for
from app.tools import (
    convert_currency,
    estimate_trip_budget,
    geocode_destination,
    get_public_holidays,
    get_traveler_profile,
    get_weather_forecast,
    save_traveler_preference,
    save_trip_plan,
    validate_itinerary_budget,
)
from app.tools.booking import needs_budget_override, request_booking_hold
from app.tools.profile import PROFILE_KEY, TRIPS_KEY

# Gemini 3 models are served from the global endpoint; resolve project from
# ADC when not provided so the agent runs anywhere (local, CI, Agent Runtime).
_, _adc_project = google.auth.default()
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", _adc_project or "")
os.environ["GOOGLE_CLOUD_LOCATION"] = "global"
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")

# Model selection is strategic, not hard-coded: each agent gets a baseline
# tier (LITE / FLASH / PRO) via model_for(), and ModelRouterPlugin escalates or
# de-escalates per request. See app/routing.py.


def _gen_config(temperature: float) -> types.GenerateContentConfig:
    return types.GenerateContentConfig(temperature=temperature)


# --------------------------------------------------------------------------
# Structured contract between the concierge and the planning pipeline.
# --------------------------------------------------------------------------
class TripRequest(BaseModel):
    """Normalised trip requirements shared by every pipeline specialist."""

    destination: str = Field(description="City or region, e.g. 'Lisbon, Portugal'.")
    start_date: str = Field(description="ISO date YYYY-MM-DD.")
    num_days: int = Field(ge=1, le=14, description="Trip length in days.")
    travelers: int = Field(ge=1, le=12, description="Number of travelers.")
    budget_usd: float = Field(ge=1, description="Total budget for the party in USD.")
    travel_style: Literal["budget", "moderate", "luxury"] = "moderate"
    home_currency: str = Field(default="USD", description="ISO-4217 code.")
    interests: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(
        default_factory=list, description="Diet, accessibility, pace, dislikes, notes."
    )


# --------------------------------------------------------------------------
# Dynamic instructions (InstructionProvider) - inject date + durable memory.
# --------------------------------------------------------------------------
def _today() -> str:
    return datetime.date.today().strftime("%A %Y-%m-%d")


def concierge_instruction(ctx: ReadonlyContext) -> str:
    return prompts.CONCIERGE.format(
        today=_today(),
        profile=ctx.state.get(PROFILE_KEY) or "none yet (new traveler)",
        saved_trips=[
            f"{t['title']} ({t['start_date']})" for t in ctx.state.get(TRIPS_KEY) or []
        ]
        or "none",
    )


def extractor_instruction(ctx: ReadonlyContext) -> str:
    return prompts.REQUEST_EXTRACTOR.format(
        today=_today(), profile=ctx.state.get(PROFILE_KEY) or "none"
    )


# --------------------------------------------------------------------------
# Pipeline specialists
# --------------------------------------------------------------------------
trip_request_extractor = Agent(
    name="trip_request_extractor",
    model=model_for("trip_request_extractor"),
    description="Turns the conversation into a structured TripRequest.",
    instruction=extractor_instruction,
    output_schema=TripRequest,
    output_key="trip_request",
    generate_content_config=_gen_config(0.0),
)

destination_scout = Agent(
    name="destination_scout",
    model=model_for("destination_scout"),
    description="Location facts, weather outlook and public holidays.",
    instruction=prompts.DESTINATION_SCOUT,
    tools=[geocode_destination, get_weather_forecast, get_public_holidays],
    include_contents="none",
    output_key="destination_brief",
    generate_content_config=_gen_config(0.2),
)

local_insights_scout = Agent(
    name="local_insights_scout",
    model=model_for("local_insights_scout"),
    description="Current attractions, neighbourhoods and food via Google Search.",
    instruction=prompts.LOCAL_INSIGHTS_SCOUT,
    tools=[google_search],
    include_contents="none",
    output_key="local_insights",
)

budget_analyst = Agent(
    name="budget_analyst",
    model=model_for("budget_analyst"),
    description="Realistic cost envelope from a deterministic cost model.",
    instruction=prompts.BUDGET_ANALYST,
    tools=[geocode_destination, estimate_trip_budget, convert_currency],
    include_contents="none",
    output_key="budget_brief",
    generate_content_config=_gen_config(0.1),
)

research_team = ParallelAgent(
    name="research_team",
    description="Runs destination, local-insight and budget research concurrently.",
    sub_agents=[destination_scout, local_insights_scout, budget_analyst],
)

itinerary_drafter = Agent(
    name="itinerary_drafter",
    model=model_for("itinerary_drafter"),
    description="Writes the day-by-day itinerary with a priced cost table.",
    instruction=prompts.ITINERARY_DRAFTER,
    include_contents="none",
    output_key="itinerary_draft",
    generate_content_config=_gen_config(0.7),
)

budget_auditor = Agent(
    name="budget_auditor",
    model=model_for("budget_auditor"),
    description="Audits the draft's costs in code; exits the loop when approved.",
    instruction=prompts.BUDGET_AUDITOR,
    tools=[validate_itinerary_budget, exit_loop],
    include_contents="none",
    output_key="budget_feedback",
    generate_content_config=_gen_config(0.0),
)

itinerary_refiner = Agent(
    name="itinerary_refiner",
    model=model_for("itinerary_refiner"),
    description="Revises the itinerary to address budget feedback.",
    instruction=prompts.ITINERARY_REFINER,
    include_contents="none",
    output_key="itinerary_draft",
    generate_content_config=_gen_config(0.4),
)

budget_review_loop = LoopAgent(
    name="budget_review_loop",
    description="Critic/refiner loop until the itinerary fits the budget.",
    sub_agents=[budget_auditor, itinerary_refiner],
    max_iterations=3,
)

itinerary_presenter = Agent(
    name="itinerary_presenter",
    model=model_for("itinerary_presenter"),
    description="Presents and saves the approved itinerary.",
    instruction=prompts.ITINERARY_PRESENTER,
    # HITL: saving an itinerary that FAILED the budget audit pauses for an
    # explicit human override (ADK tool confirmation).
    tools=[FunctionTool(save_trip_plan, require_confirmation=needs_budget_override)],
    include_contents="none",
    before_tool_callback=require_budget_approval,
    generate_content_config=_gen_config(0.3),
)

trip_planning_pipeline = SequentialAgent(
    name="trip_planning_pipeline",
    description=(
        "End-to-end trip planner. Use once destination, dates, trip length, "
        "travelers and budget are known. Researches, drafts, budget-audits "
        "and saves a complete itinerary."
    ),
    sub_agents=[
        trip_request_extractor,
        research_team,
        itinerary_drafter,
        budget_review_loop,
        itinerary_presenter,
    ],
)

# --------------------------------------------------------------------------
# Root concierge
# --------------------------------------------------------------------------
root_agent = Agent(
    # Keep in sync with agents-cli-manifest.yaml: agents-cli derives this name
    # from the project `name:` recorded there, and telemetry reports it as
    # gen_ai.agent.name. Renaming the agent only here makes the two disagree,
    # and anything selecting traces by name stops finding this agent's.
    name="travel_planner",
    model=model_for("travel_planner"),
    description="Wanderwise travel concierge: remembers travelers and plans trips.",
    instruction=concierge_instruction,
    tools=[
        PreloadMemoryTool(),
        load_memory_tool,
        get_traveler_profile,
        save_traveler_preference,
        geocode_destination,
        get_weather_forecast,
        convert_currency,
        # HITL: always pauses for human approval (editable spending cap).
        request_booking_hold,
    ],
    sub_agents=[trip_planning_pipeline],
    after_agent_callback=persist_to_memory_bank,
)

# --------------------------------------------------------------------------
# Plugins: guardrails -> model routing -> telemetry -> BigQuery analytics
# --------------------------------------------------------------------------
_plugins = [SafetyGuardrailPlugin(), ModelRouterPlugin(), TravelTelemetryPlugin()]
_project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
_dataset_id = os.environ.get("BQ_ANALYTICS_DATASET_ID", "adk_agent_analytics")
# BigQuery needs a real data location (GOOGLE_CLOUD_LOCATION is "global").
_bq_location = os.environ.get("BQ_ANALYTICS_LOCATION", "us-central1")

if _project_id and os.environ.get("DISABLE_BQ_ANALYTICS", "").lower() not in (
    "1",
    "true",
):
    try:
        bq = bigquery.Client(project=_project_id)
        bq.create_dataset(f"{_project_id}.{_dataset_id}", exists_ok=True)

        _plugins.append(
            BigQueryAgentAnalyticsPlugin(
                project_id=_project_id,
                dataset_id=_dataset_id,
                location=_bq_location,
                config=BigQueryLoggerConfig(
                    gcs_bucket_name=os.environ.get("BQ_ANALYTICS_GCS_BUCKET"),
                    connection_id=os.environ.get("BQ_ANALYTICS_CONNECTION_ID"),
                ),
            )
        )
    except Exception as e:
        logging.warning(f"Failed to initialize BigQuery Analytics: {e}")

app = App(
    root_agent=root_agent,
    name="app",
    plugins=_plugins,
    # Context management: summarise older turns so long planning sessions
    # stay within budget, and cache the large static prefix.
    events_compaction_config=EventsCompactionConfig(
        compaction_interval=6,
        overlap_size=1,
    ),
    context_cache_config=ContextCacheConfig(
        min_tokens=4096,
        ttl_seconds=900,
        cache_intervals=10,
    ),
)
