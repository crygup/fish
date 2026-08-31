from __future__ import annotations

from types import SimpleNamespace

import discord

from extensions.context import Context


def _context(*, user_color: int | discord.Colour | None) -> Context:
    bot = SimpleNamespace(
        embedcolor=0xFAA0C1,
        get_user_color=(
            (lambda _user_id: user_color) if user_color is not None else None
        ),
    )
    context = Context.__new__(Context)
    context.bot = bot  # type: ignore[attr-defined]
    context.author = SimpleNamespace(id=42)  # type: ignore[attr-defined]
    return context


def test_context_embed_color_uses_cached_user_color_with_default_fallback() -> None:
    context = _context(user_color=0x123456)
    assert context.embedcolor == 0x123456
    assert context.embed_color == discord.Colour(0x123456)

    fallback = _context(user_color=None)
    assert fallback.embedcolor == 0xFAA0C1


def test_send_color_hook_only_replaces_default_embed_and_container_colors() -> None:
    context = _context(user_color=0x123456)
    default_embed = discord.Embed(color=0xFAA0C1)
    explicit_embed = discord.Embed(color=discord.Colour.red())
    default_container = discord.ui.Container(accent_color=0xFAA0C1)
    explicit_container = discord.ui.Container(accent_color=discord.Colour.red())
    view = discord.ui.LayoutView()
    view.add_item(default_container)
    view.add_item(explicit_container)

    context._apply_user_embed_color(  # type: ignore[attr-defined]
        {
            "embeds": [default_embed, explicit_embed],
            "view": view,
        }
    )

    assert default_embed.colour == discord.Colour(0x123456)
    assert explicit_embed.colour == discord.Colour.red()
    assert default_container.accent_color == discord.Colour(0x123456)
    assert explicit_container.accent_color == discord.Colour.red()


def test_context_get_prefix_preserves_text_spacing_and_slash_prefix() -> None:
    text_context = Context.__new__(Context)
    text_context.prefix = "fish"  # type: ignore[attr-defined]
    text_context.message = SimpleNamespace(content="fish shop")  # type: ignore[attr-defined]
    assert text_context.get_prefix == "fish "

    slash_context = Context.__new__(Context)
    slash_context.prefix = "/"  # type: ignore[attr-defined]
    slash_context.message = SimpleNamespace(content="")  # type: ignore[attr-defined]
    assert slash_context.get_prefix == "/"
