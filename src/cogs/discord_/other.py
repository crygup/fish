from __future__ import annotations

import json
from typing import TYPE_CHECKING, Optional, Union

import discord
from discord.ext import commands

from utils import FieldPageSource, Pager, SimplePages

from ._base import CogBase

if TYPE_CHECKING:
    from bot import Bot
    from cogs.context import Context


class OtherCommands(CogBase):
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

    @commands.group(name="raw")
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def raw(self, ctx: Context):
        """Gets the raw data for an object"""

        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @raw.command(name="user")
    async def raw_user(self, ctx: Context, user: discord.User = commands.Author):
        """Gets the raw data for a user"""
        data = await self.bot.http.get_user(user.id)
        to_send = json.dumps(data, indent=4, sort_keys=True)

        if len(to_send) + 50 + len(str(user)) > 2000:
            file = ctx.too_big(to_send)
            await ctx.send("File too large", file=file)
            return

        await ctx.send(f"```Viewing raw data for {str(user)}``````json\n{to_send}\n```")

    @raw.command(name="member")
    async def raw_member(self, ctx: Context, member: discord.Member = commands.Author):
        """Gets the raw data for a member"""
        data = await self.bot.http.get_member(member.guild.id, member.id)
        to_send = json.dumps(data, indent=4, sort_keys=True)

        if len(to_send) + 50 + len(str(member)) > 2000:
            file = ctx.too_big(to_send)
            await ctx.send("File too large", file=file)
            return

        await ctx.send(
            f"```Viewing raw data for {str(member)}``````json\n{to_send}\n```"
        )

    @raw.command(name="message", aliases=("msg",))
    async def raw_message(self, ctx: Context):
        """Gets the raw data for a message"""

        if ctx.message.reference is None:
            return

        message = ctx.message.reference.resolved

        data = await self.bot.http.get_message(message.channel.id, message.id)  # type: ignore
        to_send = json.dumps(data, indent=4, sort_keys=True)

        if len(to_send) + 50 + len(str(message)) > 2000:
            file = ctx.too_big(to_send)
            await ctx.send("File too large", file=file)
            return

        await ctx.send(f"```json\n{to_send}\n```")

    @raw.command(name="guild")
    async def raw_guild(
        self, ctx: Context, guild: discord.Guild = commands.CurrentGuild
    ):
        """Gets the raw data for a guild"""

        data = await self.bot.http.get_guild(guild.id)
        to_send = json.dumps(data, indent=4, sort_keys=True)

        if len(to_send) + 50 + len(str(guild)) > 2000:
            file = ctx.too_big(to_send)
            await ctx.send("File too large", file=file)
            return

        await ctx.send(
            f"```Viewing raw data for {str(guild)}``````json\n{to_send}\n```"
        )

    @raw.command(name="channel")
    async def raw_channel(
        self,
        ctx: Context,
        channel: Union[
            discord.TextChannel,
            discord.VoiceChannel,
            discord.CategoryChannel,
            discord.StageChannel,
            discord.ForumChannel,
            discord.Thread,
        ] = commands.CurrentChannel,
    ):
        """Gets the raw data for a channel"""
        data = await self.bot.http.get_channel(channel.id)
        to_send = json.dumps(data, indent=4, sort_keys=True)

        if len(to_send) + 50 + len(str(channel)) > 2000:
            file = ctx.too_big(to_send)
            await ctx.send("File too large", file=file)
            return

        await ctx.send(f"```Viewing raw data for {channel}``````json\n{to_send}\n```")
