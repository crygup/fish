from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import discord
import pytest
from discord.ext import commands
from test_support import not_none, require_type

# Test doubles supply only the Discord/service fields exercised by each test.
from core import Fishie
from extensions.context import Context
from extensions.mudae import MIN_WISHKAKERA, Mudae, MudaeID, _MudaeWishes
from extensions.mudae.wishes import (
    MudaeSeriesBundleEntry,
    MudaeWishRecord,
    normalize_bundle_name,
    normalize_wish,
    parse_mudae_series_bundle,
    series_wish_from_row,
    series_wish_values,
    split_series_values,
)


def test_mudae_wish_helpers_are_text_commands_and_group_subcommands() -> None:
    standalone = {
        command.name: command
        for command in Mudae.__cog_commands__
        if command.parent is None
    }
    assert standalone["wish"].parent is None
    assert standalone["wishseries"].parent is None
    assert standalone["wishbundle"].parent is None
    assert standalone["wishkakera"].parent is None
    assert standalone["unwish"].parent is None
    assert standalone["clearwish"].parent is None
    assert standalone["unwishseries"].parent is None
    assert standalone["clearwishseries"].parent is None
    assert standalone["unwishkakera"].parent is None
    assert standalone["recent-claimed"].parent is None
    assert all(
        getattr(standalone[name], "app_command", None) is None
        for name in (
            "wish",
            "wishseries",
            "wishbundle",
            "wishkakera",
            "unwish",
            "clearwish",
            "unwishseries",
            "clearwishseries",
            "unwishkakera",
            "recent-claimed",
        )
    )

    children = {command.name: command for command in Mudae.mudae.commands}
    assert {
        "wish",
        "wishseries",
        "wishbundle",
        "wishkakera",
        "unwish",
        "clearwish",
        "unwishseries",
        "clearwishseries",
        "unwishkakera",
        "recent-claimed",
    } <= children.keys()
    toggle_children = {
        command.name: command
        for command in require_type(children["toggle"], commands.Group).commands
    }
    assert "recent-claims" in toggle_children
    assert MIN_WISHKAKERA == 67


def test_mudae_roll_parser_extracts_character_series_and_kakera() -> None:
    first = discord.Embed.from_dict(
        {
            "author": {"name": "Philosoraptor"},
            "description": "🤔\n**45**<:kakera:469835869059153940>",
        }
    )
    second = discord.Embed.from_dict(
        {
            "author": {"name": "Arsène Lupin (CR)"},
            "description": (
                "Code: Realize ~Guardian of Rebirth~\n"
                "**42**:kakera:\nReact with any emoji to claim!"
            ),
        }
    )
    multiline = discord.Embed.from_dict(
        {
            "author": {"name": "Echoes"},
            "description": (
                "JoJo's Bizarre Adventure: Diamond\n"
                "Is Unbreakable\n"
                "**61**<:kakera:469835869059153940>"
            ),
        }
    )

    assert Mudae._parse_mudae_roll(first) == ("Philosoraptor", "🤔", 45)
    assert Mudae._parse_mudae_roll(second) == (
        "Arsène Lupin (CR)",
        "Code: Realize ~Guardian of Rebirth~",
        42,
    )
    assert Mudae._parse_mudae_roll(multiline) == (
        "Echoes",
        "JoJo's Bizarre Adventure: Diamond Is Unbreakable",
        61,
    )


def test_mudae_roll_parser_rejects_information_command_embeds() -> None:
    """Mudae's ``$im`` card is not a character spawn."""

    image_command = discord.Embed.from_dict(
        {
            "author": {"name": "Rias Gremory"},
            "description": (
                "High School DxD  <:female:452463537508450304>\n"
                "*Animanga roulette*  · **1,285<:kakera:469835869059153940>**\n"
                "Claim Rank: #5\n"
                "Like Rank: #9\n"
                "Crimson-Haired Ruin Princess (+6)"
            ),
            "footer": {"text": "1 / 79"},
        }
    )

    assert Mudae._parse_mudae_roll(image_command) is None


