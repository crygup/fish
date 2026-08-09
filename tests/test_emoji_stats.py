from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

import extensions.discord_ext.emojis as emoji_module
from extensions.discord_ext.emojis import Emojis


class _Pool:
    async def fetch(self, *_args: object) -> list[dict[str, object]]:
        return [
            {"emoji_id": str(index), "unicode": True, "uses": index}
            for index in range(11, 0, -1)
        ]


class _Embed:
    title: str | None = None
    colour: Any = None
    footer: str | None = None

    def set_footer(self, *, text: str) -> None:
        self.footer = text


class _Pages:
    instance: _Pages | None = None

    def __init__(self, entries: list[str], *, per_page: int, ctx: object) -> None:
        self.entries = entries
        self.per_page = per_page
        self.ctx = ctx
        self.embed = _Embed()
        self.started = False
        _Pages.instance = self

    async def start(self, _ctx: object) -> None:
        self.started = True


@pytest.mark.asyncio
async def test_emoji_stats_use_the_builtin_paginator_with_ten_items_per_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(emoji_module, "SimplePages", _Pages)
    cog = cast(Any, object.__new__(Emojis))
    cog.bot = SimpleNamespace(
        pool=_Pool(),
        embedcolor=0x5865F2,
    )
    ctx = SimpleNamespace(author=SimpleNamespace(id=1))

    await cog._send_emoji_stats(ctx, title="Emoji stats", guild_id=123)

    assert _Pages.instance is not None
    assert _Pages.instance.per_page == 10
    assert len(_Pages.instance.entries) == 11
    assert _Pages.instance.embed.title == "Emoji stats"
    assert _Pages.instance.started
