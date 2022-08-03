from __future__ import annotations

import asyncio
import subprocess
import textwrap
from copy import deepcopy
from io import BytesIO, StringIO
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    ParamSpec,
    TypeVar,
    Union,
)

import discord
from aiohttp import ClientSession
from asyncpg import Connection, Pool
from discord.ext import commands
from wand.image import Image as wImage

T = TypeVar("T")
P = ParamSpec("P")


def to_thread(func: Callable[P, T]) -> Callable[P, Awaitable[T]]:
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        return await asyncio.to_thread(func, *args, **kwargs)

    return wrapper


if TYPE_CHECKING:
    from bot import Bot


VALID_EDIT_KWARGS: Dict[str, Any] = {
    "content": None,
    "embeds": [],
    "attachments": [],
    "suppress": False,
    "delete_after": None,
    "allowed_mentions": None,
    "view": None,
}


class ContextCog(commands.Cog, name="context"):
    def __init__(self, bot: Bot):
        self.bot: Bot = bot

    async def cog_unload(self) -> None:
        self.bot._context = commands.Context  # type: ignore

    async def cog_load(self) -> None:
        self.bot._context = Context


async def setup(bot: Bot):
    bot.messages.clear()
    await bot.add_cog(ContextCog(bot))


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
            await interaction.delete_original_message()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        await interaction.response.defer()
        if self.delete_after:
            await interaction.delete_original_message()
        self.stop()


class Context(commands.Context):
    prefix: str
    command: commands.Command[Any, ..., Any]
    bot: Bot
    guild: discord.Guild  # due to our check guild will never be None

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.pool = self.bot.pool
        self._db: Optional[Union[Pool, Connection]] = None
        self._message_count: int = 0

    @property
    def db(self) -> Union[Pool, Connection]:
        return self._db if self._db else self.pool

    def too_big(self, text: str) -> discord.File:
        s = StringIO()
        s.write(text)
        s.seek(0)
        file = discord.File(s, "large.txt")  # type: ignore
        return file

    def tick(self, opt: Optional[bool], label: Optional[str] = None) -> str:
        lookup = {
            True: "<:cr_check:956022530521563136>",
            False: "<:cr_warning:956384262016344064>",
            None: "\U00002b1c",
        }
        emoji = lookup.get(opt, "<:cr_warning:956384262016344064>")
        if label is not None:
            return f"{emoji}: {label}"
        return emoji

    def yes_no(self, value: bool) -> str:
        return "Yes" if value else "No"

    async def prompt(
        self,
        message: str,
        *,
        timeout: float = 60.0,
        delete_after: bool = True,
        author_id: Optional[int] = None,
    ) -> Optional[bool]:
        """An interactive reaction confirmation dialog.
        Parameters
        -----------
        message: str
            The message to show along with the prompt.
        timeout: float
            How long to wait before returning.
        delete_after: bool
            Whether to delete the confirmation message after we're done.
        reacquire: bool
            Whether to release the database connection and then acquire it
            again when we're done.
        author_id: Optional[int]
            The member who should respond to the prompt. Defaults to the author of the
            Context's message.
        Returns
        --------
        Optional[bool]
            ``True`` if explicit confirm,
            ``False`` if explicit deny,
            ``None`` if deny due to timeout
        """

        author_id = author_id or self.author.id
        view = ConfirmationView(
            timeout=timeout,
            delete_after=delete_after,
            ctx=self,
            author_id=author_id,
        )
        view.message = await self.send(message, view=view)
        await view.wait()
        return view.value

    async def get_twemoji(self, emoji: str, *, svg: bool = True) -> Optional[bytes]:
        try:
            folder = ("72x72", "svg")[svg]
            ext = ("png", "svg")[svg]
            url = f"https://twemoji.maxcdn.com/v/latest/{folder}/{ord(emoji):x}.{ext}"

            async with self.bot.session.get(url) as r:
                if r.ok:
                    byt = await r.read()
                    if svg:
                        return await self.svgbytes_to_btyes(byt)
                    else:
                        return byt
        except Exception:
            return None

    @to_thread
    def svgbytes_to_btyes(self, svg: bytes) -> bytes:
        with wImage(
            blob=svg, format="svg", width=500, height=500, background="none"
        ) as asset:
            _img = asset.make_blob("png")

        if _img is not None:
            return _img

        raise TypeError("Failed to convert svg to bytes")

    async def to_image(
        self, url: str, byte: bool = False, skip_check: bool = False
    ) -> BytesIO | bytes:
        from utils import response_checker

        async with self.bot.session.get(url) as resp:
            if not skip_check:
                response_checker(resp)
            data = await resp.read()

        return data if byte else BytesIO(data)

    @property
    def session(self) -> ClientSession:
        return self.bot.session

    async def delete(
        self, message: discord.Message, delay: Optional[int] = None
    ) -> None:
        try:
            if delay is not None:
                await message.delete(delay=delay)
            else:
                await message.delete()
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            pass

    async def run_process(self, command: str) -> list[str]:
        try:
            process = await asyncio.create_subprocess_shell(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            result = await process.communicate()
        except NotImplementedError:
            process = subprocess.Popen(
                command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            result = await self.bot.loop.run_in_executor(None, process.communicate)

        return [output.decode() for output in result]

    @property
    def sus_guilds(self) -> List[discord.Guild]:
        return [
            guild
            for guild in self.bot.guilds
            if sum(1 for m in guild.members if m.bot)
            > sum(1 for m in guild.members if not m.bot)
        ]

    async def show_help(self, command: Optional[commands.Command] = None):
        help = self.bot.get_command("help")

        if help is None:
            raise commands.CommandNotFound("Help command not found!")

        await self.invoke(help, command=command)  # type: ignore

    async def send(
        self,
        content: Optional[str] = None,
        reference: Optional[Union[discord.Message, discord.MessageReference]] = None,
        check_ref: Optional[bool] = False,
        **kwargs: Any,
    ) -> discord.Message:

        reference = reference or self.message.reference or None

        if self.guild is None:
            return  # type: ignore

        if not self.channel.permissions_for(self.guild.me).send_messages:
            try:
                return await self.author.send(f"I do not have permissions to send messages in {self.channel.mention}.")  # type: ignore
            except discord.Forbidden:
                pass

        if kwargs.get("embed") and kwargs.get("embeds"):
            raise TypeError("Cannot mix embed and embeds keyword arguments.")

        if check_ref:
            async for message in self.channel.history(limit=1):
                if message.id != self.message.id:
                    reference = self.message if not reference else reference

        embeds = kwargs.pop("embeds", []) or (
            [kwargs.pop("embed")] if kwargs.get("embed", None) else []
        )
        if embeds:
            for embed in embeds:
                if embed.color is None:
                    embed.color = self.bot.embedcolor

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
            return f"<utils.Context bound to message ({self.channel.id}-{self.message.id}-{self._message_count})>"
        elif self.interaction:
            return f"<utils.Context bound to interaction {self.interaction}>"
        return super().__repr__()