def test_mudae_roll_parser_rejects_ima_kakera_list() -> None:
    """Mudae's ``$ima`` list must not trigger a kakera wish."""

    ima_command = discord.Embed.from_dict(
        {
            "author": {"name": "Jujutsu Kaisen   24/91"},
            "description": (
                "*Sorcery Fight*\n*JJK*\n*呪術廻戦*\n\n"
                "**AVG:** 8,306\n**Top 10 value:** 785\n\n"
                "Total value: **17,385**<:kakera:469835869059153940>\n\n"
                "**#12** - Satoru Gojo **1,274** ka"
            ),
            "footer": {"text": "Page 1 / 7"},
        }
    )

    assert Mudae._parse_mudae_roll(ima_command) is None


def test_mudae_roll_parser_rejects_profile_embeds() -> None:
    """Mudae's ``$profile`` total must not trigger a kakera wish."""

    profile = discord.Embed.from_dict(
        {
            "author": {"name": "maronely"},
            "description": (
                "**Collection size:** 384\n"
                "**Pokédex:** 739 Pokémon\n\n"
                "**Reacts:**\n**72**x<:kakera:469791929106956298>\n"
                "**3,402**<:kakera:469835869059153940>\n\n"
                "**Keys:** 20<:bronzekey:813327760360734740>"
            ),
        }
    )

    assert Mudae._parse_mudae_roll(profile) is None


def test_mudae_roll_parser_rejects_already_claimed_cards() -> None:
    """Claimed cards must not notify character or kakera wishes."""

    claimed = discord.Embed.from_dict(
        {
            "author": {"name": "Stilgar"},
            "description": "Dune\n**35**<:kakera:469835869059153940>",
            "footer": {"text": "Belongs to kelgorathh"},
        }
    )

    assert Mudae._parse_mudae_roll(claimed) is None


def test_mudae_wish_matching_normalises_case_and_spacing() -> None:
    assert normalize_wish("  Code:   Realize ") == "code: realize"
    assert normalize_wish("Rias Gremory") == normalize_wish("rias gremory")
    assert normalize_wish("High School DxD") == normalize_wish("high school dxd")


def test_series_values_split_on_dollar_and_dedupe_without_losing_display_name() -> None:
    assert split_series_values(
        " JoJo's Bizarre Adventure $ jojo's  bizarre adventure $ Berserk $"
    ) == (
        "JoJo's Bizarre Adventure",
        "Berserk",
    )


def test_series_values_dedupe_case_and_whitespace() -> None:
    assert split_series_values("  Berserk $ BERSERK  $   ") == ("Berserk",)


def test_closest_saved_series_only_suggests_likely_typos() -> None:
    saved = ("JoJo's Bizarre Adventure", "Berserk")

    assert Mudae._closest_saved_series("jojo's bizarre adventur", saved) == (
        "JoJo's Bizarre Adventure"
    )
    # Exact matches are handled directly and do not need a confirmation.
    assert Mudae._closest_saved_series("BERSERK", saved) is None
    assert Mudae._closest_saved_series("Brand New Series", saved) is None


@pytest.mark.asyncio
async def test_wishseries_suggests_saved_typo_before_registering() -> None:
    register = AsyncMock()
    bot = SimpleNamespace(
        pool=SimpleNamespace(
            fetch=AsyncMock(return_value=[{"series_name": "Berserk"}]),
        )
    )
    cog = Mudae(cast("Fishie", bot))
    cast(Any, cog)._register_mudae_wish = register
    ctx = SimpleNamespace(
        guild=SimpleNamespace(id=123),
        author=SimpleNamespace(id=42),
        interaction=None,
        prompt=AsyncMock(return_value=object()),
        send=AsyncMock(),
    )

    await cog._register_series_wishes_with_suggestions(cast("Context", ctx), "Berserkk")

    ctx.prompt.assert_awaited_once()
    assert "Berserk" in ctx.prompt.await_args.args[0]
    register.assert_awaited_once_with(
        ctx,
        kind="series",
        value="Berserk",
        send_response=True,
    )


