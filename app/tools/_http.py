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

"""Shared, resilient HTTP helper used by every external-API tool.

Centralising HTTP access gives us one place to enforce:
  * hard timeouts (an agent tool must never hang a turn),
  * bounded retries with exponential back-off for transient failures,
  * an OpenTelemetry span per outbound call (visible in Cloud Trace),
  * a uniform error type that tools convert into structured error payloads.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from opentelemetry import trace
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("travel_planner.tools.http")

DEFAULT_TIMEOUT_S = 8.0
USER_AGENT = "travel-planner-agent/1.0 (+https://github.com/)"


class ExternalAPIError(RuntimeError):
    """Raised when an upstream API fails after retries."""


class _RetryableHTTPError(Exception):
    """Internal marker for 429/5xx responses that are worth retrying."""


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.4, max=3),
    retry=retry_if_exception_type((httpx.TransportError, _RetryableHTTPError)),
)
async def _get(url: str, params: dict[str, Any] | None) -> Any:
    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT_S,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        resp = await client.get(url, params=params)
    if resp.status_code == 429 or resp.status_code >= 500:
        raise _RetryableHTTPError(f"{resp.status_code} from {url}")
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


async def get_json(url: str, params: dict[str, Any] | None = None) -> Any:
    """GET a JSON document with retries, timeout and tracing.

    Returns ``None`` on HTTP 404 so callers can treat "not found" as data,
    not as an error. Raises :class:`ExternalAPIError` for anything else.
    """
    with tracer.start_as_current_span("http.get") as span:
        span.set_attribute("http.url", url)
        try:
            data = await _get(url, params)
            span.set_attribute("http.found", data is not None)
            return data
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
            logger.warning("External API call failed: %s (%s)", url, exc)
            raise ExternalAPIError(f"Upstream service unavailable: {exc}") from exc
