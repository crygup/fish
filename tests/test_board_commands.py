from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import discord
from discord.ext import commands

from core.cache import BoardConfig
from extensions.discord_ext.boards import Boards, BoardSettingsView


def test_board_hybrid_groups_expose_requested_subcommands() -> None:
    groups = {
        command.name: command
        for command in Boards.__cog_commands__
        if command.parent is None
    }

    assert set(groups) == {"starboard", "clownboard"}
    for name, group in groups.items():
        assert isinstance(group, commands.HybridGroup)
        assert group.fallback == "info"
        assert group.invoke_without_command is True
        assert group.app_command is not None
        assert {command.name for command in group.commands} == {
            "edit",
            "block",
            "unblock",
        }
        edit = group.get_command("edit")
        assert isinstance(edit, commands.HybridGroup)
        assert edit.invoke_without_command is True
        assert {command.name for command in edit.commands} == {
            "stars",
            "emoji",
            "nsfw",
            "channel",
        }, name
        assert all(command.checks for command in group.walk_commands())


def test_board_emoji_parser_rejects_custom_emoji_from_another_guild() -> None:
    own = SimpleNamespace(name="party", id=123, animated=True)
    guild = SimpleNamespace(get_emoji=lambda emoji_id: own if emoji_id == 123 else None)
    ctx = cast(Any, SimpleNamespace(guild=guild))
    cog = Boards()

    assert cog._parse_board_emoji(ctx, "<a:party:123>") == ("party", 123, True)

    try:
        cog._parse_board_emoji(ctx, "<:external:456>")
    except Exception as exc:
        assert "must belong to this server" in str(exc)
    else:
        raise AssertionError("An external custom emoji was accepted")


def test_board_emoji_parser_accepts_discord_unicode_shortcodes() -> None:
    ctx = cast(Any, SimpleNamespace(guild=SimpleNamespace(get_emoji=lambda _id: None)))
    cog = Boards()

    assert cog._parse_board_emoji(ctx, ":star:") == ("⭐", None, False)
    assert cog._parse_board_emoji(ctx, ":clown_face:") == ("🤡", None, False)


def test_board_settings_view_uses_components_v2_and_requested_defaults() -> None:
    ctx = cast(
        Any,
        SimpleNamespace(
            author=SimpleNamespace(id=1),
            guild=SimpleNamespace(id=10, name="Table"),
            bot=SimpleNamespace(embedcolor=0xFFB6C1),
        ),
    )
    config = BoardConfig(
        guild_id=10,
        board_type="starboard",
        channel_id=20,
        emoji_name="⭐",
    )

    view = BoardSettingsView(Boards(), ctx, "starboard", config, (0, 0))

    assert isinstance(view, discord.ui.LayoutView)
    container = cast(discord.ui.Container, view.children[0])
    text = "\n".join(
        child.content
        for child in container.children
        if isinstance(child, discord.ui.TextDisplay)
    )
    assert "## Starboard settings · Table" in text
    assert "**Required reactions:** 3" in text
    assert "**NSFW channels:** Allowed" in text
