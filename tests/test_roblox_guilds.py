from __future__ import annotations

import datetime
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest

from extensions.owner import GuildDirectoryPaginator, GuildSnapshot
from extensions.search import Search
from extensions.search.roblox import Roblox, _parse_roblox_item_query


def test_roblox_group_has_profile_fallback_and_public_subcommands() -> None:
    command = Search.roblox_group
    assert command.name == "roblox"
    assert command.fallback == "user"
    assert not command.hidden
    assert {child.name for child in command.commands} >= {
        "asset",
        "game",
        "group",
        "item",
        "leaks",
        "outfits",
        "inventory",
    }


def test_roblox_id_parses_ids_and_profile_urls() -> None:
    assert Search._roblox_id("12345") == 12345
    assert (
        Search._roblox_id(
            "https://www.roblox.com/users/12345/profile", r"roblox\.com/users/(\d+)"
        )
        == 12345
    )
    assert (
        Search._roblox_id(
            "https://roblox.com/catalog/987/item",
            r"roblox\.com/(?:catalog|library)/(\d+)",
        )
        == 987
    )
    assert Search._roblox_id("not an id", r"roblox\.com/users/(\d+)") is None


def test_roblox_item_sale_fields_use_official_catalog_statuses() -> None:
    fields, limited = Search._roblox_item_sale_fields(
        {"priceStatus": "Free", "price": 0},
        {"IsForSale": True},
        {},
    )
    assert not limited
    assert fields == ["**Price:** Free"]

    fields, limited = Search._roblox_item_sale_fields(
        {"priceStatus": "OffSale"},
        {"IsForSale": False, "PriceInRobux": 25},
        {},
    )
    assert not limited
    assert fields == ["**Price:** Off-sale"]

    fields, limited = Search._roblox_item_sale_fields(
        {
            "itemRestrictions": ["LimitedUnique"],
            "price": 100,
            "lowestPrice": 250,
            "hasResellers": True,
        },
        {"IsLimitedUnique": True, "PriceInRobux": 100},
        {"recentAveragePrice": 175},
    )
    assert limited
    assert fields == [
        "**Original price:** 100 Robux",
        "**Price:** 250 Robux",
        "**RAP:** 175 Robux",
    ]

    fields, limited = Search._roblox_item_sale_fields(
        {"itemRestrictions": ["Limited"], "priceStatus": "OffSale", "price": 75},
        {"IsLimited": True, "IsForSale": False},
        {},
    )
    assert limited
    assert fields[:2] == [
        "**Original price:** 75 Robux",
        "**Price:** No current sellers.",
    ]

    fields, limited = Search._roblox_item_sale_fields(
        {
            "itemRestrictions": ["Limited"],
            "priceStatus": "OffSale",
            "price": 75,
            "lowestPrice": 618_033_988,
        },
        {"IsLimited": True, "IsForSale": False},
        {},
    )
    assert limited
    assert "**Price:** 618,033,988 Robux" in fields


def test_roblox_item_query_parses_filters_and_asset_types() -> None:
    query, filters = _parse_roblox_item_query(
        'red hat -creator "Roblox Official" -limited -type hair'
    )
    assert query == "red hat"
    assert filters == {
        "creator": "Roblox Official",
        "limited": True,
        "type": "hair",
        "type_id": 41,
    }


def test_roblox_item_favourite_count_uses_catalog_data() -> None:
    assert Search._roblox_item_favourite_count({"favoriteCount": 1234}, {}) == 1234


