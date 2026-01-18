from __future__ import annotations

from typing import TYPE_CHECKING

import discord
import re
from discord.ext import commands

from .logging import Logging
from .server import Server
from utils import LASTFM_USERNAME, lastfm_command
from discord import app_commands

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


class Settings(Logging, Server):
    """User and server settings"""

    emoji = discord.PartialEmoji(name="\U00002699\U0000fe0f")

    def __init__(self, bot: Fishie):
        super().__init__()
        self.bot = bot

    @commands.hybrid_group(
        name="link",
    )
    async def link(self, ctx: Context):
        """Link an account to fishie"""
        await ctx.send(
            "lowk only last.fm exists rn so do link lastfm <username>", ephemeral=True
        )

    @link.command(name="lastfm")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def link_lastfm(self, ctx: Context, username: str):
        """Link your last.fm to fishie"""
        if not LASTFM_USERNAME.search(username):
            commands.BadArgument("Username provided is invalid, please check again")

        sql = """
        INSERT INTO accounts (user_id, lastfm) VALUES ($1, $2 )
        """

        await self.bot.pool.execute(sql, ctx.author.id, username)
        self.bot.db_cache.add_account(ctx.author.id, username)
        await ctx.send("done")

    @commands.hybrid_group(
        name="unlink",
    )
    async def unlink(self, ctx: Context):
        """Unlink an account from fishie"""
        await ctx.send(
            "lowk only last.fm exists rn so do unlink lastfm <username>", ephemeral=True
        )

    @unlink.command(name="lastfm")
    @lastfm_command()
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def unlink_lastfm(self, ctx: Context):
        """Unlink your last.fm from fishie"""
        lfm_name = self.bot.db_cache.lastfm[ctx.author.id]
        sql = """
        DELETE FROM accounts WHERE user_id = $1 AND lastfm = $2
        """

        await self.bot.pool.execute(sql, ctx.author.id, lfm_name)
        self.bot.db_cache.remove_account(ctx.author.id)
        await ctx.send("done")


async def setup(bot: Fishie):
    await bot.add_cog(Settings(bot))
