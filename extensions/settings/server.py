from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import discord
from discord.ext import commands

from core import Cog
from utils import AuthorView, FieldPageSource, Pager, get_or_fetch_user

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import GuildContext


def to_lower(argument: str):
    return argument.lower()


class Dropdown(discord.ui.ChannelSelect):
    def __init__(self, ctx: GuildContext):
        self.ctx = ctx
        super().__init__(
            placeholder="Choose a channel.",
            min_values=1,
            max_values=1,
            channel_types=[discord.ChannelType.text],
        )

    async def callback(self, interaction: discord.Interaction):
        channel: discord.TextChannel = self.values[0]  # type: ignore

        if self.ctx.bot.settings is None:
            raise commands.BadArgument("Settings cog could not be found somehow.")

        await self.ctx.bot.settings.add_adl_channel(channel)

        if interaction.message is None:
            raise commands.BadArgument(
                "Message is none somehow. However the auto-download channel was set, enjoy."
            )

        await interaction.message.edit(
            content=f"Auto-download channel set to {channel.mention}", view=None
        )
        await interaction.response.defer()


class DropdownView(AuthorView):
    def __init__(self, ctx: GuildContext):
        super().__init__(ctx)

        self.add_item(Dropdown(ctx))


class Server(Cog):
    @commands.hybrid_group(name="prefix", fallback="get", invoke_without_command=True)
    @commands.guild_only()
    async def prefix(self, ctx: GuildContext):
        """Manage the server prefixes"""
        format_dt = discord.utils.format_dt

        sql = """SELECT * FROM guild_prefixes WHERE guild_id = $1 ORDER BY time DESC"""
        records = await ctx.bot.pool.fetch(sql, ctx.guild.id)

        if not bool(records):
            raise commands.BadArgument("This server has no prefixes set.")

        entries = [
            (
                record["prefix"],
                f'{format_dt(record["time"], "R")}  |  {format_dt(record["time"], "d")} | {(await get_or_fetch_user(self.bot, record["author_id"])).mention}',
            )
            for record in records
        ]

        p = FieldPageSource(entries, per_page=4)
        p.embed.title = f"Prefixes in {ctx.guild}"
        menu = Pager(p, ctx=ctx)
        await menu.start(ctx)

    @prefix.command(name="add", aliases=("set", "a", "+"))
    @commands.has_guild_permissions(manage_guild=True, manage_messages=True)
    @commands.guild_only()
    async def prefix_add(
        self, ctx: GuildContext, *, prefix: str = commands.param(converter=to_lower)
    ):
        """Add a prefix to the server"""
        bot = self.bot
        now = discord.utils.utcnow()
        if len(prefix) > 10:
            raise commands.BadArgument("Prefixes can only be 10 characters long.")

        if prefix in await bot.redis.smembers(f"prefixes:{ctx.guild.id}"):
            raise commands.BadArgument("This prefix is already set.")

        sql = """INSERT INTO guild_prefixes (guild_id, prefix, author_id, time) VALUES ($1, $2, $3, $4)"""
        await bot.pool.execute(sql, ctx.guild.id, prefix, ctx.author.id, now)
        await bot.redis.sadd(f"prefixes:{ctx.guild.id}", prefix)
        await ctx.send(f"Added prefix `{prefix}` to the server.")

    @prefix.command(name="remove", aliases=("delete", "r", "d", "del", "-"))
    @commands.has_guild_permissions(manage_guild=True, manage_messages=True)
    @commands.guild_only()
    async def prefix_remove(
        self, ctx: GuildContext, *, prefix: str = commands.param(converter=to_lower)
    ):
        """Remove a prefix from the server"""
        bot = self.bot

        if prefix not in await bot.redis.smembers(f"prefixes:{ctx.guild.id}"):
            raise commands.BadArgument(
                "This prefix does not exist. Check your spelling and try again."
            )

        sql = """DELETE FROM guild_prefixes WHERE guild_id = $1 AND prefix = $2"""
        await bot.pool.execute(sql, ctx.guild.id, prefix)
        await bot.redis.srem(f"prefixes:{ctx.guild.id}", prefix)
        await ctx.send(f"Removed prefix `{prefix}` from the server.")

    async def add_adl_channel(self, channel: discord.TextChannel):
        sql = """
        INSERT INTO guild_settings (guild_id, auto_download) VALUES ($1, $2) 
        ON CONFLICT (guild_id) DO UPDATE
        SET auto_download = $2
        WHERE guild_settings.guild_id = $1
        """

        await self.bot.pool.execute(sql, channel.guild.id, channel.id)
        await self.bot.redis.sadd("auto_downloads", channel.id)

    async def remove_adl_channel(self, channel: discord.TextChannel):
        sql = """UPDATE guild_settings SET auto_download = NULL WHERE guild_id = $1"""

        await self.bot.pool.execute(sql, channel.guild.id)
        await self.bot.redis.srem("auto_downloads", str(channel.id))

    @commands.hybrid_group(
        name="auto-download",
        fallback="set",
        aliases=("adl", "autodownload", "auto_download"),
    )
    @commands.has_guild_permissions(manage_guild=True)
    @commands.guild_only()
    async def auto_download(
        self, ctx: GuildContext, *, channel: Optional[discord.TextChannel] = None
    ):
        """Add a channel to auto-download videos from"""
        sql = """SELECT auto_download FROM guild_settings WHERE guild_id = $1"""
        channel_id: int = await self.bot.pool.fetchval(sql, ctx.guild.id)
        if channel_id:
            raise commands.BadArgument(
                f"An auto-download channel is already set in this server, if you would like to change or remove it please run the command `{ctx.get_prefix}auto-download remove`"
            )

        if not channel:
            return await ctx.send(
                "Choose a channel to enable auto-downloads in", view=DropdownView(ctx)
            )

        await self.add_adl_channel(channel)

        await ctx.send(f"Set auto-download channel to {channel.mention}")

    @auto_download.command(
        name="remove",
        aliases=("r", "delete", "d", "-"),
    )
    @commands.has_guild_permissions(manage_guild=True)
    @commands.guild_only()
    async def auto_download_remove(self, ctx: GuildContext):
        """Add a channel to auto-download videos from"""
        sql = """SELECT auto_download FROM guild_settings WHERE guild_id = $1"""
        channel_id: int = await self.bot.pool.fetchval(sql, ctx.guild.id)

        if not channel_id:
            raise commands.BadArgument(
                f"No auto-download channel found. You may set one with `{ctx.get_prefix}auto-download`"
            )

        channel: discord.TextChannel = self.bot.get_channel(channel_id)  # type: ignore

        await self.remove_adl_channel(channel)
        await ctx.send(f"Removed auto-downloads from {channel.mention}")
