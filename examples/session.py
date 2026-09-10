"""Run a disposable preview session (see USAGE.md for setup and sandbox cleanup)."""

import argparse
import asyncio

from agent_api_sdk import AgentAPISDK

from modal_agents.output import TextOutput


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--workspace", default="/workspace")
    parser.add_argument("--input", default="Run a shell command to print hello from the sandbox.")
    parser.add_argument("--keep-session", action="store_true")
    args = parser.parse_args()
    async with AgentAPISDK(timeout=600) as client:
        session = await client.sessions.create(
            agent_id=args.agent_id,
            environment={"type": "self_hosted", "workspace_directory": args.workspace},
        )
        print(f"Session ID: {session.id}", flush=True)
        try:
            completed = False
            output = TextOutput()
            async for event in session.stream(input=args.input):
                if event.type in {
                    "session.turn.failed",
                    "session.turn.cancelled",
                    "session.failed",
                }:
                    raise RuntimeError(f"Agent failed: {event.type}")
                output.add(event)
                if event.type == "session.turn.completed":
                    completed = True
            if not completed:
                raise RuntimeError(
                    "Stream ended without a completed turn; inspect saved session items"
                )
            if not output.text:
                raise RuntimeError("Completed turn returned no response text")
            print(output.text)
        finally:
            if not args.keep_session:
                await client.sessions.delete(session.id)
                print("API session deleted. Terminate its Modal sandbox separately.")


if __name__ == "__main__":
    asyncio.run(main())
