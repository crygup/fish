import asyncio
import textwrap
import time
from io import BytesIO
from typing import Any, List, Tuple, Union

import discord
from bot import Bot
from discord.ext import commands
from tabulate import tabulate
from utils import UntilFlag, cleanup_code, plural, GuildContext
from jishaku.codeblocks import codeblock_converter


async def setup(bot: Bot):
    await bot.add_cog(Owner(bot))


class EvaluatedArg(commands.Converter):
    async def convert(self, ctx: commands.Context, argument: str) -> str:
        return eval(cleanup_code(argument), {"bot": ctx.bot, "ctx": ctx})


class SqlCommandFlags(
    commands.FlagConverter, prefix="-", delimiter=" ", case_insensitive=True
):
    args: List[str] = commands.Flag(name="argument", aliases=["a", "arg"], annotation=List[EvaluatedArg], default=[])  # type: ignore


class Owner(commands.Cog, name="owner", command_attrs=dict(hidden=True)):
    def __init__(self, bot: Bot):
        self.bot = bot

    async def cog_check(self, ctx: commands.Context[Bot]) -> bool:
        check = await self.bot.is_owner(ctx.author)

        if not check:
            raise commands.NotOwner

        return True

    @commands.command(name="sql")
    async def sql(self, ctx: GuildContext, *, query: UntilFlag[SqlCommandFlags]):
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

    @commands.command(name="evaluate", aliases=["eval", "e"])
    async def _evaluate(self, ctx: commands.Context, *, content: str):
        content = content.replace("self.bot", "bot").replace(
            "ref", "message.reference.resolved"
        )

        command = self.bot.get_command("jsk py")

        if command is None:
            await ctx.send("Command not found")
            return

        await command(ctx, argument=codeblock_converter(content))

    @commands.command(name="venv")
    async def _venv_shell(self, ctx: commands.Context, *, content: str):
        content = "venv/bin/python3.10 -m " + content

        command = self.bot.get_command("jsk sh")

        if command is None:
            await ctx.send("Command not found")
            return

        await command(ctx, argument=codeblock_converter(content))

    @commands.command(name="blacklist")
    async def _blacklist(
        self,
        ctx: GuildContext,
        option: Union[discord.User, discord.Guild],
        *,
        reason: str = "No reason provided",
    ):
        if isinstance(option, discord.Guild):
            if option.owner_id == ctx.bot.owner_id:
                await ctx.send("dumbass")
                return

            if option.id in self.bot.blacklisted_guilds:
                await self.bot.pool.execute(
                    "DELETE FROM guild_blacklist WHERE guild_id = $1", option.id
                )
                self.bot.blacklisted_guilds.remove(option.id)
                await ctx.send(f"Removed guild `{option}` from the blacklist")
            else:
                await self.bot.pool.execute(
                    "INSERT INTO guild_blacklist (guild_id, reason, time) VALUES ($1, $2, $3)",
                    option.id,
                    reason,
                    discord.utils.utcnow(),
                )
                self.bot.blacklisted_guilds.append(option.id)
                await ctx.send(f"Added guild `{option}` to the blacklist")

        elif isinstance(option, discord.User):
            if option.id == ctx.bot.owner_id:
                await ctx.send("dumbass")
                return

            if option.id in self.bot.blacklisted_users:
                await self.bot.pool.execute(
                    "DELETE FROM user_blacklist WHERE user_id = $1", option.id
                )
                self.bot.blacklisted_users.remove(option.id)
                await ctx.send(f"Removed user `{option}` from the blacklist")
            else:
                await self.bot.pool.execute(
                    "INSERT INTO user_blacklist (user_id, reason, time) VALUES ($1, $2, $3)",
                    option.id,
                    reason,
                    discord.utils.utcnow(),
                )
                self.bot.blacklisted_users.append(option.id)
                await ctx.send(f"Added user `{option}` to the blacklist")
        else:
            await ctx.send("what")

    @commands.command(name="whitelist")
    async def _whitelist(
        self,
        ctx: GuildContext,
        user: discord.User,
        *,
        reason: str = "No reason provided",
    ):
        if user.id in self.bot.whitelisted_users:
            await self.bot.pool.execute(
                "DELETE FROM user_whitelist WHERE user_id = $1", user.id
            )
            self.bot.whitelisted_users.remove(user.id)
            await ctx.send(f"Removed user `{user}` from the whitelist")

        else:
            await self.bot.pool.execute(
                "INSERT INTO user_whitelist (user_id, reason, time) VALUES ($1, $2, $3)",
                user.id,
                reason,
                discord.utils.utcnow(),
            )
            self.bot.whitelisted_users.append(user.id)
            await ctx.send(f"Added user `{user}` to the whitelist")