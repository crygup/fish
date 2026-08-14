from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast

from extensions.events.corn import CornReacts
from extensions.events.reactions import ReactionLogs


class _Cache:
    def __init__(self, enabled: set[int]) -> None:
        self.enabled = enabled

    def reaction_tracking_enabled(self, user_id: int) -> bool:
        return user_id in self.enabled

    def user_tracking_opted_out(self, _user_id: int, _category: str) -> bool:
        return False


class _Pool:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def execute(self, sql: str, *args: object) -> None:
        self.calls.append((sql, args))


async def test_reaction_log_requires_giver_opt_in_and_keeps_unicode_details() -> None:
    pool = _Pool()
    cog = ReactionLogs()
    cast(Any, cog).bot = SimpleNamespace(
        db_cache=_Cache({10}),
        get_user=lambda _user_id: None,
        pool=pool,
        logger=SimpleNamespace(exception=lambda *_args: None),
    )
    payload = SimpleNamespace(
        message_author_id=20,
        guild_id=30,
        channel_id=40,
        message_id=50,
        user_id=10,
        member=None,
        emoji=SimpleNamespace(id=None, name="😄"),
    )

    await cog.on_reaction_log(cast(Any, payload))

    assert len(pool.calls) == 1
    _sql, args = pool.calls[0]
    assert args[:8] == (10, 20, 30, 40, 50, "😄", None, True)
    assert isinstance(args[8], datetime)
    assert isinstance(args[9], datetime)


async def test_reaction_log_skips_when_giver_has_not_opted_in() -> None:
    pool = _Pool()
    cog = ReactionLogs()
    cast(Any, cog).bot = SimpleNamespace(
        db_cache=_Cache({20}),
        get_user=lambda _user_id: None,
        pool=pool,
        logger=SimpleNamespace(exception=lambda *_args: None),
    )
    payload = SimpleNamespace(
        message_author_id=20,
        guild_id=30,
        channel_id=40,
        message_id=50,
        user_id=10,
        member=None,
        emoji=SimpleNamespace(id=99, name="sparkle"),
    )

    await cog.on_reaction_log(cast(Any, payload))

    assert pool.calls == []


async def test_self_reactions_are_skipped_for_generic_and_corn_logs() -> None:
    user_id = 662378595192274974
    payload = SimpleNamespace(
        message_author_id=user_id,
        guild_id=30,
        channel_id=40,
        message_id=50,
        user_id=user_id,
        member=None,
        emoji=SimpleNamespace(id=None, name="🌽"),
    )

    generic_pool = _Pool()
    generic = ReactionLogs()
    cast(Any, generic).bot = SimpleNamespace(
        db_cache=_Cache({user_id}),
        get_user=lambda _user_id: None,
        pool=generic_pool,
        logger=SimpleNamespace(exception=lambda *_args: None),
    )
    await generic.on_reaction_log(cast(Any, payload))

    corn_pool = _Pool()
    corn = CornReacts()
    cast(Any, corn).bot = SimpleNamespace(
        db_cache=_Cache({user_id}),
        pool=corn_pool,
    )
    await corn.on_corn_react(cast(Any, payload))

    assert generic_pool.calls == []
    assert corn_pool.calls == []
