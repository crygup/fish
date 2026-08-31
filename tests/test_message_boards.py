from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast

import discord

from extensions.events.boards import (
    BoardEvents,
    board_emoji,
    build_board_view,
    reaction_matches,
)


def _settings(**changes: object) -> dict[str, object]:
    settings: dict[str, object] = {
        "guild_id": 1,
        "board_type": "starboard",
        "channel_id": 20,
        "enabled": True,
        "threshold": 3,
        "allow_nsfw": True,
        "emoji_name": "⭐",
        "emoji_id": None,
        "emoji_animated": False,
        "emoji_set_by": None,
    }
    settings.update(changes)
    return settings


def test_board_emoji_matching_supports_unicode_and_custom_emoji() -> None:
    unicode_settings = _settings()
    assert reaction_matches(unicode_settings, discord.PartialEmoji(name="⭐"))
    assert reaction_matches(unicode_settings, "⭐")
    assert not reaction_matches(unicode_settings, discord.PartialEmoji(name="🤡"))

    custom_settings = _settings(emoji_name="party", emoji_id=99, emoji_animated=True)
    assert reaction_matches(
        custom_settings, discord.PartialEmoji(name="renamed", id=99)
    )
    assert not reaction_matches(
        custom_settings, discord.PartialEmoji(name="party", id=100)
    )
    assert board_emoji(custom_settings) == "<a:party:99>"


async def test_board_view_uses_requested_components_v2_layout() -> None:
    created_at = datetime(2026, 8, 23, 10, 30, tzinfo=timezone.utc)
    message = SimpleNamespace(
        author=SimpleNamespace(name="cry_gup", id=766953372309127168),
        content="A message with content.",
        attachments=[],
        reference=None,
        channel=SimpleNamespace(name="general"),
        jump_url="https://discord.com/channels/1/2/3",
        created_at=created_at,
    )

    view = await build_board_view(
        cast(Any, SimpleNamespace(embedcolor=0xFFB6C1)),
        cast(Any, message),
        emoji="⭐",
        count=12_345,
    )

    assert isinstance(view, discord.ui.LayoutView)
    container = cast(discord.ui.Container, view.children[0])
    heading = cast(discord.ui.TextDisplay, container.children[0]).content
    footer = cast(discord.ui.TextDisplay, container.children[-1]).content
    assert heading.startswith("### ⭐ 12,345 · cry\\_gup (766953372309127168)\n")
    assert "A message with content." in heading
    assert isinstance(container.children[1], discord.ui.Separator)
    assert footer == (
        "-# #general · [Message](https://discord.com/channels/1/2/3) · <t:1787481000:f>"
    )


async def test_reaction_count_excludes_bots_blocked_users_and_self_reactions() -> None:
    async def users():
        for user in (
            SimpleNamespace(id=1, bot=False),
            SimpleNamespace(id=2, bot=False),
            SimpleNamespace(id=3, bot=True),
        ):
            yield user

    reaction = SimpleNamespace(emoji="⭐", users=lambda **_kwargs: users())
    message = SimpleNamespace(
        author=SimpleNamespace(id=1, bot=False), reactions=[reaction]
    )
    cog = BoardEvents()

    count = await cog._reaction_count(cast(Any, message), _settings(), {2})

    assert count == 0


async def test_human_reactions_to_bot_messages_are_counted() -> None:
    async def users():
        for user in (
            SimpleNamespace(id=1, bot=False),
            SimpleNamespace(id=99, bot=True),
        ):
            yield user

    reaction = SimpleNamespace(emoji="⭐", users=lambda **_kwargs: users())
    message = SimpleNamespace(
        author=SimpleNamespace(id=99, bot=True), reactions=[reaction]
    )

    count = await BoardEvents()._reaction_count(cast(Any, message), _settings(), set())

    assert count == 1


class _Pool:
    def __init__(self) -> None:
        self.executions: list[tuple[str, tuple[object, ...]]] = []

    async def execute(self, sql: str, *args: object) -> None:
        self.executions.append((sql, args))


class _BoardCache:
    def __init__(self, *, available: bool) -> None:
        self.available = available
        self.updates: list[tuple[int, str, dict[str, object]]] = []

    def board_default_emoji_is_available(
        self, _guild_id: int, _board_type: str
    ) -> bool:
        return self.available

    def update_board(self, guild_id: int, board_type: str, **changes: object) -> None:
        self.updates.append((guild_id, board_type, changes))


async def test_deleted_custom_emoji_falls_back_and_clears_setter() -> None:
    pool = _Pool()
    cache = _BoardCache(available=True)
    cog = BoardEvents()
    cast(Any, cog).bot = SimpleNamespace(
        pool=pool,
        db_cache=cache,
        logger=SimpleNamespace(warning=lambda *_args: None),
    )
    guild = SimpleNamespace(id=1, get_emoji=lambda _emoji_id: None)

    repaired = await cog._repair_deleted_emoji(
        cast(Any, guild),
        _settings(emoji_name="gone", emoji_id=99, emoji_set_by=42),
    )

    assert repaired is not None
    assert repaired["emoji_name"] == "⭐"
    assert repaired["emoji_id"] is None
    assert repaired["emoji_set_by"] is None
    assert "emoji_set_by = NULL" in pool.executions[0][0]
    assert cache.updates == [
        (
            1,
            "starboard",
            {
                "emoji_name": "⭐",
                "emoji_id": None,
                "emoji_animated": False,
                "emoji_set_by": None,
            },
        )
    ]


async def test_deleted_custom_emoji_disables_board_when_fallback_is_taken() -> None:
    pool = _Pool()
    cache = _BoardCache(available=False)
    cog = BoardEvents()
    cast(Any, cog).bot = SimpleNamespace(
        pool=pool,
        db_cache=cache,
        logger=SimpleNamespace(warning=lambda *_args: None),
    )
    guild = SimpleNamespace(id=1, get_emoji=lambda _emoji_id: None)

    repaired = await cog._repair_deleted_emoji(
        cast(Any, guild), _settings(emoji_name="gone", emoji_id=99)
    )

    assert repaired is None
    assert "enabled = FALSE" in pool.executions[0][0]
    assert cache.updates == [(1, "starboard", {"enabled": False})]
