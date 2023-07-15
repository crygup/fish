from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from core import Cog

from utils import FieldPageSource, Pager, get_or_fetch_user

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context, GuildContext

def to_lower(argument: str):
    return argument.lower()

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

    @prefix.command(name='add', aliases=("set", "a", "+"))
    @commands.has_guild_permissions(manage_guild=True, manage_messages=True)
    @commands.guild_only()
    async def prefix_add(self, ctx: GuildContext, *, prefix: str = commands.param(converter=to_lower)):
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

    @prefix.command(name='remove', aliases=("delete", "r", "d", "del", "-"))
    @commands.has_guild_permissions(manage_guild=True, manage_messages=True)
    @commands.guild_only()
    async def prefix_remove(self, ctx: GuildContext, *, prefix: str = commands.param(converter=to_lower)):
        """remove a prefix from the server"""
        bot = self.bot
        
        if prefix not in await bot.redis.smembers(f"prefixes:{ctx.guild.id}"):
            raise commands.BadArgument("This prefix does not exist. Check your spelling and try again.")
        
        sql = """DELETE FROM guild_prefixes WHERE guild_id = $1 AND prefix = $2"""
        await bot.pool.execute(sql, ctx.guild.id, prefix)
        await bot.redis.srem(f"prefixes:{ctx.guild.id}", prefix)
        await ctx.send(f"Removed prefix `{prefix}` from the server.")