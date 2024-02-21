from __future__ import annotations

import asyncio
import datetime
import re
from typing import (TYPE_CHECKING, Any, Dict, List, Optional, Protocol, Tuple,
                    runtime_checkable)

import discord
from dateutil.parser import parse
from discord.ext import commands, menus
from discord.ext.commands import Paginator as CommandPaginator

from .emojis import (fish_check, fish_gopage, fish_next, fish_previous,
                     fish_trash)
from .functions import human_join
from .vars import GoogleImageData

if TYPE_CHECKING:
    from extensions.context import Context

blurple = discord.ButtonStyle.blurple
red = discord.ButtonStyle.red


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
            await self.message.edit(view=self)

    async def show_checked_page(
        self, interaction: discord.Interaction, page_number: int
    ) -> None:
        max_pages = self.source.get_max_pages()
        try:
            if max_pages is None:
                await self.show_page(interaction, page_number)
            elif max_pages > page_number >= 0:
                await self.show_page(interaction, page_number)
        except IndexError:
            pass

    def _update_labels(self, page_number: int) -> None:
        self.go_to_next_page.disabled = False
        self.go_to_previous_page.disabled = False
        self.go_to_number_page.disabled = False

        max_pages = self.source.get_max_pages()
        if max_pages is not None:
            if (page_number + 1) >= max_pages:
                self.go_to_next_page.disabled = True
            if page_number == 0:
                self.go_to_previous_page.disabled = True
            if page_number == 0 and (page_number + 1) >= max_pages:
                self.go_to_number_page.disabled = True

    async def start(self, ctx: Context, e=False):
        await self.source._prepare_once()
        page = await self.source.get_page(0)
        kwargs = await self._get_kwargs_from_page(page)
        self._update_labels(0)
        self.message = await self.ctx.send(**kwargs, view=self)

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
            if interaction.response.is_done():
                if self.message:
                    await self.message.edit(**kwargs, view=self)
            else:
                await interaction.response.edit_message(**kwargs, view=self)

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user and interaction.user == self.ctx.author:
            return True
        await interaction.response.send_message(
            f'You can\'t use this, sorry. \nIf you\'d like to use this then run the command `{self.ctx.command}{self.ctx.invoked_subcommand or ""}`',
            ephemeral=True,
        )
        return False

    @discord.ui.button(emoji=fish_previous, style=blurple)
    async def go_to_previous_page(self, interaction: discord.Interaction, __):
        """go to the previous page"""
        await self.show_checked_page(interaction, self.current_page - 1)

    @discord.ui.button(emoji=fish_next, style=blurple)
    async def go_to_next_page(self, interaction: discord.Interaction, __):
        """go to the next page"""
        await self.show_checked_page(interaction, self.current_page + 1)

    @discord.ui.button(emoji=fish_gopage, style=blurple)
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
                        "Please enter a valid number", ephemeral=True
                    )
                    return

                if max_pages and page > max_pages or page < 1:
                    await interaction.response.send_message(
                        f"Page **{page}** does not exist", ephemeral=True
                    )
                    return

                await menu.show_page(interaction, page - 1)

        await interaction.response.send_modal(GoPage())

    @discord.ui.button(emoji=fish_trash, style=red)
    async def stop_pages(self, interaction: discord.Interaction, __):
        """stops the pagination session."""
        await interaction.response.defer()
        await interaction.delete_original_response()
        await self.ctx.message.add_reaction(fish_check)
        self.stop()


class FieldPageSource(menus.ListPageSource):
    """A page source that requires (field_name, field_value) tuple items."""

    def __init__(
        self, entries: List[Tuple[str, str]], *, per_page=12, footer: bool = False
    ):
        super().__init__(entries, per_page=per_page)
        self.footer = footer
        self.embed = discord.Embed(colour=0x2F3136)

    async def format_page(self, menu, entries: Tuple[str, str]):
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

    async def format_page(self, menu, entries: Dict[Any, Any]):
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

    async def format_page(self, menu, entries: Tuple[str, datetime.datetime, int]):
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

    async def format_page(self, menu, entry: GoogleImageData):
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

    async def format_page(self, menu, entries: List[commands.Cog]):
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

    async def format_page(self, menu, entries):
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

    async def format_page(self, menu, content):
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