@pytest.mark.asyncio
async def test_wishbundle_confirms_and_adds_saved_series() -> None:
    register = AsyncMock()
    bot = SimpleNamespace(pool=SimpleNamespace())
    cog = Mudae(cast("Fishie", bot))
    cast(Any, cog)._register_mudae_wish = register
    cast(Any, cog)._fetch_scraped_series_catalog = AsyncMock(
        return_value=(
            [
                # The import is intentionally avoided here; a tiny object is
                # sufficient for the command's public bundle contract.
                SimpleNamespace(
                    name="JoJo's Bizarre Adventure",
                    guild_id=123,
                    entries=(
                        SimpleNamespace(name="Diamond Is Unbreakable"),
                        SimpleNamespace(name="Stardust Crusaders"),
                    ),
                )
            ],
            2,
            1,
            [],
        )
    )
    cast(Any, cog)._wishes_for = lambda _ctx: _MudaeWishes()
    ctx = SimpleNamespace(
        guild=SimpleNamespace(id=123),
        author=SimpleNamespace(id=42),
        interaction=None,
        prompt=AsyncMock(return_value=object()),
        send=AsyncMock(),
    )

    await cog._wish_bundle_command(cast("Context", ctx), "jojo's bizarre adventure")

    ctx.prompt.assert_awaited_once()
    assert "2" in ctx.prompt.await_args.args[0]
    assert register.await_count == 2
    assert [call.kwargs["value"] for call in register.await_args_list] == [
        "Diamond Is Unbreakable",
        "Stardust Crusaders",
    ]


def test_series_wish_list_keeps_ids_separate_for_threshold_rows() -> None:
    wishes = _MudaeWishes(
        series={"jojo"},
        series_kakera={"jojo": {100}},
        series_ids={11: "jojo", 12: "jojo"},
        series_id_types={11: "series", 12: "series_kakera"},
    )

    ordinary = Mudae._wish_list_text(wishes, "series")
    threshold = Mudae._wish_list_text(wishes, "series_kakera")

    assert "`11` · jojo" in ordinary
    assert "`12` · jojo · at least 100 kakera" in ordinary
    assert "`12` · jojo · 100 kakera" in threshold


def test_bundle_parser_extracts_name_entries_and_page() -> None:
    embed = discord.Embed.from_dict(
        {
            "author": {"name": "JoJo's Bizarre Adventure\n(Bundle)"},
            "description": (
                "**381** chars with a main series in this bundle.\n"
                "**382** total chars.\n\n"
                "· Crazy Diamond's Demonic Heartbreak (**9**)\n"
                "· JoJo's Bizarre Adventure: Diamond Is Unbreakable (**49**)"
            ),
            "footer": {"text": "Page 1 / 2"},
        }
    )

    parsed = parse_mudae_series_bundle(embed)

    assert parsed is not None
    assert parsed.name == "JoJo's Bizarre Adventure"
    assert parsed.key == "jojo's bizarre adventure"
    assert parsed.page == 1
    assert parsed.total_pages == 2
    assert parsed.entries == (
        MudaeSeriesBundleEntry(
            name="Crazy Diamond's Demonic Heartbreak",
            normalized_name="crazy diamond's demonic heartbreak",
            character_count=9,
            page=1,
            position=1,
        ),
        MudaeSeriesBundleEntry(
            name="JoJo's Bizarre Adventure: Diamond Is Unbreakable",
            normalized_name="jojo's bizarre adventure: diamond is unbreakable",
            character_count=49,
            page=1,
            position=2,
        ),
    )


def test_bundle_parser_rejects_non_bundle_embed() -> None:
    embed = discord.Embed.from_dict({"author": {"name": "JoJo's Bizarre Adventure"}})
    assert parse_mudae_series_bundle(embed) is None


def test_bundle_name_normalization_removes_marker_and_linebreaks() -> None:
    assert normalize_bundle_name("JoJo's Bizarre Adventure\n (bundle) ") == (
        "JoJo's Bizarre Adventure"
    )


