from __future__ import annotations

import asyncio
from types import SimpleNamespace

# Test doubles supply only the Discord/service fields exercised by each test.
from typing import cast

import discord

from core import Fishie
from extensions.events.guilds import Guilds


class _Member:
    def __init__(self, user_id: int, *, bot: bool = False) -> None:
        self.id = user_id
        self.bot = bot


class _Guild:
    def __init__(
        self,
        guild_id: int,
        members: list[_Member],
        *,
        member_count: int | None = None,
        legacy: bool = False,
    ) -> None:
        self.id = guild_id
        self.members = members
        self.member_count = member_count if member_count is not None else len(members)
        self._legacy = legacy
        self.me = SimpleNamespace(joined_at=None)

    def get_member(self, user_id: int) -> _Member | None:
        if self._legacy and user_id == Guilds.LEGACY_BOT_ID:
            return _Member(user_id, bot=True)
        return next((member for member in self.members if member.id == user_id), None)


def _guilds(*, replacement: bool = True, guilds: list[_Guild] | None = None):
    cog = object.__new__(Guilds)
    cog.bot = cast(
        "Fishie",
        SimpleNamespace(
            guilds=guilds or [],
            user=SimpleNamespace(
                id=Guilds.REPLACEMENT_BOT_ID if replacement else Guilds.LEGACY_BOT_ID
            ),
            is_new_bot=replacement,
            is_legacy_bot=not replacement,
        ),
    )
    cog._capacity_baseline_guilds = set()
    cog._capacity_baseline_ready = False
    cog._capacity_exempt_guilds = set()
    return cog


def test_capacity_excludes_operations_guild_and_deduplicates_users() -> None:
    shared = _Member(10)
    regular = _Guild(1, [shared, _Member(11)])
    operations = _Guild(939497177821110272, [_Member(12)])
    assert Guilds._max_unique_humans([regular, operations, regular]) == 2


def test_capacity_does_not_count_partial_guild_twice() -> None:
    # ``on_guild_join`` may pass a candidate that is already in the client's
    # guild cache.  The upper bound must remain per guild, not per list entry.
    guild = _Guild(1, [_Member(10)], member_count=5)
    assert Guilds._max_unique_humans([guild, guild]) == 5


def test_legacy_member_makes_guild_permanently_exempt() -> None:
    guild = _Guild(1, [], legacy=True)
    cog = _guilds(guilds=[guild])
    assert cog._is_capacity_exempt(cast("discord.Guild", guild))
    guild._legacy = False
    assert cog._is_capacity_exempt(cast("discord.Guild", guild))


def test_capacity_baseline_does_not_evict_existing_guilds() -> None:
    guild = _Guild(1, [_Member(1)], member_count=9_000)
    cog = _guilds(guilds=[guild])

    async def run() -> None:
        await cog._enforce_capacity()

    asyncio.run(run())
    assert cog._capacity_baseline_ready
    assert cog._capacity_baseline_guilds == {1}


def test_legacy_instance_detects_replacement_on_join(monkeypatch) -> None:
    guild = _Guild(1, [])
    guild.get_member = lambda user_id: (
        _Member(user_id, bot=True) if user_id == Guilds.REPLACEMENT_BOT_ID else None
    )
    left = False

    async def leave() -> None:
        nonlocal left
        left = True

    monkeypatch.setattr(guild, "leave", leave, raising=False)
    cog = _guilds(replacement=False, guilds=[])
    monkeypatch.setattr(cog.bot, "config", {"ids": {"owner_id": 1}}, raising=False)
    monkeypatch.setattr(
        cog.bot,
        "pool",
        SimpleNamespace(execute=lambda *_args, **_kwargs: None),
        raising=False,
    )
    monkeypatch.setattr(
        cog.bot,
        "logger",
        SimpleNamespace(info=lambda *_args, **_kwargs: None),
        raising=False,
    )

    async def run() -> None:
        await cog.on_guild_join(cast("discord.Guild", guild))

    asyncio.run(run())
    assert left
