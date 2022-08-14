import re

import asyncpg
import discord
from bot import Bot, Context
from discord.ext import commands
from utils import (
    SteamIDConverter,
    UnknownAccount,
    SimplePages,
    FieldPageSource,
    Pager,
    add_prefix,
)


async def setup(bot: Bot):
    await bot.add_cog(Settings(bot))


class Settings(commands.Cog, name="settings"):
    """Settings for the bot"""

    def __init__(self, bot: Bot):
        self.bot = bot

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name="\U00002699\U0000fe0f")

    async def unlink_method(self, ctx: Context, user_id: int, option: str):
        await ctx.bot.pool.execute(
            f"UPDATE accounts SET {option} = NULL WHERE user_id = $1", user_id
        )
        await ctx.bot.redis.hdel(f"accounts:{user_id}", option)

        await ctx.send(f"Your {option} account has been unlinked.")

    async def link_method(self, ctx: Context, user_id: int, option: str, username: str):
        username = username.lower()

        try:
            await ctx.bot.pool.execute(
                f"INSERT INTO accounts (user_id, {option}) VALUES($1, $2)",
                ctx.author.id,
                username,
            )
        except asyncpg.UniqueViolationError:
            await ctx.bot.pool.execute(
                f"UPDATE accounts SET {option} = $1 WHERE user_id = $2",
                username,
                ctx.author.id,
            )
        await ctx.bot.redis.hset(f"accounts:{user_id}", option, username)

        await ctx.send(f"Your {option} account has been linked.")

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
                f'{discord.utils.format_dt(record["time"], "R")}  |  {discord.utils.format_dt(record["time"], "d")} | {(await self.bot.getch_user(record["author_id"])).mention}',
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

    @commands.command(name="accounts")
    async def accounts(self, ctx: Context, *, user: discord.User = commands.Author):
        """Shows your linked accounts"""
        accounts = await ctx.bot.pool.fetchrow(
            "SELECT * FROM accounts WHERE user_id = $1", user.id
        )

        if not accounts:
            await ctx.send(f"{str(user)} has no linked accounts.")
            return

        embed = discord.Embed()
        embed.set_author(
            name=f"{user.display_name} - Connected accounts",
            icon_url=user.display_avatar.url,
        )

        embed.add_field(name="Last.fm", value=accounts["lastfm"] or "Not set")
        embed.add_field(name="osu!", value=accounts["osu"] or "Not set")
        embed.add_field(name="Steam", value=accounts["steam"] or "Not set")
        embed.add_field(name="Roblox", value=accounts["roblox"] or "Not set")
        embed.add_field(name="Genshin UID", value=accounts["genshin"] or "Not set")
        embed.add_field(name="\u200b", value="\u200b")

        await ctx.send(embed=embed, check_ref=True)

    @commands.group(name="set", invoke_without_command=True)
    async def set(self, ctx: Context):
        """Sets your profile for a site"""
        await ctx.send_help(ctx.command)

    @set.command(name="lastfm", aliases=["fm"])
    async def set_lastfm(self, ctx: Context, username: str):
        """Sets your last.fm account"""
        if not re.fullmatch(r"[a-zA-Z0-9_-]{2,15}", username):
            raise UnknownAccount("Invalid username.")

        await self.link_method(ctx, ctx.author.id, "lastfm", username)

    @set.command(name="osu")
    async def set_osu(self, ctx: Context, *, username: str):
        """Sets your osu! account"""
        if not re.fullmatch(r"[a-zA-Z0-9_\s-]{2,16}", username):
            raise UnknownAccount("Invalid username.")

        await self.link_method(ctx, ctx.author.id, "osu", username)

    @set.command(name="steam")
    async def set_steam(self, ctx: Context, username: str):
        """Sets your steam account"""
        SteamIDConverter(username)
        await self.link_method(ctx, ctx.author.id, "steam", username)

    @set.command(name="roblox")
    async def set_roblox(self, ctx: Context, *, username: str):
        """Sets your roblox account"""

        await self.link_method(ctx, ctx.author.id, "roblox", username)

    @set.command(name="genshin")
    async def set_genshin(self, ctx: Context, *, username: str):
        """Sets your genshin account"""
        if not re.match(r"[0-9]{4,15}", username):
            raise UnknownAccount("Invalid UID.")

        await self.link_method(ctx, ctx.author.id, "genshin", username)

    @commands.group(name="unlink", invoke_without_command=True)
    async def unlink(self, ctx: Context):
        """Unlinks your account"""
        await ctx.send_help(ctx.command)

    @unlink.command(name="lastfm")
    async def unlink_lastfm(self, ctx: Context):
        await self.unlink_method(ctx, ctx.author.id, "lastfm")

    @unlink.command(name="osu")
    async def unlink_osu(self, ctx: Context):
        await self.unlink_method(ctx, ctx.author.id, "osu")

    @unlink.command(name="steam")
    async def unlink_steam(self, ctx: Context):
        await self.unlink_method(ctx, ctx.author.id, "steam")

    @unlink.command(name="roblox")
    async def unlink_roblox(self, ctx: Context):
        await self.unlink_method(ctx, ctx.author.id, "roblox")

    @unlink.command(name="genshin")
    async def unlink_genshin(self, ctx: Context):
        await self.unlink_method(ctx, ctx.author.id, "genshin")
