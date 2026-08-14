from __future__ import annotations

import asyncio
import datetime
import random
import re
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Protocol,
    Tuple,
    cast,
    runtime_checkable,
)

import discord
from dateutil.parser import parse
from discord.ext import commands, menus
from discord.ext.commands import Paginator as CommandPaginator

from .emojis import fish_check
from .functions import human_join
from .vars import GoogleImageData, Review

if TYPE_CHECKING:
    from extensions.context import Context


@runtime_checkable
class Disableable(Protocol):
    disabled: bool


class Pager(discord.ui.View):
    def __init__(
        self,
        source: menus.PageSource,
        *,
        ctx: Context,
        check_embeds: bool = True,
        compact: bool = False,
    ):
        super().__init__()
        self.source: menus.PageSource = source
        self.check_embeds: bool = check_embeds
        self.ctx: Context = ctx
        self.message: Optional[discord.Message] = None
        self.current_page: int = 0
        self.compact: bool = compact
        self.input_lock = asyncio.Lock()

    def disable_all(self) -> None:
        for button in self.children:
            if isinstance(button, Disableable):
                button.disabled = True

    async def on_timeout(self) -> None:
        self.disable_all()
        if self.message:
            await self.message.edit(
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def show_checked_page(
        self, interaction: discord.Interaction, page_number: int
    ) -> None:
        max_pages = self.source.get_max_pages()
        if max_pages is not None and max_pages > 0:
            page_number %= max_pages
        try:
            if max_pages is None:
                await self.show_page(interaction, page_number)
            elif max_pages > 0:
                await self.show_page(interaction, page_number)
        except IndexError:
            message = "That page does not exist."
            if interaction.response.is_done():
                await interaction.followup.send(
                    message,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await interaction.response.send_message(
                    message,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        if max_pages is not None and max_pages <= 0:
            message = "That page does not exist."
            if interaction.response.is_done():
                await interaction.followup.send(
                    message,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await interaction.response.send_message(
                    message,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

    def _update_labels(self, page_number: int) -> None:
        self.go_to_next_page.disabled = False
        self.go_to_previous_page.disabled = False
        self.go_to_random_page.disabled = False
        self.go_to_number_page.disabled = False

        max_pages = self.source.get_max_pages()
        if max_pages is not None:
            if max_pages <= 1:
                self.go_to_random_page.disabled = True
                self.go_to_number_page.disabled = True
        else:
            self.go_to_random_page.disabled = True

    async def start(self, ctx: Context, e=False):
        await self.source._prepare_once()
        page = await self.source.get_page(0)
        kwargs = await self._get_kwargs_from_page(page)
        self._update_labels(0)
        kwargs["allowed_mentions"] = discord.AllowedMentions.none()
        self.message = await self.ctx.send(**kwargs, view=self, ephemeral=e)

    async def _get_kwargs_from_page(self, page: int) -> Dict[str, Any]:
        value = await discord.utils.maybe_coroutine(self.source.format_page, self, page)
        if isinstance(value, dict):
            return value
        elif isinstance(value, str):
            return {"content": value, "embed": None}
        elif isinstance(value, discord.Embed):
            return {"embed": value, "content": None}
        else:
            return {}

    async def show_page(
        self, interaction: discord.Interaction, page_number: int
    ) -> None:
        page = await self.source.get_page(page_number)
        self.current_page = page_number
        kwargs = await self._get_kwargs_from_page(page)
        self._update_labels(page_number)
        if kwargs:
            kwargs["allowed_mentions"] = discord.AllowedMentions.none()
            if interaction.response.is_done():
                if self.message:
                    await self.message.edit(
                        **kwargs,
                        view=self,
                    )
            else:
                await interaction.response.edit_message(**kwargs, view=self)

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user and interaction.user == self.ctx.author:
            return True
        await interaction.response.send_message(
            f"You can't use this, sorry. \nIf you'd like to use this then run the command `{self.ctx.command}{self.ctx.invoked_subcommand or ''}`",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    @discord.ui.button(label="<", style=discord.ButtonStyle.secondary)
    async def go_to_previous_page(self, interaction: discord.Interaction, __):
        """go to the previous page"""
        await self.show_checked_page(interaction, self.current_page - 1)

    @discord.ui.button(label=">", style=discord.ButtonStyle.secondary)
    async def go_to_next_page(self, interaction: discord.Interaction, __):
        """go to the next page"""
        await self.show_checked_page(interaction, self.current_page + 1)

    @discord.ui.button(emoji="\U0001f500", style=discord.ButtonStyle.secondary)
    async def go_to_random_page(self, interaction: discord.Interaction, __):
        """Jump to a random page without repeating the current page when possible."""
        maximum = self.source.get_max_pages()
        if maximum is None or maximum <= 1:
            await interaction.response.send_message(
                "There are no other pages to choose from.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        choices = [page for page in range(maximum) if page != self.current_page]
        await self.show_checked_page(interaction, random.choice(choices))

    @discord.ui.button(label="#", style=discord.ButtonStyle.secondary)
    async def go_to_number_page(self, interaction: discord.Interaction, __):
        max_pages = self.source.get_max_pages()
        menu = self

        class GoPage(discord.ui.Modal, title="Go to page"):
            stuff = discord.ui.TextInput(
                label=f"Enter a number (1/{max_pages})",
                min_length=0,
                required=True,
                style=discord.TextStyle.short,
            )

            async def on_submit(self, interaction: discord.Interaction) -> None:
                if self.stuff.value and self.stuff.value.isdigit():
                    page = int(self.stuff.value)
                else:
                    await interaction.response.send_message(
                        "Please enter a valid number",
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return

                if max_pages and page > max_pages or page < 1:
                    await interaction.response.send_message(
                        f"Page **{page}** does not exist",
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return

                await interaction.response.defer()
                await menu.show_page(interaction, page - 1)

        await interaction.response.send_modal(GoPage())

    @discord.ui.button(emoji="\U0001f5d1\ufe0f", style=discord.ButtonStyle.secondary)
    async def stop_pages(self, interaction: discord.Interaction, __):
        """stops the pagination session."""
        await interaction.response.defer()
        if self.message:
            await self.message.delete()
        else:
            await interaction.delete_original_response()
        if self.ctx.message:
            try:
                await self.ctx.message.add_reaction(fish_check)
            except discord.HTTPException:
                pass
        self.stop()


class LayoutPageSource(Protocol):
    """Source contract for Components V2 paginators."""

    def get_max_pages(self) -> int: ...

    def format_page(self, page_number: int) -> Iterable[discord.ui.Item[Any]]: ...


class LayoutPageModal(discord.ui.Modal, title="Go to page"):
    """Shared page-number modal for Components V2 paginators."""

    def __init__(self, paginator: Any, page_count: int) -> None:
        super().__init__()
        self.paginator = paginator
        self.page_count = page_count
        self.page = discord.ui.TextInput(
            label=f"Enter a page number (1/{page_count})",
            min_length=1,
            max_length=8,
            required=True,
            style=discord.TextStyle.short,
        )
        self.add_item(self.page)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.paginator.ctx.author.id:
            await interaction.response.send_message(
                "Only the person who opened this paginator can change its page.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        value = str(self.page.value).strip()
        if not value.isdigit():
            await interaction.response.send_message(
                "Please enter a valid page number.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        page = int(value)
        if page < 1 or page > self.page_count:
            await interaction.response.send_message(
                f"Page **{page}** does not exist.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        # Modal submissions cannot use ``edit_message`` as their initial
        # response. Defer first so the shared paginator can edit its original
        # message through the interaction webhook.
        await interaction.response.defer()
        await self.paginator._set_layout_page(interaction, page - 1)


def build_layout_pagination_row(
    *,
    page: int,
    page_count: int,
    previous: Callable[[discord.Interaction], Any],
    next_page: Callable[[discord.Interaction], Any],
    shuffle: Callable[[discord.Interaction], Any],
    go_to_page: Callable[[discord.Interaction], Any],
    trash: Callable[[discord.Interaction], Any],
) -> tuple[discord.ui.ActionRow, tuple[discord.ui.Button, ...]]:
    """Build the shared Components V2 navigation row.

    The returned buttons are also exposed so a parent view can disable them on
    timeout. The order is always ``<``, ``>``, shuffle, ``#``, and trash.
    """

    previous_button = discord.ui.Button(
        label="<",
        style=discord.ButtonStyle.secondary,
    )
    next_button = discord.ui.Button(
        label=">",
        style=discord.ButtonStyle.secondary,
    )
    shuffle_button = discord.ui.Button(
        emoji="\U0001f500",
        style=discord.ButtonStyle.secondary,
        disabled=page_count <= 1,
    )
    page_button = discord.ui.Button(
        label="#",
        style=discord.ButtonStyle.secondary,
        disabled=page_count <= 1,
    )
    trash_button = discord.ui.Button(
        emoji="\U0001f5d1\ufe0f",
        style=discord.ButtonStyle.secondary,
    )
    previous_button.callback = cast(Any, previous)
    next_button.callback = cast(Any, next_page)
    shuffle_button.callback = cast(Any, shuffle)
    page_button.callback = cast(Any, go_to_page)
    trash_button.callback = cast(Any, trash)
    buttons = (
        previous_button,
        next_button,
        shuffle_button,
        page_button,
        trash_button,
    )
    return discord.ui.ActionRow(*buttons), buttons


class LayoutPager(discord.ui.LayoutView):
    """Reusable Components V2 paginator for already-loaded page data."""

    def __init__(
        self,
        source: LayoutPageSource,
        *,
        ctx: Context,
        accent_color: discord.Colour | int | None = None,
        timeout: float = 300,
    ) -> None:
        super().__init__(timeout=timeout)
        self.source = source
        self.ctx = ctx
        self.accent_color = accent_color or ctx.bot.embedcolor
        self.page = 0
        self.message: discord.Message | None = None
        self._navigation_buttons: tuple[discord.ui.Button, ...] = ()
        self._render()

    @property
    def page_count(self) -> int:
        return max(0, int(self.source.get_max_pages()))

    def _page_items(self) -> list[discord.ui.Item[Any]]:
        if self.page_count == 0:
            return list(self.source.format_page(0))
        return list(self.source.format_page(self.page))

    def _render(self) -> None:
        self.clear_items()
        children = self._page_items()
        if not children:
            children = [discord.ui.TextDisplay("No pages are available.")]
        self.add_item(discord.ui.Container(*children, accent_color=self.accent_color))
        if self.page_count > 1:
            row, buttons = build_layout_pagination_row(
                page=self.page,
                page_count=self.page_count,
                previous=self._previous_page,
                next_page=self._next_page,
                shuffle=self._shuffle_page,
                go_to_page=self._open_page_modal,
                trash=self._delete,
            )
            self._navigation_buttons = buttons
            self.add_item(row)
        else:
            self._navigation_buttons = ()

    async def _prepare_page(self, page: int) -> bool:
        """Allow a page source to lazily prepare content before rendering."""
        prepare = getattr(self.source, "prepare_page", None)
        if prepare is None:
            return True
        result = await discord.utils.maybe_coroutine(prepare, page)
        return result is not False

    async def _send_page_error(self, interaction: discord.Interaction) -> None:
        message = "That page does not exist."
        if interaction.response.is_done():
            await interaction.followup.send(
                message,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await interaction.response.send_message(
                message,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def start(self, _ctx: Context | None = None, *, e: bool = False) -> None:
        await self._prepare_page(0)
        self._render()
        self.message = await self.ctx.send(
            view=self,
            ephemeral=e,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _edit_from_interaction(self, interaction: discord.Interaction) -> None:
        self._render()
        if interaction.response.is_done():
            if self.message:
                await self.message.edit(
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await interaction.edit_original_response(
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        else:
            await interaction.response.edit_message(
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _set_layout_page(
        self, interaction: discord.Interaction, page: int
    ) -> None:
        # A page source may need to fetch data before it can render the page
        # (for example, the films paginator loads a later Letterboxd diary
        # page on demand).  Discord only gives component interactions a few
        # seconds to be acknowledged, so defer before doing any lazy work.
        # Modal submissions are already deferred by ``LayoutPageModal`` and
        # must not be acknowledged twice.
        if not interaction.response.is_done():
            await interaction.response.defer()

        page_count = self.page_count
        if page_count <= 0:
            await self._send_page_error(interaction)
            return
        page %= page_count
        requested_page = page
        if not await self._prepare_page(page):
            page_count = self.page_count
            if page_count <= 0:
                await self._send_page_error(interaction)
                return
            page %= page_count
            # A lazy source may discover that its original upper bound was
            # too large (Films does this after probing an empty final diary
            # page).  Prepare the normalized page again before rendering it.
            # If the requested page itself is unavailable, leave the current
            # page visible and report the failure instead of rendering stale
            # or empty content as though the navigation succeeded.
            if page != requested_page and not await self._prepare_page(page):
                await self._send_page_error(interaction)
                return
            if page == requested_page:
                await self._send_page_error(interaction)
                return
        self.page = page
        await self._edit_from_interaction(interaction)

    async def _previous_page(self, interaction: discord.Interaction) -> None:
        if self.page_count:
            await self._set_layout_page(interaction, (self.page - 1) % self.page_count)

    async def _next_page(self, interaction: discord.Interaction) -> None:
        if self.page_count:
            await self._set_layout_page(interaction, (self.page + 1) % self.page_count)

    async def _shuffle_page(self, interaction: discord.Interaction) -> None:
        if self.page_count <= 1:
            await interaction.response.send_message(
                "There are no other pages to choose from.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        choices = [index for index in range(self.page_count) if index != self.page]
        await self._set_layout_page(interaction, random.choice(choices))

    async def _open_page_modal(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(LayoutPageModal(self, self.page_count))

    async def _delete(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if self.message:
            await self.message.delete()
        else:
            await interaction.delete_original_response()
        self.stop()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            "Only the person who opened this paginator can use its controls.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        _item: discord.ui.Item[Any] | None = None,
    ) -> None:
        """Log component failures and acknowledge the interaction."""
        self.ctx.bot.logger.info(
            "Layout paginator %s errored for user_id=%s",
            type(self).__name__,
            self.ctx.author.id,
        )
        try:
            await self.ctx.bot.log_error(
                error,
                context=self.ctx,
                interaction=interaction,
            )
        except Exception:
            self.ctx.bot.logger.exception("Could not send paginator error report")

        message = str(error) or "That paginator could not be updated."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    message,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await interaction.response.send_message(
                    message,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except discord.DiscordException:
            self.ctx.bot.logger.exception("Could not acknowledge paginator error")

    async def on_timeout(self) -> None:
        for button in self._navigation_buttons:
            button.disabled = True
        if self.message:
            try:
                await self.message.edit(
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                pass


class ReviewPageSource:
    """Components V2 page source shared by ``userinfo`` and ``reviews``."""

    def __init__(self, entries: List[Review], *, user_label: str):
        self.entries = entries
        self.user_label = user_label

    def get_max_pages(self) -> int:
        return len(self.entries)

    def format_page(self, page_number: int) -> Iterable[discord.ui.Item[Any]]:
        if not self.entries:
            return [
                discord.ui.TextDisplay(f"## Reviews for {self.user_label}"),
                discord.ui.Separator(),
                discord.ui.TextDisplay("This user has no reviews."),
            ]
        review = self.entries[page_number]
        author = review.sender
        author_name = discord.utils.escape_mentions(
            discord.utils.escape_markdown(author.username)
        )
        comment = discord.utils.escape_mentions(review.comment or "No review text.")
        text = (
            f"## Review by {author_name}\n"
            f"{comment}\n\n"
            f"-# Page {page_number + 1}/{len(self.entries)} · Review ID: {review.id}"
        )
        if author.profilePhoto:
            return [
                discord.ui.Section(
                    discord.ui.TextDisplay(text),
                    accessory=discord.ui.Thumbnail(author.profilePhoto),
                )
            ]
        return [discord.ui.TextDisplay(text)]


class FieldPageSource(menus.ListPageSource):
    """A page source that requires (field_name, field_value) tuple items."""

    def __init__(
        self, entries: List[Tuple[str, str]], *, per_page=12, footer: bool = False
    ):
        super().__init__(entries, per_page=per_page)
        self.footer = footer
        self.embed = discord.Embed(colour=0x2F3136)

    async def format_page(self, menu: Pager, entries: Tuple[str, str]):
        self.embed.clear_fields()

        for key, value in entries:
            self.embed.add_field(name=key, value=value, inline=False)

        maximum = self.get_max_pages()
        if maximum > 1 and not self.footer:
            text = (
                f"Page {menu.current_page + 1}/{maximum} ({len(self.entries)} entries)"
            )
            self.embed.set_footer(text=text)

        return self.embed


class UrbanPageSource(menus.ListPageSource):
    """A page source that requires Dict[Any, Any] tuple items."""

    BRACKETED = re.compile(r"(\[(.+?)\])")

    def __init__(
        self, entries: List[Dict[Any, Any]], *, per_page=12, footer: bool = False
    ):
        super().__init__(entries, per_page=per_page)
        self.footer = footer
        self.embed = discord.Embed(colour=0x2F3136)

    # credit to Danny for this https://github.com/Rapptz/RoboDanny/blob/rewrite/cogs/buttons.py#L50-L58
    def cleanup_definition(self, definition: str, *, regex=BRACKETED) -> str:
        def repl(m):
            word = m.group(2)
            return f'[{word}](http://{word.replace(" ", "-")}.urbanup.com)'

        ret = regex.sub(repl, definition)
        if len(ret) >= 2048:
            return ret[0:2000] + " [...]"
        return ret

    async def format_page(self, menu: Pager, entries: Dict[Any, Any]):
        data = entries[0]
        embed = self.embed
        embed.clear_fields()
        maximum = self.get_max_pages()

        embed.title = data["word"]
        embed.description = self.cleanup_definition(data["definition"])
        embed.timestamp = parse(data["written_on"])

        embed.set_footer(
            text=f"Page {menu.current_page + 1}/{maximum} \nUploaded by {data['author']}"
        )

        return embed


class AvatarsPageSource(menus.ListPageSource):
    """A page source that requires (avatar, created_at) tuple items."""

    def __init__(
        self, entries: List[Tuple[str, datetime.datetime, int]], *, per_page=1
    ):
        super().__init__(entries, per_page=per_page)
        self.embed = discord.Embed(colour=0x2F3136)

    async def format_page(
        self, menu: Pager, entries: Tuple[str, datetime.datetime, int]
    ):
        maximum = self.get_max_pages()

        self.embed.set_footer(
            text=f"Page {menu.current_page + 1}/{maximum} (ID: {entries[2]}) \nChanged"
        )
        self.embed.timestamp = entries[1]
        self.embed.set_image(url=entries[0])

        return self.embed


class GoogleImagePageSource(menus.ListPageSource):
    def __init__(self, entries: List[GoogleImageData], *, per_page=1):
        super().__init__(entries, per_page=per_page)
        self.embed = discord.Embed(colour=0x2F3136)

    async def format_page(self, menu: Pager, entry: GoogleImageData):
        self.embed.clear_fields()

        self.embed.set_image(url=entry.image_url)
        self.embed.set_author(
            name=str(entry.author), icon_url=entry.author.display_avatar.url
        )
        self.embed.title = entry.snippet
        self.embed.url = entry.url

        self.embed.set_footer(
            text=f"Page {menu.current_page + 1}/{self.get_max_pages()} of Google Image search - {entry.query}",
            icon_url="https://cdn.discordapp.com/attachments/1055712784458989598/1061514627093110795/google-go.png",
        )

        return self.embed


class FrontHelpPageSource(menus.ListPageSource):
    def __init__(
        self,
        entries: List[commands.Cog],
        *,
        per_page=12,
        help_command: commands.HelpCommand,
    ):
        super().__init__(entries, per_page=per_page)
        self.help_command = help_command
        self.embed = discord.Embed(colour=0x2F3136)

    async def format_page(self, menu: Pager, entries: List[commands.Cog]):
        self.embed.clear_fields()

        for cog in entries:
            cmds = await self.help_command.filter_commands(cog.get_commands())
            if len(cmds) == 0:
                continue

            if cog is None:
                continue

            self.embed.add_field(
                name=cog.qualified_name.capitalize(),
                value=human_join(
                    [f"**`{command.qualified_name}`**" for command in cmds],
                    final="and",
                )
                or "No commands found here.",
                inline=False,
            )

        maximum = self.get_max_pages()
        if maximum > 1:
            text = (
                f"Page {menu.current_page + 1}/{maximum} ({len(self.entries)} entries)"
            )
            self.embed.set_footer(text=text)

        return self.embed


class ImagePageSource(menus.ListPageSource):
    def __init__(self, entries, *, per_page=1):
        super().__init__(entries, per_page=per_page)
        self.embed = discord.Embed(colour=0x2F3136)

    async def format_page(self, menu: Pager, entries):
        self.embed.clear_fields()
        self.embed.set_image(url=entries)

        maximum = self.get_max_pages()

        if maximum > 1:
            text = (
                f"Page {menu.current_page + 1}/{maximum} ({len(self.entries)} entries)"
            )
            self.embed.set_footer(text=text)

        return self.embed


class TextPageSource(menus.ListPageSource):
    def __init__(self, text, *, prefix="```", suffix="```", max_size=2000):
        pages = CommandPaginator(prefix=prefix, suffix=suffix, max_size=max_size - 200)
        for line in text.split("\n"):
            pages.add_line(line)

        super().__init__(entries=pages.pages, per_page=1)

    async def format_page(self, menu: Pager, content):
        maximum = self.get_max_pages()
        if maximum > 1:
            return f"{content}\nPage {menu.current_page + 1}/{maximum}"
        return content


class SimplePageSource(menus.ListPageSource):
    async def format_page(self, menu, entries):
        pages = []
        for index, entry in enumerate(entries, start=menu.current_page * self.per_page):
            pages.append(f"{index + 1}. {entry}")

        maximum = self.get_max_pages()
        if maximum > 1:
            footer = (
                f"Page {menu.current_page + 1}/{maximum} ({len(self.entries)} entries)"
            )
            menu.embed.set_footer(text=footer)

        menu.embed.description = "\n".join(pages)
        return menu.embed


class SimplePages(Pager):
    """A simple pagination session reminiscent of the old Pages interface.

    Basically an embed with some normal formatting.
    """

    def __init__(self, entries, *, ctx: Context, per_page: int = 12):
        super().__init__(SimplePageSource(entries, per_page=per_page), ctx=ctx)
        self.embed = discord.Embed(colour=discord.Colour.blurple())


class ReviewsPageSource(menus.ListPageSource):
    """A page source that requires (Review) List items."""

    def __init__(self, entries: List[Review], *, per_page=1):
        super().__init__(entries, per_page=per_page)
        self.embed = discord.Embed(colour=0x2F3136)

    async def format_page(self, menu: Pager, entries: Review):
        maximum = self.get_max_pages()
        review = entries
        author = review.sender

        self.embed.set_footer(
            text=f"Page {menu.current_page + 1}/{maximum} (ID: {review.id}) \nReviewed"
        )
        self.embed.description = review.comment
        self.embed.timestamp = datetime.datetime.fromtimestamp(review.timestamp)
        self.embed.set_author(
            name=f"{author.username} ({author.user_id})",
            icon_url=author.profilePhoto,
            url=f"https://reviewdb.mantikafasi.dev/dashboard?query={review.target_id}",
        )

        return self.embed
