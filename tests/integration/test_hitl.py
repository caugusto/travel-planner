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

"""Integration test: human-in-the-loop booking approval against real Gemini."""

import os

os.environ.setdefault("DISABLE_BQ_ANALYTICS", "1")

import pytest
from google.adk.memory import InMemoryMemoryService
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agent import app

SAVED_TRIP = {
    "artifact": "itinerary-porto-portugal-2026-11-05.md",
    "title": "Porto in 2 days",
    "destination": "Porto, Portugal",
    "start_date": "2026-11-05",
    "num_days": 2,
    "total_cost_usd": 341.0,
}
ASK = (
    "Please place a booking hold for my saved Porto trip "
    f"({SAVED_TRIP['artifact']}). My email is traveler@example.com, cap $900."
)


async def _turn(runner: Runner, sid: str, content: types.Content) -> list:
    return [
        ev
        async for ev in runner.run_async(
            user_id="hitl_user", session_id=sid, new_message=content
        )
    ]


def _confirmation_calls(events: list) -> list:
    return [
        p.function_call
        for ev in events
        for p in (ev.content.parts if ev.content else []) or []
        if p.function_call and p.function_call.name == "adk_request_confirmation"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("approve", [True, False])
async def test_booking_requires_human_approval(approve: bool) -> None:
    sessions = InMemorySessionService()
    runner = Runner(
        app=app, session_service=sessions, memory_service=InMemoryMemoryService()
    )
    s = await sessions.create_session(
        app_name=app.name,
        user_id="hitl_user",
        state={"user:saved_trips": [SAVED_TRIP]},
    )

    events = await _turn(
        runner, s.id, types.Content(role="user", parts=[types.Part(text=ASK)])
    )
    calls = _confirmation_calls(events)
    assert calls, "booking must pause for human approval"
    state = (
        await sessions.get_session(
            app_name=app.name, user_id="hitl_user", session_id=s.id
        )
    ).state
    assert not state.get("user:booking_requests"), "nothing booked before approval"

    reply = types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=calls[0].id,
                    name="adk_request_confirmation",
                    response={"confirmed": approve, "payload": {"max_total_usd": 900}},
                )
            )
        ],
    )
    await _turn(runner, s.id, reply)
    state = (
        await sessions.get_session(
            app_name=app.name, user_id="hitl_user", session_id=s.id
        )
    ).state
    booked = state.get("user:booking_requests") or []
    if approve:
        assert len(booked) == 1 and booked[0]["reference"].startswith("WW-")
        assert booked[0]["contact"] == "t***@example.com"
    else:
        assert booked == []