def test_series_wish_row_helpers_preserve_metadata_and_filter_kind() -> None:
    row = {
        "id": 7,
        "guild_id": 123,
        "user_id": 42,
        "wish_type": "series_kakera",
        "wish_value": "JoJo's Bizarre Adventure: Diamond Is Unbreakable",
        "bundle_id": "7",
        "bundle_key": "jojo's bizarre adventure",
        "bundle_name": "JoJo's Bizarre Adventure",
        "source_guild_id": 123,
        "source_page": 2,
        "source_entry": 4,
        "kakera_threshold": 100,
    }
    record = series_wish_from_row(row)
    assert isinstance(record, MudaeWishRecord)
    assert record is not None
    assert record.id == 7
    assert record.value_key == "jojo's bizarre adventure: diamond is unbreakable"
    assert record.is_series_kakera
    assert series_wish_from_row({**row, "wish_type": "character"}) is None


def test_series_wish_values_normalizes_and_validates() -> None:
    values = series_wish_values(
        "  Diamond\nIs Unbreakable ",
        wish_type="series_kakera",
        kakera_threshold=100,
        bundle_name="JoJo's Bizarre Adventure (Bundle)",
    )
    assert values["wish_value"] == "diamond is unbreakable"
    assert values["normalized_value"] == "diamond is unbreakable"
    assert values["bundle_key"] == "jojo's bizarre adventure"
    with pytest.raises(ValueError):
        series_wish_values("Berserk", wish_type="series_kakera")


async def test_mudae_wish_listener_dms_only_matching_server_and_wish() -> None:
    user = SimpleNamespace(send=AsyncMock())
    guild = SimpleNamespace(id=123)
    bot = SimpleNamespace(
        get_user=lambda user_id: user if user_id == 42 else None,
        fetch_user=AsyncMock(),
        logger=SimpleNamespace(debug=AsyncMock()),
    )
    cog = Mudae.__new__(Mudae)
    cast(Any, cog).bot = bot
    cast(Any, cog)._mudae_wishes = {
        (123, 42): _MudaeWishes(series={normalize_wish("🤔")}),
        (456, 42): _MudaeWishes(series={normalize_wish("🤔")}),
    }
    embed = discord.Embed.from_dict(
        {
            "author": {"name": "Philosoraptor"},
            "description": "🤔\n**45**<:kakera:469835869059153940>",
        }
    )
    message = SimpleNamespace(
        author=SimpleNamespace(id=MudaeID),
        guild=guild,
        embeds=[embed],
        channel=SimpleNamespace(send=AsyncMock()),
        jump_url="https://discord.com/channels/123/456/789",
    )

    await cast(Any, cog)._mudae_wish_listener(message)

    user.send.assert_awaited_once()
    sent_args, sent_kwargs = user.send.await_args
    assert sent_args == (
        "Mudae character, **Philosoraptor** from series **🤔** spawned.\n"
        "Series: **🤔**\n"
        "Kakera: **45**\n\n"
        "[Jump to the Message](https://discord.com/channels/123/456/789)",
    )
    allowed_mentions = sent_kwargs["allowed_mentions"]
    assert not allowed_mentions.everyone
    assert not allowed_mentions.users
    assert not allowed_mentions.roles
    message.channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_mudae_kakera_wish_notification_labels_kakera() -> None:
    user = SimpleNamespace(send=AsyncMock())
    bot = SimpleNamespace(
        get_user=lambda user_id: user if user_id == 42 else None,
        fetch_user=AsyncMock(),
        logger=SimpleNamespace(debug=AsyncMock()),
    )
    cog = Mudae.__new__(Mudae)
    cast(Any, cog).bot = bot
    cast(Any, cog)._mudae_wishes = {(123, 42): _MudaeWishes(kakera={100})}
    embed = discord.Embed.from_dict(
        {
            "author": {"name": "Touka Kirishima"},
            "description": "**669**<:kakera:469835869059153940>",
        }
    )
    message = SimpleNamespace(
        author=SimpleNamespace(id=MudaeID),
        guild=SimpleNamespace(id=123),
        embeds=[embed],
        jump_url="https://discord.com/channels/123/456/789",
    )

    await cast(Any, cog)._mudae_wish_listener(message)

    assert user.send.await_args.args[0].startswith(
        "Mudae character with 669 kakera spawned."
    )


