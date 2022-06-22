import time
from io import BytesIO
from typing import List

import discord
from bot import Bot
from discord.ext import commands
from tabulate import tabulate
from utils import UntilFlag, cleanup_code, plural


async def setup(bot: Bot):
    await bot.add_cog(Owner(bot))


class EvaluatedArg(commands.Converter):
    async def convert(self, ctx: commands.Context, argument: str) -> str:
        return eval(cleanup_code(argument), {"bot": ctx.bot, "ctx": ctx})


class SqlCommandFlags(
    commands.FlagConverter, prefix="-", delimiter=" ", case_insensitive=True
):
    args: List[str] = commands.Flag(name="argument", aliases=["a", "arg"], annotation=List[EvaluatedArg], default=[])  # type: ignore


class Owner(commands.Cog, name="owner"):
    def __init__(self, bot: Bot):
        self.bot = bot

    @commands.command(name="sql",hidden=True)
    @commands.is_owner()
    async def sql(self, ctx: commands.Context, *, query: UntilFlag[SqlCommandFlags]):
        """|coro|
        Executes an SQL query
        Parameters
        ----------
        query: str
            The query to execute.
        """
        query.value = cleanup_code(query.value)
        is_multistatement = query.value.count(";") > 1
        if is_multistatement:
            # fetch does not support multiple statements
            strategy = ctx.bot.pool.execute
        else:
            strategy = ctx.bot.pool.fetch

        try:
            start = time.perf_counter()
            results = await strategy(query.value, *query.flags.args)
            dt = (time.perf_counter() - start) * 1000.0
        except Exception as e:
            return await ctx.send(f"{type(e).__name__}: {e}")

        rows = len(results)
        if rows == 0 or isinstance(results, str):
            result = "Query returned 0 rows" if rows == 0 else str(results)
            await ctx.send(f"`{result}`\n*Ran in {dt:.2f}ms*")

        else:
            table = tabulate(results, headers="keys", tablefmt="orgtbl")

            fmt = f"```\n{table}\n```*Returned {plural(rows):row} in {dt:.2f}ms*"
            if len(fmt) > 2000:
                fp = BytesIO(table.encode("utf-8"))
                await ctx.send(
                    f"*Too many results...\nReturned {plural(rows):row} in {dt:.2f}ms*",
                    file=discord.File(fp, "output.txt"),
                )
            else:
                await ctx.send(fmt)
