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

"""Safety guardrails and orchestration-policy callbacks.

* ``SafetyGuardrailPlugin`` (global): redacts payment-card / passport numbers
  from user input before anything is logged or sent to a model, and
  short-circuits obvious prompt-injection attempts without spending tokens.
* ``require_budget_approval`` (agent-level before_tool): a hard policy gate
  so an itinerary can only be saved after the deterministic budget audit
  has approved it - the LLM cannot skip the audit step.
* ``persist_to_memory_bank`` (agent-level after_agent): asynchronously
  ships the session to Vertex AI Memory Bank so facts learned today are
  recalled in future sessions.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from app.observability import GUARDRAIL_BLOCKS, log_event

_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_PASSPORT_RE = re.compile(
    r"\b(passport(?:\s*(?:no|number|#))?[:\s]*)([A-Z0-9]{6,9})\b", re.I
)
_INJECTION_PATTERNS = [
    r"ignore (all |any )?(previous|prior|above) (instructions|prompts)",
    r"disregard (your|the) (system|previous) (prompt|instructions)",
    r"reveal (your|the) (system prompt|instructions|hidden prompt)",
    r"you are now (dan|in developer mode)",
    r"print (your|the) (system prompt|instructions)",
]
_INJECTION_RE = re.compile("|".join(_INJECTION_PATTERNS), re.I)

REFUSAL_TEXT = (
    "I can't help with that request, but I'd love to help you plan a trip! "
    "Tell me where you'd like to go, when, and your budget."
)


def redact_pii(text: str) -> tuple[str, int]:
    """Masks card-like and passport numbers. Returns (text, redaction_count)."""
    count = 0

    def _card(m: re.Match) -> str:
        nonlocal count
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19:
            count += 1
            return f"[REDACTED-CARD-{digits[-4:]}]"
        return m.group(0)

    text = _CARD_RE.sub(_card, text)

    def _pp(m: re.Match) -> str:
        nonlocal count
        count += 1
        return f"{m.group(1)}[REDACTED-PASSPORT]"

    return _PASSPORT_RE.sub(_pp, text), count


def is_prompt_injection(text: str) -> bool:
    return bool(_INJECTION_RE.search(text or ""))


class SafetyGuardrailPlugin(BasePlugin):
    """Input-side guardrails applied before any agent or model runs."""

    def __init__(self) -> None:
        super().__init__(name="safety_guardrails")
        self._blocked_invocations: set[str] = set()

    async def on_user_message_callback(
        self, *, invocation_context, user_message: types.Content
    ):
        if not user_message or not user_message.parts:
            return None
        changed, new_parts = False, []
        for part in user_message.parts:
            if part.text:
                if is_prompt_injection(part.text):
                    self._blocked_invocations.add(invocation_context.invocation_id)
                    GUARDRAIL_BLOCKS.add(1, {"reason": "prompt_injection"})
                    log_event(
                        "guardrail_block", logging.WARNING, reason="prompt_injection"
                    )
                redacted, n = redact_pii(part.text)
                if n:
                    changed = True
                    log_event("pii_redacted", redactions=n)
                    part = types.Part.from_text(text=redacted)
            new_parts.append(part)
        return (
            types.Content(role=user_message.role, parts=new_parts) if changed else None
        )

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> LlmResponse | None:
        if callback_context.invocation_id in self._blocked_invocations:
            return LlmResponse(
                content=types.Content(
                    role="model", parts=[types.Part.from_text(text=REFUSAL_TEXT)]
                )
            )
        return None

    async def after_run_callback(self, *, invocation_context) -> None:
        self._blocked_invocations.discard(invocation_context.invocation_id)


def require_budget_approval(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext
) -> dict | None:
    """Blocks save_trip_plan unless a budget audit has run.

    Layered control:
      * no audit at all            -> hard block (deterministic, here)
      * audit ran, over budget     -> human override required (HITL via
        ``require_confirmation=needs_budget_override`` on the tool)
      * audit ran, within budget   -> allowed
    """
    if tool.name != "save_trip_plan":
        return None
    check = tool_context.state.get("budget_check") or {}
    if not check:
        log_event(
            "policy_block",
            logging.WARNING,
            tool=tool.name,
            reason="budget_not_audited",
        )
        return {
            "status": "error",
            "error_message": "Policy: the itinerary has not been run through "
            "validate_itinerary_budget. Audit it before saving.",
        }
    return None


_background_tasks: set[asyncio.Task] = set()


async def persist_to_memory_bank(callback_context: CallbackContext) -> None:
    """After each root-agent turn, push the session to long-term memory.

    Runs in the background so memory extraction never adds user latency.
    No-ops when no memory service is configured (e.g. unit tests).
    """
    inv = callback_context._invocation_context
    if inv.memory_service is None:
        return None

    async def _push() -> None:
        try:
            await callback_context.add_session_to_memory()
            log_event("memory_persisted", session_id=callback_context.session.id)
        except Exception as exc:
            log_event("memory_persist_failed", logging.WARNING, error=repr(exc))

    task = asyncio.create_task(_push())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return None
