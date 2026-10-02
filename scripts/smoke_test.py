"""Local end-to-end smoke test: multi-turn conversation + memory across sessions."""

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

TURNS = [
    "Hi! I'm vegetarian, I love street art and live music, and I prefer a relaxed pace.",
    "What's the weather looking like in Lisbon over the next 3 days?",
    "Plan me a 3-day trip to Lisbon starting 2026-10-20 for 2 people, total budget $1400.",
]


async def main() -> None:
    sessions, memory = InMemorySessionService(), InMemoryMemoryService()
    runner = Runner(
        app=app,
        session_service=sessions,
        memory_service=memory,
        artifact_service=InMemoryArtifactService(),
    )
    s = await sessions.create_session(app_name=app.name, user_id="u1")
    turns = TURNS if len(sys.argv) < 2 else sys.argv[1:]
    for t in turns:
        print(f"\n=== USER: {t}")
        async for ev in runner.run_async(
            user_id="u1",
            session_id=s.id,
            new_message=types.Content(
                role="user", parts=[types.Part.from_text(text=t)]
            ),
        ):
            for p in (ev.content.parts if ev.content else []) or []:
                if p.function_call:
                    print(
                        f"  [{ev.author}] -> {p.function_call.name}({str(dict(p.function_call.args))[:120]})"
                    )
                elif p.text and not p.thought:
                    print(f"  [{ev.author}] {p.text[:1500]}")
            if ev.actions and ev.actions.transfer_to_agent:
                print(f"  [{ev.author}] TRANSFER -> {ev.actions.transfer_to_agent}")
    s = await sessions.get_session(app_name=app.name, user_id="u1", session_id=s.id)
    print("\n=== STATE KEYS:", sorted(s.state.keys()))
    print("profile:", s.state.get("user:traveler_profile"))
    print(
        "budget_check:",
        {
            k: v
            for k, v in (s.state.get("budget_check") or {}).items()
            if k != "most_expensive"
        },
    )
    await asyncio.sleep(2)
    # New session, same user: user-scoped state must carry over.
    s2 = await sessions.create_session(app_name=app.name, user_id="u1")
    print("new-session profile:", s2.state.get("user:traveler_profile"))
    print("saved trips:", s2.state.get("user:saved_trips"))


asyncio.run(main())
