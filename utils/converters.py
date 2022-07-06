from __future__ import annotations

from typing import Generic, Type, TypeVar

import discord
from bot import Bot
from discord.ext import commands
from discord.ext.commands import FlagConverter

from .helpers import Regexes

FCT = TypeVar("FCT", bound="FlagConverter")

__all__ = ["UntilFlag", "TenorUrlConverter"]


class UntilFlag(Generic[FCT]):
    def __init__(self, value: str, flags: FCT) -> None:
        self.value = value
        self.flags = flags
        self._regex = self.flags.__commands_flag_regex__  # type: ignore

    def __class_getitem__(cls, item: Type[FlagConverter]) -> UntilFlag:
        return cls(value="...", flags=item())

    def validate_value(self, argument: str) -> bool:
        stripped = argument.strip()
        if not stripped:
            raise commands.BadArgument(f"No body has been specified before the flags.")
        return True

    async def convert(self, ctx: commands.Context, argument: str) -> UntilFlag:
        value = self._regex.split(argument, maxsplit=1)[0]
        if not await discord.utils.maybe_coroutine(self.validate_value, argument):
            raise commands.BadArgument("Failed to validate argument preceding flags.")
        flags = await self.flags.convert(ctx, argument=argument[len(value) :])
        return UntilFlag(value=value, flags=flags)


class TenorUrlConverter(commands.Converter):
    async def convert(self, ctx: commands.Context[Bot], url: str) -> str:
        response = await ctx.bot.session.get(url)

        failed = commands.BadArgument("An Error occured when fetching the tenor GIF")

        try:
            content = await response.text()
            if match := Regexes.TENOR_GIF_REGEX.search(content):
                async with ctx.bot.session.get(match.group()) as gif:
                    if gif.ok:
                        return str(gif.url)
                    else:
                        raise failed
            else:
                raise failed

        except Exception:
            raise failed
