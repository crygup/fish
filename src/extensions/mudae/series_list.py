"""Components V2 views for the saved Mudae series catalogue."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Iterable

import discord

if TYPE_CHECKING:
    from extensions.context import Context


@dataclass(frozen=True, slots=True)
class ScrapedSeriesEntry:
    """A series stored from a Mudae bundle."""

    name: str
    character_count: int | None = None


@dataclass(frozen=True, slots=True)
class ScrapedSeriesBundle:
    """A bundle and all of the series currently stored beneath it."""

    id: int
    name: str
    guild_id: int
    entries: tuple[ScrapedSeriesEntry, ...] = ()


def _safe_colour(ctx: Context) -> discord.Colour | int:
    value = getattr(ctx, "embedcolor", None)
    if value is None:
        bot = getattr(ctx, "bot", None)
        value = getattr(bot, "embedcolor", discord.Colour.blurple())
    return value


class SeriesListView(discord.ui.LayoutView):
    """Paginate the catalogue and the entries inside each bundle.

    Page zero is the catalogue summary.  The remaining outer pages are sorted
    bundles, while the two controls inside a bundle container paginate only
    that bundle's series.  Keeping the inner controls in the container makes
    the distinction between bundle and series navigation clear in Discord's
    Components V2 layout.
    """

    def __init__(
        self,
        ctx: Context,
        bundles: Iterable[ScrapedSeriesBundle],
        *,
        total_series: int,
        total_bundles: int,
        highest_guilds: Iterable[tuple[str, int]],
        initial_page: int = 0,
        timeout: float = 300,
    ) -> None:
        super().__init__(timeout=timeout)
        self.ctx = ctx
        self.bundles = tuple(bundles)
        self.total_series = max(0, int(total_series))
        self.total_bundles = max(0, int(total_bundles))
        self.highest_guilds = tuple(highest_guilds)
        self.page = max(0, min(int(initial_page), len(self.bundles)))
        self.bundle_page = 0
        self.message: discord.Message | None = None
        self._buttons: list[discord.ui.Button] = []
        self._render()

    @property
    def page_count(self) -> int:
        """Number of outer pages, including the summary page."""

        return max(1, len(self.bundles) + 1)

    @staticmethod
    def _chunk_entries(
        entries: tuple[ScrapedSeriesEntry, ...],
    ) -> tuple[tuple[ScrapedSeriesEntry, ...], ...]:
        """Split bundle entries into ten-series pages."""

        if not entries:
            return ((),)
        return tuple(
            entries[index : index + 10] for index in range(0, len(entries), 10)
        )

    def _summary_items(self) -> list[discord.ui.Item[Any]]:
        lines = ["Highest guilds:"]
        if self.highest_guilds:
            lines.extend(
                f"#{index} {name} · {count:,} series"
                for index, (name, count) in enumerate(self.highest_guilds, 1)
            )
        else:
            lines.append("None")
        return [
            discord.ui.TextDisplay(
                "## List of series Fishie has saved.\n\n"
                f"{self.total_series:,} total series.\n"
                f"{self.total_bundles:,} total bundles."
            ),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(lines)),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                "-# Run `fish scrapeseries` with a recent $imab <bundle> search "
                "to help save a series!"
            ),
        ]

    def _bundle_items(self, bundle: ScrapedSeriesBundle) -> list[discord.ui.Item[Any]]:
        chunks = self._chunk_entries(bundle.entries)
        self.bundle_page %= len(chunks)
        entries = chunks[self.bundle_page]
        lines = [
            f"## {discord.utils.escape_markdown(bundle.name)}",
            f"{len(bundle.entries):,} saved series.",
        ]
        if entries:
            for entry in entries:
                name = discord.utils.escape_markdown(entry.name)
                if entry.character_count is None:
                    lines.append(f"- **{name}**")
                else:
                    lines.append(f"- **{name}** ({entry.character_count:,})")
        else:
            lines.append("No series saved in this bundle.")
        lines.append(
            f"\n-# Bundle {self.page}/{len(self.bundles)} · "
            f"Series page {self.bundle_page + 1}/{len(chunks)}"
        )
        children: list[discord.ui.Item[Any]] = [
            discord.ui.TextDisplay("\n".join(lines))
        ]
        if len(chunks) > 1:
            children.append(discord.ui.Separator())
            previous = discord.ui.Button(label="<", style=discord.ButtonStyle.secondary)
            next_button = discord.ui.Button(
                label=">", style=discord.ButtonStyle.secondary
            )

            async def previous_callback(interaction: discord.Interaction) -> None:
                self.bundle_page = (self.bundle_page - 1) % len(chunks)
                await self._edit_from_interaction(interaction)

            async def next_callback(interaction: discord.Interaction) -> None:
                self.bundle_page = (self.bundle_page + 1) % len(chunks)
                await self._edit_from_interaction(interaction)

            previous.callback = previous_callback
            next_button.callback = next_callback
            children.append(discord.ui.ActionRow(previous, next_button))
            self._buttons.extend((previous, next_button))
        return children

    def _render(self) -> None:
        self.clear_items()
        self._buttons = []
        if self.page == 0 or not self.bundles:
            children = self._summary_items()
        else:
            children = self._bundle_items(self.bundles[self.page - 1])
        self.add_item(
            discord.ui.Container(*children, accent_color=_safe_colour(self.ctx))
        )

        previous = discord.ui.Button(
            label="<",
            style=discord.ButtonStyle.secondary,
            disabled=self.page_count <= 1,
        )
        next_button = discord.ui.Button(
            label=">",
            style=discord.ButtonStyle.secondary,
            disabled=self.page_count <= 1,
        )

        async def previous_callback(interaction: discord.Interaction) -> None:
            self.page = (self.page - 1) % self.page_count
            self.bundle_page = 0
            await self._edit_from_interaction(interaction)

        async def next_callback(interaction: discord.Interaction) -> None:
            self.page = (self.page + 1) % self.page_count
            self.bundle_page = 0
            await self._edit_from_interaction(interaction)

        previous.callback = previous_callback
        next_button.callback = next_callback
        self._buttons.extend((previous, next_button))
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


class SeriesListConfirmationView(discord.ui.LayoutView):
    """Ask before opening a fuzzy bundle/series match."""

    def __init__(
        self,
        ctx: Context,
        prompt: str,
        create_view: Callable[[], SeriesListView],
        *,
        timeout: float = 120,
    ) -> None:
        super().__init__(timeout=timeout)
        self.ctx = ctx
        self.create_view = create_view
        self.message: discord.Message | None = None
        yes = discord.ui.Button(label="Yes", style=discord.ButtonStyle.secondary)
        no = discord.ui.Button(label="No", style=discord.ButtonStyle.secondary)

        async def yes_callback(interaction: discord.Interaction) -> None:
            view = self.create_view()
            view.message = interaction.message
            await interaction.response.edit_message(
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            self.stop()

        async def no_callback(interaction: discord.Interaction) -> None:
            await interaction.response.edit_message(
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            self.stop()

        yes.callback = yes_callback
        no.callback = no_callback
        self._buttons = (yes, no)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(prompt),
                discord.ui.ActionRow(yes, no),
                accent_color=_safe_colour(ctx),
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            "Only the person who opened this list can confirm it.",
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


__all__ = [
    "ScrapedSeriesBundle",
    "ScrapedSeriesEntry",
    "SeriesListConfirmationView",
    "SeriesListView",
]
