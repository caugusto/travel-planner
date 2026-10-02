"""End-to-end demo of strategic model routing + human-in-the-loop approvals.

Runs the agent locally, plans a trip, then asks for a booking hold. Whenever
the agent pauses with an ``adk_request_confirmation`` call, this script acts
as the human reviewer and approves (or rejects with ``--reject``).

Usage:
    LOG_LEVEL=INFO uv run python scripts/hitl_demo.py [--reject]
"""

import asyncio
import os
import sys

os.environ.setdefault("DISABLE_BQ_ANALYTICS", "1")
sys.path.insert(0, os.getcwd())

from google.adk.artifacts import InMemoryArtifactService
from google.adk.memory import InMemoryMemoryService
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agent import app

APPROVE = "--reject" not in sys.argv
TURNS = [
    "Plan me a 2-day trip to Porto starting 2026-11-05 for 1 person, total budget $900.",
    "Looks great - please place a booking hold for that trip. "
    "My email is traveler@example.com and don't go above $900.",
]


async def run_turn(runner: Runner, sid: str, message: types.Content) -> list:
    """Runs one turn and returns pending confirmation function calls."""
    pending = []
    async for ev in runner.run_async(user_id="u1", session_id=sid, new_message=message):
        for p in (ev.content.parts if ev.content else []) or []:
            if p.function_call:
                fc = p.function_call
                print(f"  [{ev.author}] -> {fc.name}({str(dict(fc.args or {}))[:160]})")
                if fc.name == "adk_request_confirmation":
                    pending.append(fc)
            elif p.text and not p.thought:
                print(f"  [{ev.author}] {p.text[:800]}")
    return pending


async def main() -> None:
    sessions = InMemorySessionService()
    runner = Runner(
        app=app,
        session_service=sessions,
        memory_service=InMemoryMemoryService(),
        artifact_service=InMemoryArtifactService(),
    )
    s = await sessions.create_session(app_name=app.name, user_id="u1")
    for t in TURNS:
        print(f"\n=== USER: {t}")
        pending = await run_turn(
            runner,
            s.id,
            types.Content(role="user", parts=[types.Part.from_text(text=t)]),
        )
        while pending:
            parts = []
            for fc in pending:
                original = (fc.args or {}).get("originalFunctionCall", {})
                conf = (fc.args or {}).get("toolConfirmation", {})
                print(
                    f"\n=== HUMAN REVIEW for {original.get('name')}: "
                    f"{conf.get('hint')} -> {'APPROVE' if APPROVE else 'REJECT'}"
                )
                parts.append(
                    types.Part(
                        function_response=types.FunctionResponse(
                            id=fc.id,
                            name="adk_request_confirmation",
                            response={
                                "confirmed": APPROVE,
                                "payload": conf.get("payload"),
                            },
                        )
                    )
                )
            pending = await run_turn(
                runner, s.id, types.Content(role="user", parts=parts)
            )

    s = await sessions.get_session(app_name=app.name, user_id="u1", session_id=s.id)
    print("\n=== booking_requests:", s.state.get("user:booking_requests"))
    print(
        "=== budget_check within_budget:",
        (s.state.get("budget_check") or {}).get("within_budget"),
    )


if __name__ == "__main__":
    asyncio.run(main())
