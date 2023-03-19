from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Dict, Optional, Type, TypeAlias, Union

import discord
from discord.ext import commands

from utils import BaseCog, BlankException, Channel, FieldPageSource, Pager, SimplePages

if TYPE_CHECKING:
    from bot import Bot
    from cogs.context import Context

Everything: TypeAlias = Optional[
    Union[
        discord.Member,
        discord.User,
        discord.Message,
        discord.Guild,
        Channel,
    ]
]


class OtherCommands(BaseCog):
    def __init__(self, bot: Bot):
        self.bot = bot

    @commands.group(name="emojis", invoke_without_command=True)
    async def emojis(self, ctx: Context, guild: Optional[discord.Guild] = None):
        """Get the server emojis."""
        guild = guild or ctx.guild

        if not guild:
            raise commands.GuildNotFound("Unknown guild")

        if not guild.emojis:
            raise commands.GuildNotFound("Guild has no emojis")

        if ctx.bot.user is None:
            raise TypeError("Bot is not logged in.")

        order = sorted(guild.emojis, key=lambda e: e.created_at)

        data = [f"{str(e)} `<:{e.name}\u200b:{e.id}>`" for e in order]
        pages = SimplePages(entries=data, per_page=10, ctx=ctx)
        pages.embed.title = f"Emojis for {guild.name}"
        await pages.start(ctx)

    @emojis.command(name="advanced")
    async def emojis_advanced(
        self, ctx: Context, guild: Optional[discord.Guild] = None
    ):
        """Get the server emojis."""
        guild = guild or ctx.guild

        if not guild:
            raise commands.GuildNotFound("Unknown guild")

        if not guild.emojis:
            raise commands.GuildNotFound("Guild has no emojis")

        if ctx.bot.user is None:
            raise TypeError("Bot is not logged in.")

        order = sorted(guild.emojis, key=lambda e: e.created_at)

        entries = [
            (
                e.name,
                f'Preview: {str(e)} \nCreated: {discord.utils.format_dt(e.created_at, "D")} \nRaw: `<:{e.name}\u200b:{e.id}>`',
            )
            for e in order
        ]

        p = FieldPageSource(entries, per_page=2)
        p.embed.title = f"Emojis in {guild.name}"
        menu = Pager(p, ctx=ctx)
        await menu.start(ctx)

    async def do_raw_command(
        self,
        ctx: Context,
        argument: Everything,
    ):
        if argument is None:
            if ctx.message.reference is None or not isinstance(
                ctx.message.reference.resolved, discord.Message
            ):
                raise BlankException(
                    "Please reply to a message to get the raw data from it or supply a valid argument."
                )

            argument = ctx.message.reference.resolved

        items: Dict[Type, Dict[Any, Any]] = {
            discord.User: {"func": self.bot.http.get_user, "args": (argument.id,)},
            discord.Member: {
                "func": self.bot.http.get_member,
                "args": (argument.guild.id, argument.id)
                if isinstance(argument, discord.Member)
                else (argument.id,),
            },
            discord.Message: {
                "func": self.bot.http.get_message,
                "args": (argument.channel.id, argument.id)
                if isinstance(argument, discord.Message)
                else (argument.id,),
            },
            discord.Guild: {"func": self.bot.http.get_guild, "args": (argument.id,)},
        }

        if isinstance(argument, Channel):
            function, args = self.bot.http.get_channel, (argument.id,)
        else:
            data = items[type(argument)]
            function, args = data.get("func"), data.get("args")

        if function is None or args is None:
            raise BlankException("Couldn't get function, for some reason,,,")

        data: Dict[Any, Any] = await function(*args)  # type: ignore

        to_send = json.dumps(data, indent=4, sort_keys=True)

        header = (
            f"Viewing raw message data"
            if isinstance(argument, discord.Message)
            else f"Viewing raw data for {str(argument)}"
        )

        if len(to_send) + len(header) + 12 + len(str(argument)) > 2000:
            file = ctx.too_big(to_send)
            return await ctx.send("File too large", file=file)

        await ctx.send(f"```{header}``````json\n{to_send}\n```")

    @commands.group(name="raw", invoke_without_command=True)
    @commands.cooldown(1, 15, commands.BucketType.user)
    async def raw(self, ctx: Context, *, argument: Everything):
        """Gets the raw data for an object"""
        await self.do_raw_command(ctx, argument)

    @raw.command(name="user")
    async def raw_user(self, ctx: Context, *, user: discord.User = commands.Author):
        """Gets the raw data for a user"""
        await self.do_raw_command(ctx, user)
