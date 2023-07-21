from __future__ import annotations

import json
from io import BytesIO
from typing import TYPE_CHECKING, Any, Dict, Optional, Union

import discord
from discord.ext import commands

from core import Cog
from utils import AllChannels, DiscordObjects

if TYPE_CHECKING:
    from extensions.context import Context, GuildContext


class RawCommands(Cog):
    async def raw_message(self, message: discord.Message) -> Dict[Any, Any]:
        return dict(await self.bot.http.get_message(message.channel.id, message.id))

    async def raw_user(self, user: discord.User) -> Dict[Any, Any]:
        return dict(await self.bot.http.get_user(user.id))

    async def raw_member(self, member: discord.Member) -> Dict[Any, Any]:
        return dict(await self.bot.http.get_member(member.guild.id, member.id))

    async def raw_guild(self, guild: discord.Guild) -> Dict[Any, Any]:
        return dict(await self.bot.http.get_guild(guild.id))

    async def raw_channel(self, channel: AllChannels) -> Dict[Any, Any]:
        return dict(await self.bot.http.get_channel(channel.id))

    @commands.group(name="raw", invoke_without_command=True)
    async def raw(
        self,
        ctx: Context,
        arg: DiscordObjects = commands.param(
            displayed_name="argument", displayed_default="[Object=<Replied Message>]"
        ),
    ):
        print(arg, type(arg))
        if arg is None:
            ref = ctx.message.reference

            if ref is None:
                raise commands.BadArgument(
                    "Please reply to a message or provide an argument to get the raw data from."
                )

            if (
                isinstance(ref.resolved, discord.DeletedReferencedMessage)
                or ref.resolved is None
            ):
                raise commands.BadArgument("Invalid argument provided.")

            arg = ref.resolved

        if isinstance(arg, discord.Message):
            data = await self.raw_message(arg)

        elif isinstance(arg, discord.User):
            await ctx.send("Got user")
            data = await self.raw_user(arg)

        elif isinstance(arg, discord.Member):
            await ctx.send(
                f"Got {type(arg)} \nChannel type: {type(ctx.channel)} \nGuild ID: {arg.guild.id}"
            )
            data = await self.raw_member(arg)

        elif isinstance(arg, discord.Guild):
            data = await self.raw_guild(arg)

        elif isinstance(arg, AllChannels):
            data = await self.raw_channel(arg)

        else:
            raise commands.BadArgument("Invalid argument provided.")

        b = "`" * 3
        data = json.dumps(data, indent=4, sort_keys=True)
        content = f"{b}json\n{data}\n{b}"

        if len(content) > 2000:
            content = "Text too long."
            files = [ctx.too_big(str(data))]

        else:
            files = []

        await ctx.send(content, files=files)

    @raw.command(name="user")
    async def raw_user_command(
        self, ctx: Context, *, user: discord.User = commands.Author
    ):
        await self.raw_user(user)
