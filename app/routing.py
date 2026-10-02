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

"""Strategic model routing.

Not every step deserves the same model. Wanderwise routes each LLM call to
one of three tiers, balancing cost, latency and reasoning quality:

=========  ===========================  =====================================
Tier       Default model                Used for
=========  ===========================  =====================================
LITE       gemini-3.5-flash-lite        Structured extraction, tool-calling
                                        scouts, short summaries (cheap, fast)
FLASH      gemini-3.8-flash             Conversation, search grounding,
                                        auditing, presentation (balanced)
PRO        gemini-3.1-pro-preview       Multi-constraint itinerary design and
                                        hard budget refinements (deep reasoning)
=========  ===========================  =====================================

Routing happens in two layers:

1. **Static (by task)** - each agent is built with a *baseline tier* that
   fits its job (see ``AGENT_TIERS``).
2. **Dynamic (by request)** - ``ModelRouterPlugin.before_model_callback``
   scores the live request and can *escalate* (e.g. a 10-day, 6-traveler trip
   with diet + accessibility constraints, or a second budget-refinement pass)
   or *de-escalate* (e.g. a trivial greeting to the concierge). Every
   decision is logged and attached to the active trace span.

A third safety layer, ``on_model_error_callback``, fails over to the next
tier if a model is unavailable or rate-limited, so one model outage never
takes the agent down.

All model IDs are overridable via env vars (``MODEL_LITE``, ``MODEL_FLASH``,
``MODEL_PRO``) so ops can swap models without a code change.
"""

from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass, field

from google.adk.agents.callback_context import CallbackContext
from google.adk.models import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types
from opentelemetry import trace

from app.observability import log_event


class Tier(enum.IntEnum):
    LITE = 0
    FLASH = 1
    PRO = 2


MODELS: dict[Tier, str] = {
    Tier.LITE: os.environ.get("MODEL_LITE", "gemini-3.5-flash-lite"),
    Tier.FLASH: os.environ.get("MODEL_FLASH", "gemini-3.8-flash"),
    Tier.PRO: os.environ.get("MODEL_PRO", "gemini-3.1-pro-preview"),
}

# Baseline tier per agent (static routing by task type).
AGENT_TIERS: dict[str, Tier] = {
    "travel_planner": Tier.FLASH,  # conversation + routing decisions
    "trip_request_extractor": Tier.LITE,  # schema-constrained extraction
    "destination_scout": Tier.LITE,  # 3 deterministic tool calls + summary
    "budget_analyst": Tier.LITE,  # deterministic tools + summary
    "local_insights_scout": Tier.FLASH,  # search grounding quality matters
    "itinerary_drafter": Tier.PRO,  # hardest reasoning step
    "budget_auditor": Tier.FLASH,  # exhaustive, precise tool arguments
    "itinerary_refiner": Tier.FLASH,  # escalates to PRO on hard cases
    "itinerary_presenter": Tier.FLASH,  # long-form faithful rendering
}

# Fail-over order when a model call errors.
FALLBACK: dict[Tier, Tier] = {
    Tier.PRO: Tier.FLASH,
    Tier.FLASH: Tier.LITE,
    Tier.LITE: Tier.FLASH,
}

_GREETINGS = {"hi", "hello", "hey", "thanks", "thank you", "ok", "okay", "bye", "cool"}


def model_for(agent_name: str) -> Gemini:
    """Builds the baseline Gemini model for an agent (static routing)."""
    tier = AGENT_TIERS.get(agent_name, Tier.FLASH)
    return Gemini(model=MODELS[tier], retry_options=types.HttpRetryOptions(attempts=3))


@dataclass
class RoutingDecision:
    agent: str
    baseline: Tier
    tier: Tier
    reasons: list[str] = field(default_factory=list)

    @property
    def model(self) -> str:
        return MODELS[self.tier]


