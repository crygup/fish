from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

import asyncpg
import discord
from discord.ext import commands

from utils import (
    FieldPageSource,
    Pager,
    add_prefix,
    get_or_fetch_user,
    BlankException,
    DoNothing,
    BaseCog,
)


if TYPE_CHECKING:
    from cogs.context import Context


class ServerSettings(BaseCog):
    @commands.command(
        name="fm-reactions",
        invoke_without_command=True,
        aliases=("fmr", "fm_reactions"),
    )
    async def fm_reactions(self, ctx: Context):
        """Toggles auto reactions to fish fm commands for yourself."""

        sql = """
        INSERT INTO user_settings (user_id, fm_autoreact) VALUES ($1, $2)
        ON CONFLICT (user_id) DO UPDATE
        SET fm_autoreact = NOT user_settings.fm_autoreact
        WHERE user_settings.user_id = $1
        RETURNING *
        """
        results = await self.bot.pool.fetchrow(sql, ctx.author.id, True)

        if results is None:
            raise DoNothing()

        value: bool = results["fm_autoreact"]

        if value:
            await self.bot.redis.sadd("fm_autoreactions", ctx.author.id)
        else:
            await self.bot.redis.srem("fm_autoreactions", ctx.author.id)

        await ctx.send(
            f"{'Enabled' if value else 'Disabled'} auto-reactions on the fm command for you."
        )

    @commands.group(
        name="auto-reactions",
        invoke_without_command=True,
        aliases=("ar", "auto_reactions"),
    )
    @commands.has_guild_permissions(manage_guild=True)
    @commands.bot_has_guild_permissions(add_reactions=True)
    async def auto_reactions(self, ctx: Context):
        """Toggles auto reactions to media posts in this server."""

        sql = """
        INSERT INTO guild_settings (guild_id, auto_reactions) VALUES ($1, $2)
        ON CONFLICT (guild_id) DO UPDATE
        SET auto_reactions = NOT guild_settings.auto_reactions
        WHERE guild_settings.guild_id = $1
        RETURNING *
        """
        results = await self.bot.pool.fetchrow(sql, ctx.guild.id, True)

        if results is None:
            raise DoNothing()

        value: bool = results["auto_reactions"]

        if value:
            await self.bot.redis.sadd("auto_reactions", ctx.guild.id)
        else:
            await self.bot.redis.srem("auto_reactions", ctx.guild.id)

        await ctx.send(
            f"{'Enabled' if value else 'Disabled'} auto-reactions in this server."
        )

    @commands.command(
        name="auto-solve",
        aliases=("as", "auto_solve"),
        extras={"UPerms": ["Manage Server"]},
    )
    @commands.has_permissions(manage_guild=True)
    async def auto_solve(self, ctx: Context):
        """Toggles automatic solving of pokétwo's pokémon hints"""

        try:
            sql = "INSERT INTO guild_settings(guild_id, poketwo) VALUES($1, $2)"
            await self.bot.pool.execute(sql, ctx.guild.id, True)
        except asyncpg.UniqueViolationError:
            if str(ctx.guild.id) in await self.bot.redis.smembers("poketwo_guilds"):
                sql = "UPDATE guild_settings SET poketwo = NULL WHERE guild_id = $1"
                await self.bot.pool.execute(sql, ctx.guild.id)
                await self.bot.redis.srem("poketwo_guilds", ctx.guild.id)
                await ctx.send("Disabled auto solving for this server.")
                return

            sql = "UPDATE guild_settings SET poketwo = $1 WHERE guild_id = $2"
            await self.bot.pool.execute(sql, True, ctx.guild.id)

        await self.bot.redis.sadd("poketwo_guilds", ctx.guild.id)
        await ctx.send("Enabled auto solving for this server.")

    @commands.group(name="prefix", invoke_without_command=True)
    async def prefix(self, ctx: Context):
        """Set the prefix for the bot"""
        sql = """SELECT * FROM guild_prefixes WHERE guild_id = $1 ORDER BY time DESC"""
        results = await ctx.bot.pool.fetch(sql, ctx.guild.id)

        if results == []:
            await ctx.send("This server has no prefixes set.")
            return

        entries = [
            (
                record["prefix"],
                f'{discord.utils.format_dt(record["time"], "R")}  |  {discord.utils.format_dt(record["time"], "d")} | {(await get_or_fetch_user(self.bot, record["author_id"])).mention}',
            )
            for record in results
        ]

        p = FieldPageSource(entries, per_page=4)
        p.embed.title = f"Prefixes in {ctx.guild}"
        menu = Pager(p, ctx=ctx)
        await menu.start(ctx)

    @prefix.command(name="add", aliases=("set", "setprefix", "+"))
    @commands.has_permissions(manage_guild=True)
    async def prefix_add(self, ctx: Context, *, prefix: str):
        """Add a prefix to the server"""
        if len(prefix) > 10:
            await ctx.send("Prefixes can only be 10 characters long.")
            return

        try:
            check = self.bot.prefixes[ctx.guild.id]
            if prefix in check:
                await ctx.send("This prefix is already set.")
                return

        except KeyError:
            pass

        sql = """INSERT INTO guild_prefixes (guild_id, prefix, author_id, time) VALUES ($1, $2, $3, $4)"""
        await ctx.bot.pool.execute(
            sql, ctx.guild.id, prefix, ctx.author.id, discord.utils.utcnow()
        )
        add_prefix(self.bot, ctx.guild.id, prefix)
        await ctx.send(f"Added prefix `{prefix}` to the server.")

    @prefix.command(name="remove", aliases=("delete", "del", "rm", "-"))
    @commands.has_permissions(manage_guild=True)
    async def prefix_remove(self, ctx: Context, *, prefix: str):
        """Remove a prefix from the server"""

        try:
            sql = """DELETE FROM guild_prefixes WHERE guild_id = $1 AND prefix = $2"""
            await ctx.bot.pool.execute(sql, ctx.guild.id, prefix)
            self.bot.prefixes[ctx.guild.id].remove(prefix)
            await ctx.send(f"Removed prefix `{prefix}` from the server.")
        except (KeyError, ValueError):
            await ctx.send("This prefix does not exist.")
            return

    @commands.group(
        name="auto-download", aliases=("auto-dl", "adl"), invoke_without_command=True
    )
    async def auto_download(self, ctx: Context):
        sql = """SELECT auto_download FROM guild_settings WHERE guild_id = $1"""
        results = await self.bot.pool.fetchval(sql, ctx.guild.id)

        if not results:
            message = "This server does not have an auto-download channel set yet."

            if (
                isinstance(ctx.author, discord.Member)
                and ctx.author.guild_permissions.manage_guild
            ):
                message += f"\nYou can set one with `{ctx.prefix}auto_download set`."

            await ctx.send(message)
            return

        channel = self.bot.get_channel(results)
        if not channel or not isinstance(channel, discord.TextChannel):
            return

        await ctx.send(f"Auto-download is set to {channel.mention}.")

    @auto_download.command(
        name="set",
        aliases=("create", "add", "make"),
        extras={"UPerms": ["Manage Server"]},
    )
    @commands.has_guild_permissions(manage_guild=True)
    async def auto_download_set(self, ctx: Context, channel: discord.TextChannel):
        adl_channels = await self.bot.redis.smembers("auto_download_channels")

        if str(channel.id) in adl_channels:
            return await ctx.send(f"Auto-download is already setup here.")

        if not channel.permissions_for(ctx.me).send_messages:
            raise BlankException(
                f"I don't have permission to send messages in {channel.mention}."
            )

        sql = """
        INSERT INTO guild_settings (guild_id, auto_download) 
        VALUES ($1, $2)
        ON CONFLICT(guild_id) DO UPDATE 
        SET auto_download = $2 
        WHERE guild_settings.guild_id = $1
        """

        await self.bot.pool.execute(sql, ctx.guild.id, channel.id)
        await self.bot.redis.sadd("auto_download_channels", channel.id)
        await ctx.send(f"Auto-download is now set to {channel.mention}.")

    @auto_download.command(
        name="remove", aliases=("delete",), extras={"UPerms": ["Manage Server"]}
    )
    @commands.has_guild_permissions(manage_guild=True)
    async def auto_download_remove(self, ctx: Context):
        sql = """SELECT auto_download FROM guild_settings WHERE guild_id = $1"""
        result: int = await self.bot.pool.fetchval(sql, ctx.guild.id)

        if not result:
            raise BlankException(
                "This server does not have an auto-download channel set yet."
            )

        channel: discord.TextChannel = self.bot.get_channel(result)  # type: ignore

        results = await ctx.prompt(
            f"Are you sure you want to remove auto-downloads from {channel.mention}?"
        )
        if not results:
            return await ctx.send(f"Well I didn't want to remove it anyway.")

        sql = """UPDATE guild_settings SET auto_download = NULL WHERE guild_id = $1"""
        await self.bot.pool.execute(sql, ctx.guild.id)
        await self.bot.redis.srem("auto_download_channels", str(result))
        await ctx.send(f"Removed auto-downloads for this server.")