@pytest.mark.asyncio
async def test_roblox_leaks_only_keeps_roblox_owned_items() -> None:
    roblox = Roblox.__new__(Roblox)
    calls: list[dict[str, Any]] = []

    async def fake_json(
        _method: str, _url: str, *, params: dict[str, Any]
    ) -> dict[str, Any]:
        calls.append(params)
        return {
            "data": [
                {
                    "id": 1,
                    "creatorTargetId": 1,
                    "name": "Official",
                    "isOffSale": True,
                },
                {
                    "id": 2,
                    "creatorTargetId": 99,
                    "name": "Third party",
                    "isOffSale": True,
                },
            ]
        }

    setattr(roblox, "_roblox_json", fake_json)
    items = await roblox._roblox_recent_catalog_items()

    assert [item["id"] for item in items] == [1]
    assert calls
    assert any(call.get("CreatorTargetId") == "1" for call in calls)
    assert any("CreatorTargetId" not in call for call in calls)


@pytest.mark.asyncio
async def test_roblox_default_uses_the_connected_account() -> None:
    roblox = Roblox.__new__(Roblox)

    class Pool:
        async def fetchrow(self, _query: str, _user_id: int) -> dict[str, str]:
            return {"roblox": "317821579"}

    setattr(roblox, "bot", cast(Any, SimpleNamespace(pool=Pool())))

    async def fake_json(method: str, url: str, **_kwargs: Any) -> dict[str, Any]:
        assert method == "GET"
        assert url.endswith("/users/317821579")
        return {"id": 317821579, "name": "spazzkittie"}

    setattr(roblox, "_roblox_json", cast(Any, fake_json))
    ctx = SimpleNamespace(author=SimpleNamespace(id=766953372309127168, name="wrong"))

    profile = await roblox._roblox_user(None, cast(Any, ctx))

    assert profile["id"] == 317821579


@pytest.mark.asyncio
async def test_roblox_profile_only_displays_banned_when_true() -> None:
    roblox = Roblox.__new__(Roblox)
    setattr(roblox, "bot", cast(Any, SimpleNamespace(embedcolor=0x123456)))

    async def fake_thumbnail(_user_id: int) -> str:
        return "https://tr.rbxcdn.com/avatar.png"

    async def fake_json(method: str, url: str, **_kwargs: Any) -> dict[str, Any]:
        if method == "POST" and "presence" in url:
            return {"userPresences": [{"userPresenceType": 0}]}
        raise AssertionError(f"Unexpected Roblox endpoint: {url}")

    async def fake_stats(_user_id: int) -> dict[str, str]:
        return {}

    captured: dict[str, Any] = {}

    def fake_view(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    async def fake_send(**_kwargs: Any) -> None:
        return None

    setattr(roblox, "_roblox_thumbnail", fake_thumbnail)
    setattr(roblox, "_roblox_json", fake_json)
    setattr(roblox, "_roblox_user_stats", fake_stats)
    setattr(roblox, "_roblox_view", fake_view)
    ctx = SimpleNamespace(send=fake_send)

    await roblox._send_roblox_profile(
        cast(Any, ctx), {"id": 1, "name": "public", "isBanned": False}
    )
    assert "Banned" not in "\n".join(captured["sections"])


@pytest.mark.asyncio
async def test_inventory_does_not_trust_false_visibility_response() -> None:
    roblox = Roblox.__new__(Roblox)
    requested: list[str] = []

    async def fake_json(method: str, url: str, **_kwargs: Any) -> dict[str, Any]:
        assert method == "GET"
        requested.append(url)
        if url.endswith("/categories"):
            return {
                "categories": [
                    {
                        "items": [{"type": "AssetType", "id": 2}],
                    }
                ]
            }
        if "/inventory/2" in url:
            return {
                "data": [{"assetId": 5459784628, "assetName": "Public decal"}],
                "nextPageCursor": None,
            }
        raise AssertionError(f"Unexpected Roblox endpoint: {url}")

    setattr(roblox, "_roblox_json", cast(Any, fake_json))

    items = await roblox._roblox_inventory_items(317821579)

    assert items and items[0]["assetId"] == 5459784628
    assert not any("can-view-inventory" in url for url in requested)


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
    assert not view.previous.disabled
    assert not view.next.disabled
