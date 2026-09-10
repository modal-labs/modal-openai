"""Small synchronous CLI adapters for the installed Agents API preview SDK."""

import asyncio

from agent_api_sdk import AgentAPISDK


async def _list_agents(api_key: str) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    async with AgentAPISDK(api_key=api_key, timeout=30) as client:
        cursor = None
        while True:
            page = await client.agents.list(cursor=cursor, limit=100)
            result.extend((agent.id, agent.name or agent.id) for agent in page.page)
            if not page.has_more:
                return result
            if not page.next_cursor or page.next_cursor == cursor:
                raise ValueError("Agent listing did not advance its pagination cursor")
            cursor = page.next_cursor


def list_agents(api_key: str) -> list[tuple[str, str]]:
    return asyncio.run(_list_agents(api_key))


async def _create_agent(api_key: str, name: str, model: str) -> str:
    async with AgentAPISDK(api_key=api_key, timeout=30) as client:
        agent = await client.agents.create(
            name=name, model=model, instructions="Follow the user's instructions."
        )
        return agent.id


def create_agent(api_key: str, name: str, model: str) -> str:
    return asyncio.run(_create_agent(api_key, name, model))
