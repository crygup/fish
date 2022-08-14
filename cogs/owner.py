import argparse
import difflib
import imghdr
import itertools
import re
import shlex
import textwrap
import time
import traceback
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple, Union

import discord
from bot import Bot
from discord.ext import commands
from jishaku.codeblocks import codeblock_converter
from jishaku.paginators import WrappedPaginator
from tabulate import tabulate
from utils import (
    ExtensionConverter,
    GuildContext,
    UntilFlag,
    cleanup_code,
    human_join,
    plural,
)

from cogs.context import Context, P


async def setup(bot: Bot):
    await bot.add_cog(Owner(bot))


class EvaluatedArg(commands.Converter):
    async def convert(self, ctx: Context, argument: str) -> str:
        return eval(cleanup_code(argument), {"bot": ctx.bot, "ctx": ctx})


class SqlCommandFlags(
    commands.FlagConverter, prefix="-", delimiter=" ", case_insensitive=True
):
    args: List[str] = commands.Flag(name="argument", aliases=["a", "arg"], annotation=List[EvaluatedArg], default=[])  # type: ignore


class Arguments(argparse.ArgumentParser):
    def error(self, message):
        raise RuntimeError(message)


class Owner(commands.Cog, name="owner", command_attrs=dict(hidden=True)):
    """Commands for the owner"""

    def __init__(self, bot: Bot):
        self.bot = bot

    async def cog_check(self, ctx: Context) -> bool:
        check = await self.bot.is_owner(ctx.author)

        if not check:
            raise commands.NotOwner

        return True

    @commands.command(name="load", aliases=("reload",))
    async def load(self, ctx: Context, *extensions: ExtensionConverter):
        """Loads or reloads a cog"""

        paginator = WrappedPaginator(prefix="", suffix="")

        if not extensions:
            raise commands.BadArgument("No extensions provided")

        for extension in itertools.chain(*extensions):  # type: ignore

            results = difflib.get_close_matches(extension, self.bot.exts, cutoff=0.5)
            extension = results[0] if results else extension

            method, icon = (
                (
                    self.bot.reload_extension,
                    "<:cr_reload:956384262096031744>",
                )
                if extension in self.bot.extensions
                else (self.bot.load_extension, "<:cr_load:956384261945040896>")
            )

            try:
                await discord.utils.maybe_coroutine(method, extension)
            except Exception as exc:  # pylint: disable=broad-except
                traceback_data = "".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__, 1)
                )

                paginator.add_line(
                    f"{icon}<:cr_warning:956384262016344064> `{extension}`\n```py\n{traceback_data}\n```",
                    empty=True,
                )
            else:
                paginator.add_line(f"{icon} `{extension}`", empty=True)

        content = []
        for page in paginator.pages:
            content.append(page)

        content = "\n".join([str(x).replace("\n\n", "\n") for x in content])
        embed = discord.Embed(
            title="Reload" if ctx.invoked_with == "reload" else "Load",
            description=content,
        )
        await ctx.send(embed=embed)

    @commands.command(name="unload")
    async def unload(self, ctx: Context, *extensions: ExtensionConverter):
        """Reloads a cog"""

        paginator = WrappedPaginator(prefix="", suffix="")

        # 'jsk reload' on its own just reloads jishaku
        if ctx.invoked_with == "reload" and not extensions:
            extensions = [["jishaku"]]  # type: ignore

        for extension in itertools.chain(*extensions):  # type: ignore
            if extension not in self.bot.extensions:
                results = difflib.get_close_matches(
                    extension, self.bot.extensions.keys()
                )
                extension = results[0] if results else extension

            method, icon = (
                self.bot.unload_extension,
                "<:cr_unload:957281084100456480>",
            )

            try:
                await discord.utils.maybe_coroutine(method, extension)
            except Exception as exc:
                traceback_data = "".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__, 1)
                )

                paginator.add_line(
                    f"{icon}<:cr_warning:956384262016344064> `{extension}`\n```py\n{traceback_data}\n```",
                    empty=True,
                )
            else:
                paginator.add_line(f"{icon} `{extension}`", empty=True)

        content = []
        for page in paginator.pages:
            content.append(page)

        content = "\n".join([str(x).replace("\n\n", "\n") for x in content])
        embed = discord.Embed(
            title="Reload" if ctx.invoked_with == "reload" else "Load",
            description=content,
        )
        await ctx.send(embed=embed)

    @commands.command(name="snipe")
    @commands.is_owner()
    async def snipe(
        self,
        ctx: Context,
        index: Optional[int],
        channel: Optional[discord.TextChannel] = commands.CurrentChannel,
        *,
        member: Optional[discord.Member],
    ):
        """Shows a deleted message"""
        index = index or 1

        if ctx.guild is None or channel is None:
            return

        if member:
            sql = """
            SELECT * FROM message_logs where channel_id = $1 AND author_id = $2 AND deleted IS TRUE ORDER BY created_at DESC
            """
            results = await self.bot.pool.fetch(sql, channel.id, member.id)
        else:
            sql = """
            SELECT * FROM message_logs where channel_id = $1 AND deleted IS TRUE ORDER BY created_at DESC
            """
            results = await self.bot.pool.fetch(sql, channel.id)

        if index - 1 >= len(results):
            await ctx.send("Index out of range.")
            return

        if results == []:
            await ctx.send("Nothing was deleted here...")
            return

        user = self.bot.get_user(results[index - 1]["author_id"]) or "Unknown"

        embeds: List[discord.Embed] = []
        files: List[discord.File] = []

        embed = discord.Embed(
            color=self.bot.embedcolor, timestamp=results[index - 1]["created_at"]
        )
        embed.description = (
            textwrap.shorten(
                results[index - 1]["message_content"], width=300, placeholder="..."
            )
            or "Message did not contain any content."
        )
        embed.set_author(
            name=f"{str(user)}",
            icon_url=user.display_avatar.url
            if isinstance(user, discord.User)
            else ctx.guild.me.display_avatar.url,
        )
        message_id = results[index - 1]["message_id"]
        embed.set_footer(text=f"Index {index} of {len(results)}\nMessage deleted ")
        embeds.append(embed)

        if results[index - 1]["has_attachments"]:
            attachment_sql = """SELECT * FROM message_attachment_logs where message_id = $1 AND deleted IS TRUE"""
            attachment_results = await self.bot.pool.fetch(attachment_sql, message_id)
            for _index, result in enumerate(attachment_results):
                file = discord.File(
                    BytesIO(result["attachment"]),
                    filename=f'{message_id}_{_index}.{imghdr.what(None, result["attachment"])}',
                )
                files.append(file)
                embed = discord.Embed(
                    color=self.bot.embedcolor,
                    timestamp=results[index - 1]["created_at"],
                )
                embed.set_image(
                    url=f'attachment://{message_id}_{_index}.{imghdr.what(None, result["attachment"])}'
                )
                embeds.append(embed)

        await ctx.send(embeds=embeds[:10], files=files[:9])
        if len(embeds) >= 10:
            await ctx.send(embeds=embeds[-1:], files=files[-1:])

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
    async def evaluate_command(self, ctx: Context, *, content: str):
        content = re.sub(r"ref", "message.reference.resolved", content)
        content = re.sub(r"self.bot", "bot", content)

        command = self.bot.get_command("jsk py")

        if command is None:
            await ctx.send("Command not found")
            return

        await command(ctx, argument=codeblock_converter(content))

    @commands.command(name="venv")
    async def _venv_shell(self, ctx: Context, *, content: str):
        content = "venv/bin/python3.10 -m " + content

        command = self.bot.get_command("jsk sh")

        if command is None:
            await ctx.send("Command not found")
            return

        await command(ctx, argument=codeblock_converter(content))

    @commands.command(name="update")
    async def update(self, ctx: Context):
        command = self.bot.get_command("jsk git")

        if command is None:
            await ctx.send("Command not found")
            return

        await command(ctx, argument="pull")

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

            if option.id in await self.bot.redis.smembers("blacklisted_guilds"):
                await self.bot.pool.execute(
                    "DELETE FROM guild_blacklist WHERE guild_id = $1", option.id
                )
                await self.bot.redis.srem("blacklisted_guilds", option.id)
                await ctx.send(f"Removed guild `{option}` from the blacklist")
            else:
                await self.bot.pool.execute(
                    "INSERT INTO guild_blacklist (guild_id, reason, time) VALUES ($1, $2, $3)",
                    option.id,
                    reason,
                    discord.utils.utcnow(),
                )
                await self.bot.redis.sadd("blacklisted_guilds", option.id)
                await ctx.send(f"Added guild `{option}` to the blacklist")

        elif isinstance(option, discord.User):
            if option.id == ctx.bot.owner_id:
                await ctx.send("dumbass")
                return

            if str(option.id) in await self.bot.redis.smembers("blacklisted_users"):
                await self.bot.pool.execute(
                    "DELETE FROM user_blacklist WHERE user_id = $1", option.id
                )
                await self.bot.redis.srem("blacklisted_users", option.id)
                await ctx.send(f"Removed user `{option}` from the blacklist")
            else:
                await self.bot.pool.execute(
                    "INSERT INTO user_blacklist (user_id, reason, time) VALUES ($1, $2, $3)",
                    option.id,
                    reason,
                    discord.utils.utcnow(),
                )
                await self.bot.redis.sadd("blacklisted_users", option.id)
                await ctx.send(f"Added user `{option}` to the blacklist")
        else:
            await ctx.send("what")
