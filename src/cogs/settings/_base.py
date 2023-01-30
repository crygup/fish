from __future__ import annotations

from typing import TYPE_CHECKING

from discord.ext.commands import Cog

if TYPE_CHECKING:
    from bot import Bot
    from cogs.context import Context


class CogBase(Cog):
    def __init__(self, bot: Bot):
        self.bot: Bot = bot

    async def unlink_method(self, ctx: Context, user_id: int, option: str):
        await ctx.bot.pool.execute(
            f"UPDATE accounts SET {option} = NULL WHERE user_id = $1", user_id
        )
        await ctx.bot.redis.hdel(f"accounts:{user_id}", option)

        await ctx.send(f"Your {option} account has been unlinked.", ephemeral=True)

    async def link_method(self, ctx: Context, user_id: int, option: str, username: str):
        username = username.lower()
        sql = f"""
        INSERT INTO accounts (user_id, {option}) VALUES ($1, $2)
        ON CONFLICT (user_id) DO UPDATE
        SET {option} = $2 WHERE accounts.user_id = $1
        """
        await ctx.bot.pool.execute(
            sql,
            ctx.author.id,
            username,
        )
        await ctx.bot.redis.hset(f"accounts:{user_id}", option, username)

        await ctx.send(f"Your {option} account has been linked.", ephemeral=True)
