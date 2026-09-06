from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from test_support import not_none

from extensions.mudae import (
    SUPPORTER_BADGE_EMOJI_ID,
    SUPPORTER_BADGE_EMOJI_NAME,
    SUPPORTER_BADGE_KEY,
    SUPPORTER_BADGE_TEXT,
    SUPPORTER_MONTHLY_COINS,
    Mudae,
)
from extensions.mudae.kakera_exchange import SUPPORT_GUILD_ID


def _cog(*, pool: Any, credit: Any, db_cache: Any | None = None) -> Mudae:
    bot = SimpleNamespace(
        pool=pool,
        currency=SimpleNamespace(credit=credit),
        db_cache=db_cache,
        logger=SimpleNamespace(
            info=lambda *args: None,
            debug=lambda *args, **kwargs: None,
            exception=lambda *args, **kwargs: None,
        ),
    )
    cog = Mudae.__new__(Mudae)
    cast(Any, cog).bot = bot
    return cog


@pytest.mark.asyncio
async def test_support_booster_update_awards_badge_and_monthly_coins() -> None:
    pool = SimpleNamespace(execute=AsyncMock())
    credit = AsyncMock()
    cache = SimpleNamespace(user_badges={})
    cog = _cog(pool=pool, credit=credit, db_cache=cache)
    before = SimpleNamespace(
        id=123,
        guild=SimpleNamespace(id=SUPPORT_GUILD_ID),
        premium_since=None,
    )
    after = SimpleNamespace(
        id=123,
        guild=SimpleNamespace(id=SUPPORT_GUILD_ID),
        premium_since=datetime.now(timezone.utc),
    )

    await cast(Any, cog)._support_booster_member_update(before, after)

    credit.assert_awaited_once()
    user_id, amount, source = not_none(credit.await_args).args[:3]
    assert user_id == 123
    assert amount == SUPPORTER_MONTHLY_COINS
    assert source == "fishie_supporter"
    assert (
        not_none(credit.await_args)
        .kwargs["reference_key"]
        .startswith("fishie_supporter:123:")
    )
    assert cache.user_badges[123] == [
        {
            "emoji_name": SUPPORTER_BADGE_EMOJI_NAME,
            "emoji_id": SUPPORTER_BADGE_EMOJI_ID,
            "is_custom": True,
            "animated": False,
            "text": SUPPORTER_BADGE_TEXT,
            "badge_key": SUPPORTER_BADGE_KEY,
        }
    ]
    assert pool.execute.await_count == 1


@pytest.mark.asyncio
async def test_support_booster_update_deactivates_badge_when_boost_ends() -> None:
    pool = SimpleNamespace(execute=AsyncMock())
    credit = AsyncMock()
    cache = SimpleNamespace(
        user_badges={
            123: [
                {
                    "emoji_name": SUPPORTER_BADGE_EMOJI_NAME,
                    "emoji_id": SUPPORTER_BADGE_EMOJI_ID,
                    "is_custom": True,
                    "animated": False,
                    "text": SUPPORTER_BADGE_TEXT,
                    "badge_key": SUPPORTER_BADGE_KEY,
                }
            ]
        }
    )
    cog = _cog(pool=pool, credit=credit, db_cache=cache)
    before = SimpleNamespace(
        id=123,
        guild=SimpleNamespace(id=SUPPORT_GUILD_ID),
        premium_since=datetime.now(timezone.utc),
    )
    after = SimpleNamespace(
        id=123,
        guild=SimpleNamespace(id=SUPPORT_GUILD_ID),
        premium_since=None,
    )

    await cast(Any, cog)._support_booster_member_update(before, after)

    credit.assert_not_awaited()
    assert cache.user_badges == {}
    assert pool.execute.await_count == 1
    assert "UPDATE user_badges" in pool.execute.await_args.args[0]


@pytest.mark.asyncio
async def test_support_booster_update_ignores_other_guilds_and_non_boost_changes() -> (
    None
):
    pool = SimpleNamespace(execute=AsyncMock())
    credit = AsyncMock()
    cog = _cog(pool=pool, credit=credit, db_cache=SimpleNamespace(user_badges={}))
    now = datetime.now(timezone.utc)

    for guild_id, before_premium, after_premium in (
        (1, None, now),
        (SUPPORT_GUILD_ID, now, now),
    ):
        before = SimpleNamespace(
            id=123,
            guild=SimpleNamespace(id=guild_id),
            premium_since=before_premium,
        )
        after = SimpleNamespace(
            id=123,
            guild=SimpleNamespace(id=guild_id),
            premium_since=after_premium,
        )
        await cast(Any, cog)._support_booster_member_update(before, after)

    credit.assert_not_awaited()
    pool.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_support_booster_update_detects_managed_role_changes() -> None:
    pool = SimpleNamespace(execute=AsyncMock())
    credit = AsyncMock()
    cog = _cog(pool=pool, credit=credit, db_cache=SimpleNamespace(user_badges={}))
    booster_role = SimpleNamespace(id=999)
    guild = SimpleNamespace(id=SUPPORT_GUILD_ID, premium_subscriber_role=booster_role)
    before = SimpleNamespace(id=123, guild=guild, premium_since=None, roles=[])
    after = SimpleNamespace(
        id=123,
        guild=guild,
        premium_since=None,
        roles=[SimpleNamespace(id=999)],
    )

    await cast(Any, cog)._support_booster_member_update(before, after)

    credit.assert_awaited_once()
    assert pool.execute.await_count == 1

    # Removing the role deactivates the badge even if Discord has not yet
    # updated the premium_since timestamp in the member payload.
    pool.execute.reset_mock()
    credit.reset_mock()
    await cast(Any, cog)._support_booster_member_update(after, before)

    credit.assert_not_awaited()
    assert pool.execute.await_count == 1


@pytest.mark.asyncio
async def test_support_booster_reconciliation_syncs_current_and_removes_stale() -> None:
    current_member = SimpleNamespace(id=123, premium_since=datetime.now(timezone.utc))
    guild = SimpleNamespace(
        id=SUPPORT_GUILD_ID,
        premium_subscribers=[current_member],
        get_member=lambda user_id: current_member if user_id == 123 else None,
    )
    pool = SimpleNamespace(
        fetch=AsyncMock(return_value=[{"user_id": 123}, {"user_id": 456}]),
        execute=AsyncMock(),
    )
    credit = AsyncMock()
    bot_cache = SimpleNamespace(user_badges={})
    cog = _cog(pool=pool, credit=credit, db_cache=bot_cache)
    cast(Any, cog).bot.get_guild = lambda guild_id: guild

    await cast(Any, cog)._reconcile_support_boosters()

    assert credit.await_count == 1
    assert pool.execute.await_count == 2
    assert any(call.args[0] == 123 for call in credit.await_args_list)
    assert any(call.args[1] == 456 for call in pool.execute.await_args_list)


@pytest.mark.asyncio
async def test_support_booster_reconciliation_skips_when_guild_unavailable() -> None:
    pool = SimpleNamespace(fetch=AsyncMock(), execute=AsyncMock())
    credit = AsyncMock()
    cog = _cog(pool=pool, credit=credit, db_cache=SimpleNamespace(user_badges={}))
    cast(Any, cog).bot.get_guild = lambda _guild_id: None

    await cast(Any, cog)._reconcile_support_boosters()

    pool.fetch.assert_not_awaited()
    pool.execute.assert_not_awaited()
    credit.assert_not_awaited()
