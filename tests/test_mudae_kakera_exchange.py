from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from extensions.mudae import Mudae, MudaeID
from extensions.mudae.kakera_exchange import (
    FISHIE_OWNER_ID,
    KAKERA_PER_COIN,
    SUPPORT_GUILD_ID,
    parse_mudae_kakera_gift,
)


def test_parse_mudae_kakera_gift_extracts_mentions_and_amount() -> None:
    gift = parse_mudae_kakera_gift(
        f"<@427951571025002497> just gifted **5,470**:kakera:to <@{FISHIE_OWNER_ID}>"
    )

    assert gift is not None
    assert gift.giver_id == 427951571025002497
    assert gift.receiver_id == FISHIE_OWNER_ID
    assert gift.kakera == 5_470
    assert gift.coins == 5_470 // KAKERA_PER_COIN


@pytest.mark.parametrize(
    "content",
    (
        "<@1> gifted **100**:kakera:to <@2>",
        "<@1> just gifted 100:kakera:to <@2>",
        "<@1> just gifted **1,00**:kakera:to <@2>",
        "plain text mentioning kakera",
    ),
)
def test_parse_mudae_kakera_gift_rejects_non_gift_messages(content: str) -> None:
    assert parse_mudae_kakera_gift(content) is None


@pytest.mark.asyncio
async def test_mudae_kakera_exchange_credits_only_support_gifts() -> None:
    credit = AsyncMock()
    bot = SimpleNamespace(
        currency=SimpleNamespace(credit=credit),
        pool=SimpleNamespace(fetchval=AsyncMock(return_value=None)),
        logger=SimpleNamespace(info=lambda *args: None, exception=lambda *args: None),
    )
    cog = Mudae.__new__(Mudae)
    cast(Any, cog).bot = bot

    message = SimpleNamespace(
        id=1544342553384456324,
        guild=SimpleNamespace(id=SUPPORT_GUILD_ID),
        author=SimpleNamespace(id=MudaeID),
        content=f"<@427951571025002497> just gifted **5,470**:kakera:to <@{FISHIE_OWNER_ID}>",
    )
    await cast(Any, cog)._mudae_kakera_exchange_listener(message)

    credit.assert_awaited_once_with(
        427951571025002497,
        2_735,
        "mudae_kakera_exchange",
        reference_key="mudae_kakera_exchange:1544342553384456324",
    )


@pytest.mark.asyncio
async def test_mudae_kakera_exchange_ignores_wrong_scope_or_recipient() -> None:
    credit = AsyncMock()
    bot = SimpleNamespace(
        currency=SimpleNamespace(credit=credit),
        pool=SimpleNamespace(fetchval=AsyncMock(return_value=None)),
        logger=SimpleNamespace(info=lambda *args: None, exception=lambda *args: None),
    )
    cog = Mudae.__new__(Mudae)
    cast(Any, cog).bot = bot
    content = (
        "<@427951571025002497> just gifted **5,470**:kakera:to <@766953372309127168>"
    )

    for guild_id, author_id, receiver_id in (
        (123, MudaeID, FISHIE_OWNER_ID),
        (SUPPORT_GUILD_ID, 123, FISHIE_OWNER_ID),
        (SUPPORT_GUILD_ID, MudaeID, 123),
    ):
        message = SimpleNamespace(
            id=guild_id + author_id,
            guild=SimpleNamespace(id=guild_id),
            author=SimpleNamespace(id=author_id),
            content=(
                content
                if receiver_id == FISHIE_OWNER_ID
                else content.replace(str(FISHIE_OWNER_ID), str(receiver_id))
            ),
        )
        await cast(Any, cog)._mudae_kakera_exchange_listener(message)

    credit.assert_not_awaited()


@pytest.mark.asyncio
async def test_mudae_kakera_exchange_is_idempotent_by_message_reference() -> None:
    credit = AsyncMock()
    bot = SimpleNamespace(
        currency=SimpleNamespace(credit=credit),
        pool=SimpleNamespace(fetchval=AsyncMock(return_value=1)),
        logger=SimpleNamespace(info=lambda *args: None, exception=lambda *args: None),
    )
    cog = Mudae.__new__(Mudae)
    cast(Any, cog).bot = bot
    message = SimpleNamespace(
        id=123,
        guild=SimpleNamespace(id=SUPPORT_GUILD_ID),
        author=SimpleNamespace(id=MudaeID),
        content="<@427951571025002497> just gifted **5,470**:kakera:to <@766953372309127168>",
    )

    await cast(Any, cog)._mudae_kakera_exchange_listener(message)

    credit.assert_not_awaited()
