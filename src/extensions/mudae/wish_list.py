"""Components V2 view for a user's Mudae series wishes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable

import discord

if TYPE_CHECKING:
    from extensions.context import Context


@dataclass(frozen=True, slots=True)
class SeriesWishEntry:
    """One saved series wish and whether it exists in the scraped catalogue."""

    id: int | None
    name: str
    exact: bool = False


def _safe_colour(ctx: Context) -> discord.Colour | int:
    value = getattr(ctx, "embedcolor", None)
    if value is None:
        bot = getattr(ctx, "bot", None)
        value = getattr(bot, "embedcolor", discord.Colour.blurple())
    return value


class SeriesWishListView(discord.ui.LayoutView):
    """Paginate series wishes, showing ten entries on each page."""

    PAGE_SIZE = 10

    def __init__(
        self,
        ctx: Context,
        entries: Iterable[SeriesWishEntry],
        *,
        timeout: float = 300,
    ) -> None:
        super().__init__(timeout=timeout)
        self.ctx = ctx
        self.entries = tuple(entries)
        self.page = 0
        self.message: discord.Message | None = None
        self._buttons: tuple[discord.ui.Button, ...] = ()
        self._render()

    @property
    def page_count(self) -> int:
        return max(1, (len(self.entries) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)

    @staticmethod
    def _entry_text(entry: SeriesWishEntry) -> str:
        name = discord.utils.escape_mentions(discord.utils.escape_markdown(entry.name))
        id_text = "?" if entry.id is None else str(entry.id)
        marker = "**" if entry.exact else "***"
        return f"`{id_text}` · {marker}{name}{marker}"

    def _render(self) -> None:
        self.clear_items()
        self.page %= self.page_count
        start = self.page * self.PAGE_SIZE
        page_entries = self.entries[start : start + self.PAGE_SIZE]
        username = discord.utils.escape_mentions(
            discord.utils.escape_markdown(getattr(self.ctx.author, "name", "User"))
        )
        lines = ["Series wishes:"]
        if page_entries:
            lines.extend(self._entry_text(entry) for entry in page_entries)
        else:
            lines.append("None")

        container = discord.ui.Container(
            discord.ui.TextDisplay(f"## Wished series list for {username}"),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(lines)),
            discord.ui.Separator(),
            discord.ui.TextDisplay(f"-# Page {self.page + 1}/{self.page_count}"),
            accent_color=_safe_colour(self.ctx),
        )
        self.add_item(container)

        previous = discord.ui.Button(label="<", style=discord.ButtonStyle.secondary)
        next_button = discord.ui.Button(label=">", style=discord.ButtonStyle.secondary)

        async def previous_callback(interaction: discord.Interaction) -> None:
            self.page = (self.page - 1) % self.page_count
            await self._edit_from_interaction(interaction)

        async def next_callback(interaction: discord.Interaction) -> None:
            self.page = (self.page + 1) % self.page_count
            await self._edit_from_interaction(interaction)

        previous.callback = previous_callback
        next_button.callback = next_callback
        self._buttons = (previous, next_button)
        self.add_item(discord.ui.ActionRow(previous, next_button))

    async def _edit_from_interaction(self, interaction: discord.Interaction) -> None:
        self._render()
        if interaction.response.is_done():
            if self.message is not None:
                await self.message.edit(
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            return
        await interaction.response.edit_message(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def start(self, *, ephemeral: bool = False) -> None:
        self.message = await self.ctx.send(
            view=self,
            ephemeral=ephemeral,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            "Only the person who opened this list can use its controls.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    async def on_timeout(self) -> None:
        for button in self._buttons:
            button.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                pass


__all__ = ["SeriesWishEntry", "SeriesWishListView"]
