from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Callable, Generic, Optional, TypeVar, Union

import aiohttp
import discord
from discord.context_managers import Typing
from discord.ext import commands
from discord.ext.commands.context import DeferTyping

if TYPE_CHECKING:
    from core import Fishie

T = TypeVar("T")


class ConfirmationView(discord.ui.View):
    def __init__(
        self, *, timeout: float, author_id: int, ctx: Context, delete_after: bool
    ) -> None:
        super().__init__(timeout=timeout)
        self.value: Optional[bool] = None
        self.delete_after: bool = delete_after
        self.author_id: int = author_id
        self.ctx: Context = ctx
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user and interaction.user.id == self.author_id:
            return True

        await interaction.response.send_message(
            "This confirmation dialog is not for you.", ephemeral=True
        )
        return False

    async def on_timeout(self) -> None:
        if self.delete_after and self.message:
            await self.message.delete()

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.green)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        self.value = True
        await interaction.response.defer()
        if self.delete_after:
            await interaction.delete_original_response()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        await interaction.response.defer()
        if self.delete_after:
            await interaction.delete_original_response()
        self.stop()


class DisambiguatorView(discord.ui.View, Generic[T]):
    message: discord.Message
    selected: T

    def __init__(self, ctx: Context, data: list[T], entry: Callable[[T], Any]):
        super().__init__()
        self.ctx: Context = ctx
        self.data: list[T] = data

        options = []
        for i, x in enumerate(data):
            opt = entry(x)
            if not isinstance(opt, discord.SelectOption):
                opt = discord.SelectOption(label=str(opt))
            opt.value = str(i)
            options.append(opt)

        select = discord.ui.Select(options=options)

        select.callback = self.on_select_submit
        self.select = select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user and interaction.user.id == self.ctx.author.id:
            return True

        await interaction.response.send_message(
            "This confirmation dialog is not for you.", ephemeral=True
        )
        return False

    async def on_select_submit(self, interaction: discord.Interaction):
        index = int(self.select.values[0])
        self.selected = self.data[index]
        await interaction.response.defer()
        if not self.message.flags.ephemeral:
            await self.message.delete()

        self.stop()


class Context(commands.Context["Fishie"]):
    session: aiohttp.ClientSession
    bot: "Fishie"

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.session = self.bot.session

    async def prompt(
        self,
        message: str,
        *,
        timeout: float = 60.0,
        delete_after: bool = True,
        author_id: Optional[int] = None,
        **kwargs,
    ) -> Optional[discord.Message]:
        author_id = author_id or self.author.id
        view = ConfirmationView(
            timeout=timeout,
            delete_after=delete_after,
            ctx=self,
            author_id=author_id,
        )
        view.message = await self.send(message, view=view, **kwargs)
        await view.wait()
        if view.value:
            return view.message
        else:
            try:
                await view.message.delete()
            except:
                pass

            return None

    async def disambiguate(
        self, matches: list[T], entry: Callable[[T], Any], *, ephemeral: bool = False
    ) -> T:
        if len(matches) == 0:
            raise ValueError("No results found.")

        if len(matches) == 1:
            return matches[0]

        if len(matches) > 25:
            raise ValueError("Too many results... sorry.")

        view = DisambiguatorView(self, matches, entry)
        view.message = await self.send(
            "There are too many matches... Which one did you mean?",
            view=view,
            ephemeral=ephemeral,
        )
        await view.wait()
        return view.selected

    async def trigger_typing(
        self, *, ephemeral: bool = False
    ) -> Typing | DeferTyping | None:
        try:
            return super().typing(ephemeral=ephemeral)
        except:
            return

    @property
    def get_prefix(self):
        prefix = self.prefix
        if re.search(f"^{prefix} ", self.message.content):
            return f"{prefix} "

        return prefix


class GuildContext(Context):
    author: discord.Member
    channel: discord.abc.GuildChannel
    guild: discord.Guild


async def setup(bot: "Fishie") -> None:
    bot.context_cls = Context


async def teardown(bot: Fishie) -> None:
    bot.context_cls = commands.Context
