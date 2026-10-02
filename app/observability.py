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

"""Observability for the travel planner.

Three signals, all correlated by trace id:

* **Traces** - ADK emits OpenTelemetry spans for every agent, LLM call and
  tool call; on Agent Runtime they are exported to Cloud Trace. This plugin
  enriches the active span with business attributes (destination, budget
  verdict, token usage) so traces are searchable by what matters.
* **Structured logs** - one JSON line per lifecycle event, written to stdout
  with ``logging.googleapis.com/trace`` so Cloud Logging links each log line
  to its trace.
* **Metrics** - OpenTelemetry counters/histograms (tool latency, tool errors,
  tokens, guardrail blocks) exported via the configured MeterProvider.

Long-term analytics (every event -> BigQuery) is handled separately by ADK's
``BigQueryAgentAnalyticsPlugin`` (see ``agent.py``).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from opentelemetry import metrics, trace

_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
LOGGER_NAME = "travel_planner"


class CloudJsonFormatter(logging.Formatter):
    """Formats records as Cloud Logging structured JSON with trace linkage."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }
        entry.update(getattr(record, "fields", {}) or {})
        ctx = trace.get_current_span().get_span_context()
        if ctx and ctx.is_valid:
            entry["logging.googleapis.com/trace"] = (
                f"projects/{_PROJECT}/traces/{format(ctx.trace_id, '032x')}"
            )
            entry["logging.googleapis.com/spanId"] = format(ctx.span_id, "016x")
            entry["logging.googleapis.com/trace_sampled"] = ctx.trace_flags.sampled
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging() -> logging.Logger:
    """Idempotently configures the structured JSON logger."""
    logger = logging.getLogger(LOGGER_NAME)
    if not any(isinstance(h.formatter, CloudJsonFormatter) for h in logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(CloudJsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
        logger.propagate = False
    return logger


def log_event(event: str, level: int = logging.INFO, **fields: Any) -> None:
    """Emit a structured log line: ``log_event("tool_end", tool="x", ms=12)``."""
    logging.getLogger(LOGGER_NAME).log(
        level, event, extra={"fields": {"event": event, **fields}}
    )


_meter = metrics.get_meter("travel_planner")
TOOL_CALLS = _meter.create_counter(
    "travel_planner.tool.calls", description="Tool invocations"
)
TOOL_ERRORS = _meter.create_counter(
    "travel_planner.tool.errors", description="Tool failures"
)
TOOL_LATENCY = _meter.create_histogram(
    "travel_planner.tool.latency", unit="ms", description="Tool latency"
)
LLM_TOKENS = _meter.create_counter(
    "travel_planner.llm.tokens", description="LLM tokens used"
)
GUARDRAIL_BLOCKS = _meter.create_counter(
    "travel_planner.guardrail.blocks", description="Requests blocked by guardrails"
)


class TravelTelemetryPlugin(BasePlugin):
    """Global plugin adding logs, metrics and span attributes to every step."""

    def __init__(self) -> None:
        super().__init__(name="travel_telemetry")
        configure_logging()
        self._tool_start: dict[str, float] = {}
        self._agent_start: dict[str, float] = {}

    # ---------------------------------------------------------------- agents
    async def before_agent_callback(self, *, agent, callback_context: CallbackContext):
        self._agent_start[f"{callback_context.invocation_id}:{agent.name}"] = (
            time.perf_counter()
        )
        log_event(
            "agent_start",
            agent=agent.name,
            invocation_id=callback_context.invocation_id,
            session_id=callback_context.session.id,
            user_id=callback_context.user_id,
        )
        return None

    async def after_agent_callback(self, *, agent, callback_context: CallbackContext):
        start = self._agent_start.pop(
            f"{callback_context.invocation_id}:{agent.name}", None
        )
        duration = round((time.perf_counter() - start) * 1000, 1) if start else None
        log_event(
            "agent_end",
            agent=agent.name,
            invocation_id=callback_context.invocation_id,
            duration_ms=duration,
        )
        return None

    # ----------------------------------------------------------------- model
    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> LlmResponse | None:
        usage = llm_response.usage_metadata
        if usage:
            attrs = {"agent": callback_context.agent_name}
            prompt = usage.prompt_token_count or 0
            output = usage.candidates_token_count or 0
            cached = usage.cached_content_token_count or 0
            LLM_TOKENS.add(prompt, {**attrs, "type": "prompt"})
            LLM_TOKENS.add(output, {**attrs, "type": "output"})
            span = trace.get_current_span()
            span.set_attribute("travel.tokens.cached", cached)
            log_event(
                "llm_usage",
                agent=callback_context.agent_name,
                prompt_tokens=prompt,
                output_tokens=output,
                cached_tokens=cached,
            )
        return None

    async def on_model_error_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
        error: Exception,
    ) -> LlmResponse | None:
        log_event(
            "llm_error",
            logging.ERROR,
            agent=callback_context.agent_name,
            error=repr(error),
        )
        return None  # let ADK's retry / error propagation handle it

    # ----------------------------------------------------------------- tools
    async def before_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext
    ) -> dict | None:
        self._tool_start[tool_context.function_call_id or tool.name] = (
            time.perf_counter()
        )
        TOOL_CALLS.add(1, {"tool": tool.name, "agent": tool_context.agent_name})
        span = trace.get_current_span()
        for key in ("destination", "country_code", "travel_style", "budget_usd"):
            if key in tool_args:
                span.set_attribute(f"travel.{key}", str(tool_args[key]))
        log_event(
            "tool_start",
            tool=tool.name,
            agent=tool_context.agent_name,
            args=_safe(tool_args),
        )
        return None

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict,
    ) -> dict | None:
        start = self._tool_start.pop(tool_context.function_call_id or tool.name, None)
        ms = round((time.perf_counter() - start) * 1000, 1) if start else 0.0
        status = (
            result.get("status", "success") if isinstance(result, dict) else "success"
        )
        TOOL_LATENCY.record(ms, {"tool": tool.name, "status": status})
        if status == "error":
            TOOL_ERRORS.add(1, {"tool": tool.name, "kind": "handled"})
        span = trace.get_current_span()
        span.set_attribute("travel.tool.status", status)
        if isinstance(result, dict) and "within_budget" in result:
            span.set_attribute("travel.within_budget", bool(result["within_budget"]))
        log_event(
            "tool_end",
            logging.WARNING if status == "error" else logging.INFO,
            tool=tool.name,
            status=status,
            duration_ms=ms,
            error=result.get("error_message") if isinstance(result, dict) else None,
        )
        return None

    async def on_tool_error_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        error: Exception,
    ) -> dict | None:
        """Converts unexpected tool exceptions into recoverable tool results."""
        self._tool_start.pop(tool_context.function_call_id or tool.name, None)
        TOOL_ERRORS.add(1, {"tool": tool.name, "kind": "exception"})
        log_event("tool_exception", logging.ERROR, tool=tool.name, error=repr(error))
        return {
            "status": "error",
            "error_message": f"{tool.name} failed unexpectedly. Try a different "
            "approach or continue without this data and tell the traveler.",
        }


def _safe(args: dict[str, Any], limit: int = 300) -> dict[str, Any]:
    """Truncate large args (e.g. itinerary markdown) before logging."""
    out = {}
    for k, v in args.items():
        s = v if isinstance(v, (int, float, bool)) else str(v)
        out[k] = s[:limit] + "..." if isinstance(s, str) and len(s) > limit else s
    return out
