from __future__ import annotations

import datetime
from types import SimpleNamespace
from typing import Any, cast

import discord

from extensions.owner import GuildDirectoryPaginator, GuildSnapshot
from extensions.tools import Tools


def test_roblox_group_has_profile_fallback_and_public_subcommands() -> None:
    command = Tools.roblox_group
    assert command.name == "roblox"
    assert command.fallback == "user"
    assert not command.hidden
    assert {child.name for child in command.commands} >= {
        "asset",
        "game",
        "group",
        "item",
        "outfits",
        "inventory",
    }


def test_roblox_id_parses_ids_and_profile_urls() -> None:
    assert Tools._roblox_id("12345") == 12345
    assert (
        Tools._roblox_id(
            "https://www.roblox.com/users/12345/profile", r"roblox\.com/users/(\d+)"
        )
        == 12345
    )
    assert (
        Tools._roblox_id(
            "https://roblox.com/catalog/987/item",
            r"roblox\.com/(?:catalog|library)/(\d+)",
        )
        == 987
    )
    assert Tools._roblox_id("not an id", r"roblox\.com/users/(\d+)") is None


def test_guild_directory_is_components_v2_and_shows_metrics() -> None:
    now = datetime.datetime.now(datetime.timezone.utc)
    owner = SimpleNamespace(name="owner", id=42)
    guild = SimpleNamespace(
        id=99,
        name="Example",
        member_count=12,
        members=[],
        owner=owner,
        owner_id=42,
        created_at=now,
        get_channel=lambda _channel_id: None,
    )
    ctx = SimpleNamespace(
        author=SimpleNamespace(id=7),
        bot=SimpleNamespace(embedcolor=discord.Colour.blurple()),
    )
    snapshot = GuildSnapshot(
        cast(Any, guild),
        command_count=8,
        last_command=now,
        last_download=now,
        last_download_auto=True,
        poketwo=True,
        joined_at=now,
    )

    view = GuildDirectoryPaginator(cast(Any, ctx), cast(Any, object()), [snapshot])

    assert isinstance(view, discord.ui.LayoutView)
    assert isinstance(view.children[0], discord.ui.Container)
    text = view.details.content
    assert "Commands:** 8" in text
    assert "Downloads:**" in text
    assert "Pokétwo solver:** Enabled" in text
    assert view.previous.disabled
    assert view.next.disabled
