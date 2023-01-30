from __future__ import annotations

from typing import TYPE_CHECKING, Set

from discord.ext import commands

from utils import CHECK, BlankException, human_join

from ._base import CogBase

if TYPE_CHECKING:
    from cogs.context import Context

Logger = [
    "avatars",
    "guild avatars",
    "nicknames",
    "discrims",
    "usernames",
    "joins",
    "uptime",
]


class OptCog(CogBase):
    @commands.group(name="opt", invoke_without_command=True)
    async def opt_group(self, ctx: Context):
        """Opt in or out of a logger

        Due to technical reasons, opting out from `guild names`, `guild bans` and `guild icons` is not possible yet, but you can still delete the data at any time."""
        items: Set[str] = await ctx.redis.smembers(f"opted_out:{ctx.author.id}")

        if items == set():
            raise BlankException(
                "You haven't opted out of any loggers. good on you \U0001f609"
            )

        await ctx.send(
            f"You have opted out from {human_join([f'`{item}`' for item in items], final='and')}"
        )

    @opt_group.command(name="in")
    async def opt_in(self, ctx: Context, *, logger: str):
        if logger.lower() not in Logger:
            raise BlankException(
                f"Logger must be one of {human_join([f'`{log}`' for log in Logger])}"
            )

        sql = """UPDATE opted_out SET items = array_remove(opted_out.items, $1) WHERE user_id = $2"""

        await ctx.pool.execute(sql, logger, ctx.author.id)
        await ctx.redis.srem(f"opted_out:{ctx.author.id}", logger)
        await ctx.send(str(CHECK))

    @opt_group.command(name="out")
    async def opt_out(self, ctx: Context, *, logger: str):
        if logger.lower() not in Logger:
            raise BlankException(
                f"Logger must be one of {human_join([f'`{log}`' for log in Logger])}"
            )

        sql = """
        INSERT INTO opted_out (user_id, items) VALUES ($1, ARRAY [$2]) 
        ON CONFLICT (user_id) DO UPDATE
        SET items = array_append(opted_out.items, $2) 
        WHERE opted_out.user_id = $1
        """

        await ctx.pool.execute(sql, ctx.author.id, logger)
        await ctx.redis.sadd(f"opted_out:{ctx.author.id}", logger)
        await ctx.send(str(CHECK))