def trip_complexity(state: dict) -> tuple[int, list[str]]:
    """Scores how hard the current trip is to plan (0 = trivial)."""
    req = state.get("trip_request") or {}
    if not isinstance(req, dict):
        return 0, []
    score, reasons = 0, []
    if int(req.get("num_days", 0) or 0) >= 7:
        score += 1
        reasons.append("long_trip")
    if int(req.get("travelers", 0) or 0) >= 5:
        score += 1
        reasons.append("large_party")
    if len(req.get("constraints") or []) >= 3:
        score += 1
        reasons.append("many_constraints")
    estimate = (state.get("budget_estimate") or {}).get("total_usd")
    budget = req.get("budget_usd")
    if estimate and budget and float(estimate) > 0.9 * float(budget):
        score += 1
        reasons.append("tight_budget")
    return score, reasons


def _last_user_text(llm_request: LlmRequest) -> str:
    for content in reversed(llm_request.contents or []):
        if content.role == "user" and content.parts:
            text = " ".join(p.text for p in content.parts if p.text)
            if text:
                return text
    return ""


def decide(agent_name: str, state: dict, llm_request: LlmRequest) -> RoutingDecision:
    """Pure routing policy - unit-testable without any model calls."""
    baseline = AGENT_TIERS.get(agent_name, Tier.FLASH)
    d = RoutingDecision(agent=agent_name, baseline=baseline, tier=baseline)
    complexity, why = trip_complexity(state)

    if agent_name == "itinerary_refiner":
        check = state.get("budget_check") or {}
        over_pct = -float(check.get("remaining_usd", 0)) / max(
            float(check.get("budget_usd", 1)), 1
        )
        if over_pct > 0.15 or complexity >= 2:
            d.tier = Tier.PRO
            d.reasons.append(
                f"hard_refinement(over={over_pct:.0%},complexity={complexity})"
            )

    elif agent_name in (
        "budget_auditor",
        "itinerary_presenter",
        "local_insights_scout",
    ):
        if complexity >= 3:
            d.tier = Tier.PRO
            d.reasons.append("complex_trip:" + ",".join(why))

    elif agent_name == "itinerary_drafter":
        if (
            complexity == 0
            and int((state.get("trip_request") or {}).get("num_days", 9)) <= 2
        ):
            d.tier = Tier.FLASH
            d.reasons.append("simple_short_trip")

    elif agent_name == "travel_planner":
        text = _last_user_text(llm_request).strip().lower().rstrip("!.?")
        has_tool_turn = any(
            p.function_response
            for c in (llm_request.contents or [])[-2:]
            for p in (c.parts or [])
        )
        if text in _GREETINGS and not has_tool_turn:
            d.tier = Tier.LITE
            d.reasons.append("small_talk")

    if not d.reasons:
        d.reasons.append("baseline")
    return d


class ModelRouterPlugin(BasePlugin):
    """Applies dynamic routing to every LLM call and fails over on errors."""

    def __init__(self) -> None:
        super().__init__(name="model_router")

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> LlmResponse | None:
        d = decide(
            callback_context.agent_name, callback_context.state.to_dict(), llm_request
        )
        llm_request.model = d.model
        span = trace.get_current_span()
        span.set_attribute("travel.model.tier", d.tier.name)
        span.set_attribute("travel.model.routed", d.model)
        span.set_attribute("travel.model.reasons", ",".join(d.reasons))
        log_event(
            "model_routed",
            agent=d.agent,
            baseline=d.baseline.name,
            tier=d.tier.name,
            model=d.model,
            escalated=d.tier > d.baseline,
            reasons=d.reasons,
        )
        return None

    async def on_model_error_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
        error: Exception,
    ) -> LlmResponse | None:
        """Fails over to the next tier once; otherwise let the error propagate."""
        current = next(
            (t for t, m in MODELS.items() if m == llm_request.model), Tier.FLASH
        )
        fallback = FALLBACK[current]
        log_event(
            "model_failover",
            logging.WARNING,
            agent=callback_context.agent_name,
            failed_model=llm_request.model,
            fallback_model=MODELS[fallback],
            error=repr(error)[:300],
        )
        llm_request.model = MODELS[fallback]
        try:
            last: LlmResponse | None = None
            async for resp in Gemini(model=MODELS[fallback]).generate_content_async(
                llm_request, stream=False
            ):
                last = resp
            return last
        except Exception as exc:
            log_event("model_failover_failed", logging.ERROR, error=repr(exc)[:300])
            return None
