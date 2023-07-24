from __future__ import annotations

import re
from copy import deepcopy
from io import StringIO
from typing import TYPE_CHECKING, Any, Callable, Dict, Generic, List, Optional, TypeVar

import aiohttp
import discord
from discord.context_managers import Typing
from discord.ext import commands
from discord.ext.commands.context import DeferTyping

if TYPE_CHECKING:
    from core import Fishie

T = TypeVar("T")

VALID_EDIT_KWARGS: Dict[str, Any] = {
    "content": None,
    "embeds": [],
    "attachments": [],
    "suppress": False,
    "delete_after": None,
    "allowed_mentions": None,
    "view": None,
}


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
        self._message_count: int = 0
        self.pool = self.bot.pool
        self.redis = self.bot.redis

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

    def too_big(self, text: str) -> discord.File:
        s = StringIO()
        s.write(text)
        s.seek(0)
        file = discord.File(s, "large.txt")  # type: ignore
        return file

    @property
    def ref(self) -> Optional[discord.Message]:
        ref = self.message.reference

        if not ref:
            return None

        if isinstance(ref.resolved, discord.DeletedReferencedMessage):
            return None

        return ref.resolved

    # thanks leo
    async def send(
        self, content: str | None = None, *args: Any, **kwargs: Any
    ) -> discord.Message:
        if kwargs.get("embed") and kwargs.get("embeds"):
            raise TypeError("Cannot mix embed and embeds keyword arguments.")

        embeds = kwargs.pop("embeds", []) or (
            [kwargs.pop("embed")] if kwargs.get("embed", None) else []
        )

        kwargs["embeds"] = embeds

        if self._previous_message:
            new_kwargs = deepcopy(VALID_EDIT_KWARGS)
            new_kwargs["content"] = content
            new_kwargs.update(kwargs)
            edit_kw = {k: v for k, v in new_kwargs.items() if k in VALID_EDIT_KWARGS}
            attachments = new_kwargs.pop("files", []) or (
                [new_kwargs.pop("file")] if new_kwargs.get("file", None) else []
            )

            if attachments:
                edit_kw["attachments"] = attachments
                new_kwargs["files"] = attachments

            try:
                m = await self._previous_message.edit(**edit_kw)
                self._previous_message = m
                self._message_count += 1
                return m
            except discord.HTTPException:
                self._previous_message = None
                self._previous_message = m = await super().send(content, **kwargs)
                return m

        self._previous_message = m = await super().send(content, **kwargs)
        self._message_count += 1
        return m

    @property
    def _previous_message(self) -> Optional[discord.Message]:
        if self.message:
            try:
                return self.bot.messages[repr(self)]
            except KeyError:
                return None

    @_previous_message.setter
    def _previous_message(self, message: Optional[discord.Message]) -> None:
        if isinstance(message, discord.Message):
            self.bot.messages[repr(self)] = message
        else:
            self.bot.messages.pop(repr(self), None)

    def __repr__(self) -> str:
        if self.message:
            return f"<extensions.context bound to message ({self.channel.id}-{self.message.id}-{self._message_count})>"
        elif self.interaction:
            return f"<extensions.context bound to interaction {self.interaction}>"

        return super().__repr__()


class GuildContext(Context):
    author: discord.Member
    channel: discord.abc.GuildChannel
    guild: discord.Guild


async def setup(bot: "Fishie") -> None:
    bot.context_cls = Context


async def teardown(bot: Fishie) -> None:
    bot.context_cls = commands.Context
