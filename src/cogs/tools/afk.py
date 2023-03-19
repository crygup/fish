from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands
from discord.utils import escape_markdown

from utils import human_timedelta, BaseCog

if TYPE_CHECKING:
    from bot import Bot
    from cogs.context import Context


class AfkCommands(BaseCog):
    def __init__(self, bot: Bot):
        self.bot = bot

    @commands.command(name="afk")
    async def afk(self, ctx: Context, reason: str = "No reason set."):
        """Enables afk messages"""
        if str(ctx.author.id) in await self.bot.redis.smembers("afk_users"):
            return

        sql = """
        INSERT INTO afk (user_id, reason, time)
        VALUES ($1, $2, $3)
        """

        await self.bot.pool.execute(sql, ctx.author.id, reason, discord.utils.utcnow())
        await self.bot.redis.sadd("afk_users", ctx.author.id)
        await ctx.send(f"Alright {ctx.author.mention}, see you soon.")

    @commands.Cog.listener("on_message")
    async def afk_check(self, message: discord.Message):
        if message.guild is None:
            return

        afk_users = await self.bot.redis.smembers("afk_users")

        if str(message.author.id) in afk_users:
            return

        mentions = [user for user in message.mentions if str(user.id) in afk_users]

        if mentions == []:
            return

        to_send = []

        for user in mentions:
            sql = """SELECT * FROM afk WHERE user_id = $1"""
            results = await self.bot.pool.fetchrow(sql, user.id)

            if results is None:
                continue

            to_send.append(
                f"{user.mention} was last online {human_timedelta(results['time'])}, **{escape_markdown(results['reason'])}**"
            )

        try:
            await message.author.send("\n".join(to_send))
        except (discord.Forbidden, discord.HTTPException):
            to_send = f"{'Those users are' if len(mentions) > 1 else 'That user is'} currently AFK, they'll be back later."
            await message.channel.send(to_send)

    @commands.Cog.listener("on_message")
    async def afk_return(self, message: discord.Message):
        if message.guild is None:
            return
        afk_users = await self.bot.redis.smembers("afk_users")

        if str(message.author.id) in afk_users:
            await message.add_reaction("\U0001f44b")
            sql = """DELETE FROM afk WHERE user_id = $1"""
            await self.bot.pool.execute(sql, message.author.id)
            await self.bot.redis.srem("afk_users", message.author.id)