@pytest.mark.asyncio
async def test_mudae_cog_load_caches_persisted_wishes() -> None:
    bot = SimpleNamespace(
        pool=SimpleNamespace(
            fetch=AsyncMock(
                return_value=[
                    {
                        "guild_id": 123,
                        "user_id": 42,
                        "wish_type": "character",
                        "wish_value": "  Philosoraptor ",
                    },
                    {
                        "guild_id": 123,
                        "user_id": 42,
                        "wish_type": "series",
                        "wish_value": "Code:   Realize",
                    },
                    {
                        "guild_id": 123,
                        "user_id": 42,
                        "wish_type": "kakera",
                        "wish_value": "100",
                    },
                ]
            )
        ),
        logger=SimpleNamespace(info=lambda *args: None, warning=lambda *args: None),
    )
    cog = Mudae(cast("Fishie", bot))

    await cog.cog_load()

    wishes = cog._mudae_wishes[(123, 42)]
    assert wishes.characters == {"philosoraptor"}
    assert wishes.series == {"code: realize"}
    assert wishes.kakera == {100}


@pytest.mark.asyncio
async def test_new_mudae_wish_is_persisted_before_cache_update() -> None:
    execute = AsyncMock()
    bot = SimpleNamespace(
        pool=SimpleNamespace(execute=execute),
    )
    cog = Mudae(cast("Fishie", bot))
    ctx = SimpleNamespace(
        guild=SimpleNamespace(id=123),
        author=SimpleNamespace(id=42),
        send=AsyncMock(),
    )

    await cog._register_mudae_wish(
        cast("Context", ctx), kind="character", value="  Riza Hawkeye "
    )

    execute.assert_awaited_once()
    assert not_none(execute.await_args).args[1:] == (
        123,
        42,
        "character",
        "riza hawkeye",
    )
    assert cog._mudae_wishes[(123, 42)].characters == {"riza hawkeye"}


@pytest.mark.asyncio
async def test_series_kakera_threshold_updates_replace_stale_cache_value() -> None:
    execute = AsyncMock()
    bot = SimpleNamespace(pool=SimpleNamespace(execute=execute))
    cog = Mudae(cast("Fishie", bot))
    ctx = SimpleNamespace(
        guild=SimpleNamespace(id=123),
        author=SimpleNamespace(id=42),
        send=AsyncMock(),
    )

    await cog._register_mudae_wish(
        cast("Context", ctx), kind="series_kakera", value="JoJo", kakera_threshold=100
    )
    await cog._register_mudae_wish(
        cast("Context", ctx), kind="series_kakera", value="jojo", kakera_threshold=200
    )

    assert cog._mudae_wishes[(123, 42)].series_kakera == {"jojo": {200}}


@pytest.mark.asyncio
async def test_mudae_wish_removals_update_database_and_cache() -> None:
    execute = AsyncMock()
    bot = SimpleNamespace(pool=SimpleNamespace(execute=execute))
    cog = Mudae(cast("Fishie", bot))
    cast(Any, cog)._mudae_wishes = {
        (123, 42): _MudaeWishes(
            characters={"riza hawkeye"}, series={"fullmetal alchemist"}, kakera={100}
        )
    }
    ctx = SimpleNamespace(
        guild=SimpleNamespace(id=123),
        author=SimpleNamespace(id=42),
        send=AsyncMock(),
    )

    await cog._unwish_character(cast("Context", ctx), "Riza Hawkeye")
    assert not_none(execute.await_args).args[1:] == (
        123,
        42,
        "character",
        "riza hawkeye",
    )
    assert cog._mudae_wishes[(123, 42)].characters == set()

    await cog._clear_mudae_wishes(
        cast("Context", ctx), kind="series", response="cleared"
    )
    assert cog._mudae_wishes[(123, 42)].series == set()

    await cog._register_mudae_wish(cast("Context", ctx), kind="kakera", value=0)
    assert cog._mudae_wishes == {}
