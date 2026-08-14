import asyncio
import datetime
from types import SimpleNamespace
from typing import Any, cast

import discord
from discord import app_commands

from core.bot import (
    ERROR_COMPONENT_CHUNK,
    ERROR_INVOCATION_LIMIT,
    Fishie,
    _error_chunks,
    _error_code_block,
    command_requires_tracking_consent,
    describe_missing_app_parameters,
    required_intents,
)


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


def test_error_report_chunks_fit_components_text_limit() -> None:
    chunks = _error_chunks("diagnostic\n" * 1_000)

    assert len(chunks) > 1
    assert all(len(_error_code_block(chunk)) < 4_000 for chunk in chunks)
    assert ERROR_COMPONENT_CHUNK <= 2_200
    assert ERROR_INVOCATION_LIMIT <= 700


def test_tracking_consent_excludes_games_and_marks_history_commands() -> None:
    history = SimpleNamespace(
        qualified_name="avatarhistory",
        extras={},
        parent=None,
    )
    game = SimpleNamespace(
        qualified_name="stats connectfour",
        extras={},
        parent=None,
    )
    alias_game = SimpleNamespace(
        qualified_name="commandstats click",
        extras={},
        parent=None,
    )
    shorthand_game = SimpleNamespace(
        qualified_name="stats ttt",
        extras={},
        parent=None,
    )
    user_history = SimpleNamespace(
        qualified_name="user usernames",
        extras={},
        parent=None,
    )
    user_avatar_history = SimpleNamespace(
        qualified_name="user avatar history",
        extras={},
        parent=None,
    )
    current_avatar = SimpleNamespace(
        qualified_name="avatar",
        extras={},
        parent=None,
    )
    avatar_history = SimpleNamespace(
        qualified_name="avatars",
        extras={},
        parent=None,
    )
    user_info = SimpleNamespace(
        qualified_name="user info",
        extras={},
        parent=None,
    )
    user_fallback = SimpleNamespace(
        qualified_name="user",
        extras={},
        parent=None,
    )
    emoji_lookup = SimpleNamespace(
        qualified_name="emoji",
        extras={},
        parent=None,
    )
    emoji_stats = SimpleNamespace(
        qualified_name="emoji stats",
        extras={},
        parent=None,
    )
    activity = SimpleNamespace(
        qualified_name="activity",
        extras={},
        parent=None,
    )
    command_stats = SimpleNamespace(
        qualified_name="stats command",
        extras={},
        parent=None,
    )
    download_stats = SimpleNamespace(
        qualified_name="stats download",
        extras={},
        parent=None,
    )
    stats_emoji = SimpleNamespace(
        qualified_name="stats emoji",
        extras={},
        parent=None,
    )
    corn = SimpleNamespace(
        qualified_name="corn",
        extras={},
        parent=None,
    )
    snipe = SimpleNamespace(
        qualified_name="snipe",
        extras={},
        parent=None,
    )
    editsnipe = SimpleNamespace(
        qualified_name="editsnipe",
        extras={},
        parent=None,
    )
    join_stats = SimpleNamespace(
        qualified_name="stats joins",
        extras={},
        parent=None,
    )
    tag_stats = SimpleNamespace(
        qualified_name="tag stats",
        extras={},
        parent=None,
    )
    reactions = SimpleNamespace(
        qualified_name="reactions",
        extras={},
        parent=None,
    )

    assert command_requires_tracking_consent(cast(Any, history))
    assert command_requires_tracking_consent(cast(Any, user_history))
    assert command_requires_tracking_consent(cast(Any, user_avatar_history))
    assert command_requires_tracking_consent(cast(Any, avatar_history))
    assert not command_requires_tracking_consent(cast(Any, current_avatar))
    assert not command_requires_tracking_consent(cast(Any, user_info))
    assert not command_requires_tracking_consent(cast(Any, user_fallback))
    assert not command_requires_tracking_consent(cast(Any, emoji_lookup))
    assert not command_requires_tracking_consent(cast(Any, emoji_stats))
    assert not command_requires_tracking_consent(cast(Any, activity))
    assert not command_requires_tracking_consent(cast(Any, command_stats))
    assert not command_requires_tracking_consent(cast(Any, download_stats))
    assert not command_requires_tracking_consent(cast(Any, stats_emoji))
    assert not command_requires_tracking_consent(
        cast(Any, SimpleNamespace(qualified_name="stats", extras={}, parent=None))
    )
    assert not command_requires_tracking_consent(cast(Any, corn))
    assert not command_requires_tracking_consent(cast(Any, snipe))
    assert not command_requires_tracking_consent(cast(Any, editsnipe))
    assert command_requires_tracking_consent(cast(Any, join_stats))
    assert command_requires_tracking_consent(cast(Any, tag_stats))
    assert not command_requires_tracking_consent(cast(Any, game))
    assert not command_requires_tracking_consent(cast(Any, alias_game))
    assert not command_requires_tracking_consent(cast(Any, shorthand_game))
    assert not command_requires_tracking_consent(cast(Any, reactions))
