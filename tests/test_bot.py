import asyncio
import datetime
from types import SimpleNamespace
from typing import Any, cast

import discord
from discord import app_commands

from core.bot import Fishie, describe_missing_app_parameters, required_intents


def test_gateway_intents_are_explicitly_scoped() -> None:
    intents = required_intents()
    assert intents.message_content
    assert intents.members
    assert intents.presences
    assert intents.moderation
    assert intents.voice_states
    assert not intents.integrations
    assert not intents.guild_scheduled_events


def test_recent_restart_message_is_updated_and_cleared(
    monkeypatch: Any,
) -> None:
    now = discord.utils.utcnow()
    edits: list[str] = []
    queries: list[str] = []

    async def fetchrow(_query: str) -> dict[str, Any]:
        return {
            "channel_id": 10,
            "message_id": 20,
            "requested_at": now - datetime.timedelta(seconds=5),
        }

    async def execute(query: str, *_args: Any) -> None:
        queries.append(query)

    async def fetch_message(_message_id: int) -> Any:
        async def edit(*, content: str) -> None:
            edits.append(content)

        return SimpleNamespace(
            created_at=now - datetime.timedelta(seconds=5),
            edit=edit,
        )

    channel = SimpleNamespace(fetch_message=fetch_message)
    bot = SimpleNamespace(
        pool=SimpleNamespace(fetchrow=fetchrow, execute=execute),
        get_channel=lambda _channel_id: channel,
        logger=SimpleNamespace(exception=lambda *_args: None),
    )
    monkeypatch.setattr(
        "core.bot.Messageable",
        type(channel),
    )

    asyncio.run(Fishie._complete_recent_restart(cast(Any, bot)))

    assert edits and edits[0].startswith("Fishie is back online. Restart took ")
    assert any("DELETE FROM bot_restart_state" in query for query in queries)


def test_missing_app_parameter_descriptions_are_filled() -> None:
    @app_commands.command(name="lookup")
    async def lookup(
        _interaction: discord.Interaction,
        user: discord.User,
        custom_value: str,
    ) -> None:
        pass

    describe_missing_app_parameters(lookup)
    descriptions = {
        parameter.name: parameter.description for parameter in lookup.parameters
    }

    assert descriptions["user"] == "Discord user to view"
    assert descriptions["custom_value"] == "Custom value for this command"
