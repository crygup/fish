from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import discord
from discord.ext import menus

from extensions.discord_ext.info import UserView
from utils.paginator import (
    LayoutPager,
    Pager,
    ReviewPageSource,
)
from utils.vars import Review, ReviewSender


def _context() -> Any:
    return SimpleNamespace(
        author=SimpleNamespace(id=1),
        bot=SimpleNamespace(embedcolor=discord.Colour.blurple()),
        guild=None,
        message=None,
    )


def _reviews(count: int = 2) -> list[Review]:
    return [
        Review(
            id=index + 1,
            sender=ReviewSender(
                user_id=20 + index,
                profilePhoto="https://example.com/avatar.png",
                username=f"reviewer-{index}",
            ),
            comment=f"Review {index}",
            timestamp=1_700_000_000 + index,
            target_id=1,
        )
        for index in range(count)
    ]


def test_legacy_pager_uses_shared_controls() -> None:
    pager = Pager(
        menus.ListPageSource(["one", "two"], per_page=1),
        ctx=cast(Any, _context()),
    )

    controls = [
        child for child in pager.children if isinstance(child, discord.ui.Button)
    ]
    assert [button.label for button in controls] == ["<", ">", None, "#", None]
    assert all(button.style is discord.ButtonStyle.secondary for button in controls)
    assert not controls[0].disabled
    assert not controls[1].disabled
    assert str(controls[2].emoji) == "🔀"
    assert str(controls[4].emoji) == "🗑️"


def test_editable_legacy_pager_uses_red_trash() -> None:
    async def delete_page(_page_number: int) -> bool:
        return True

    pager = Pager(
        menus.ListPageSource(["one", "two"], per_page=1),
        ctx=cast(Any, _context()),
        delete_page=delete_page,
    )

    controls = [
        child for child in pager.children if isinstance(child, discord.ui.Button)
    ]
    assert all(
        button.style is discord.ButtonStyle.secondary for button in controls[:-1]
    )
    assert controls[-1].style is discord.ButtonStyle.danger


def test_layout_review_paginator_matches_userinfo_structure() -> None:
    pager = LayoutPager(
        ReviewPageSource(_reviews(), user_label="target"),
        ctx=cast(Any, _context()),
    )

    assert len(pager.children) == 2
    container = cast(discord.ui.Container, pager.children[0])
    assert isinstance(container.children[0], discord.ui.Section)
    section = cast(discord.ui.Section, container.children[0])
    section_text = cast(discord.ui.TextDisplay, section.children[0]).content
    assert "## Review by reviewer-0" in section_text
    assert "Review 0" in section_text
    assert "Page 1/2 · Review ID: 1" in section_text
    row = cast(discord.ui.ActionRow, pager.children[1])
    assert [getattr(button, "label", None) for button in row.children] == [
        "<",
        ">",
        None,
        "#",
        None,
    ]
    assert all(
        button.style is discord.ButtonStyle.secondary
        for button in row.children
        if isinstance(button, discord.ui.Button)
    )
    assert not cast(discord.ui.Button, row.children[0]).disabled
    assert not cast(discord.ui.Button, row.children[1]).disabled


def test_layout_review_paginator_empty_state_has_no_controls() -> None:
    pager = LayoutPager(
        ReviewPageSource([], user_label="target"),
        ctx=cast(Any, _context()),
    )

    assert len(pager.children) == 1
    container = cast(discord.ui.Container, pager.children[0])
    text = "\n".join(
        child.content
        for child in container.children
        if isinstance(child, discord.ui.TextDisplay)
    )
    assert "## Reviews for target" in text
    assert "This user has no reviews." in text


def test_userinfo_review_page_uses_shared_source() -> None:
    user = SimpleNamespace(
        id=1,
        mention="<@1>",
        bot=False,
        banner=None,
        display_avatar=SimpleNamespace(url="https://example.com/avatar.png"),
    )
    view = UserView(
        cast(Any, _context()),
        cast(Any, user),
        cast(Any, user),
        "Profile",
        "Footer",
    )
    view._reviews = _reviews(1)
    view._review_source = ReviewPageSource(view._reviews, user_label="target")
    view._page = "reviews"
    view._render()

    container = cast(discord.ui.Container, view.children[0])
    section = cast(discord.ui.Section, container.children[0])
    section_text = cast(discord.ui.TextDisplay, section.children[0]).content
    assert "## Review by reviewer-0" in section_text
    row = cast(discord.ui.ActionRow, view.children[-1])
    assert [getattr(button, "label", None) for button in row.children] == [
        "<",
        ">",
        None,
        "#",
        None,
    ]


def test_userinfo_index_shows_profile_title_before_mention() -> None:
    user = SimpleNamespace(
        id=1,
        mention="<@1>",
        bot=False,
        banner=None,
        display_avatar=SimpleNamespace(url="https://example.com/avatar.png"),
    )
    view = UserView(
        cast(Any, _context()),
        cast(Any, user),
        cast(Any, user),
        "**Badges:**\n🐟 Fishie",
        "Footer",
        profile_title="Dr Pepper test",
    )

    container = cast(discord.ui.Container, view.children[0])
    section = cast(discord.ui.Section, container.children[0])
    body = cast(discord.ui.TextDisplay, section.children[1]).content
    assert body == "-# Dr Pepper test\n<@1>\n**Badges:**\n🐟 Fishie"


def test_layout_pager_defers_before_preparing_a_lazy_page() -> None:
    events: list[str] = []

    class LazySource:
        def get_max_pages(self) -> int:
            return 2

        def format_page(self, page_number: int) -> list[discord.ui.Item[Any]]:
            return [discord.ui.TextDisplay(f"Page {page_number + 1}")]

        async def prepare_page(self, page_number: int) -> bool:
            events.append("prepare")
            assert interaction.response.is_done()
            return True

    class Response:
        def __init__(self) -> None:
            self.done = False

        def is_done(self) -> bool:
            return self.done

        async def defer(self) -> None:
            events.append("defer")
            self.done = True

    class Interaction:
        def __init__(self) -> None:
            self.response = Response()

    class Message:
        async def edit(self, **kwargs: Any) -> None:
            events.append("edit")

    async def scenario() -> None:
        nonlocal interaction
        interaction = Interaction()
        ctx = _context()
        pager = LayoutPager(cast(Any, LazySource()), ctx=cast(Any, ctx))
        pager.message = cast(Any, Message())
        await pager._set_layout_page(cast(Any, interaction), 1)
        assert pager.page == 1

    interaction: Interaction
    asyncio.run(scenario())
    assert events == ["defer", "prepare", "edit"]
