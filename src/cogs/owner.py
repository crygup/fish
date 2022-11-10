import argparse
import difflib
import itertools
import re
import textwrap
import time
import traceback
from io import BytesIO
from typing import Dict, List, Optional, Union

import asyncpg
import discord
from bot import Bot, Context
from discord.ext import commands
from jishaku.codeblocks import codeblock_converter
from jishaku.paginators import WrappedPaginator
from tabulate import tabulate
from utils import (
    AuthorView,
    ExtensionConverter,
    NoCover,
    Pager,
    SimplePages,
    UntilFlag,
    cleanup_code,
    plural,
    response_checker,
    to_bytesio,
)


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


class FakeUser:
    def __init__(
        self,
        user: Union[discord.Member, discord.User],
    ) -> None:
        self.name = user.name
        self.id = user.id
        self.tag = user.discriminator
        self.full = f"{user!r}"

    def __str__(self) -> str:
        return self.full

    def __repr__(self) -> str:
        return self.full


class FakeGuild:
    def __init__(self, guild: discord.Guild) -> None:
        self.name = guild.name
        self.id = guild.id
        self.icon = guild.icon.url if guild.icon else None

    def __str__(self) -> str:
        return self.name


class Owner(
    commands.Cog,
    name="owner",
    command_attrs=dict(
        hidden=True,
        extras={"UPerms": ["Bot Owner"]},
    ),
):
    """Commands for the owner"""

    def __init__(self, bot: Bot):
        self.bot = bot

    async def cog_check(self, ctx: Context) -> bool:
        check = await self.bot.is_owner(ctx.author)

        if not check:
            raise commands.NotOwner

        return True

    @commands.command(name="test")
    async def test(self, ctx: Context, *, message: str):
        new_message = message.format_map({"user": ctx.author, "server": ctx.guild})

        await ctx.send(
            new_message,
            allowed_mentions=discord.AllowedMentions(everyone=False, users=True),
        )

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
        embed.set_footer(text=f"Index {index} of {len(results)}\nMessage deleted ")

        await ctx.send(embed=embed)

    @commands.command(name="sql")
    async def sql(self, ctx: Context, *, query: UntilFlag[SqlCommandFlags]):
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

        await command(ctx, argument=codeblock_converter("pull"))

    @commands.command(name="block", aliases=("blacklist",))
    async def block(
        self,
        ctx: Context,
        snowflake: discord.Object,
        *,
        reason: str = "No reason provided",
    ):
        try:
            sql = """INSERT INTO block_list(snowflake, reason, time) VALUES ($1, $2, $3)"""
            await self.bot.pool.execute(
                sql, snowflake.id, reason, discord.utils.utcnow()
            )
            await self.bot.redis.sadd("block_list", snowflake.id)
            msg = f"{snowflake.id} has been blocked"
        except asyncpg.UniqueViolationError:
            sql = """DELETE FROM block_list WHERE snowflake = $1"""
            await self.bot.pool.execute(sql, snowflake.id)
            await self.bot.redis.srem("block_list", snowflake.id)
            msg = f"{snowflake.id} has been unblocked"

        await ctx.send(msg)

    @commands.group(name="dev", invoke_without_command=True)
    async def dev(self, ctx: Context):
        """Developer commands"""

    @dev.command(name="servers")
    async def dev_serers(self, ctx: Context):
        guilds: List[discord.Guild] = sorted(self.bot.guilds, key=lambda g: g.member_count, reverse=True)  # type: ignore
        data = [
            f"{g} | {g.id} | {sum(not m.bot for m in g.members):,} | {sum(m.bot for m in g.members):,}"
            for g in guilds
        ]
        pages = SimplePages(entries=data, per_page=10, ctx=ctx)
        pages.embed.title = "Servers"
        await pages.start(ctx)

    @dev.command(name="cover")
    async def dev_cover(self, ctx: Context, *, query: str):
        url = "https://api.spotify.com/v1/search"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.bot.spotify_key}",
        }

        data = {"q": query, "type": "album", "market": "ES", "limit": "1"}

        async with self.bot.session.get(url, headers=headers, params=data) as r:
            response_checker(r)
            results: Dict = await r.json()

        try:
            image_url = results["albums"]["items"][0]["images"][0]["url"]
        except (IndexError, KeyError):
            raise NoCover("No cover found for this album, sorry.")

        await ctx.send(
            view=CoverView(ctx, results),
            file=discord.File(
                await to_bytesio(ctx.session, image_url),
                "cover.png",
                spoiler=results["albums"]["items"][0]["id"]
                in await self.bot.redis.smembers("nsfw_covers"),
            ),
        )


class CoverView(AuthorView):
    def __init__(self, ctx: Context, data: Dict):
        self.data = data
        super().__init__(ctx)

    @discord.ui.button(label="Mark NSFW", style=discord.ButtonStyle.red)
    async def mark_nsfw(self, interaction: discord.Interaction, __):
        sql = """INSERT INTO nsfw_covers(album_id) VALUES ($1)"""
        cover_id = self.data["albums"]["items"][0]["id"]
        bot = self.ctx.bot
        await bot.pool.execute(sql, cover_id)
        await bot.redis.sadd("nsfw_covers", cover_id)

        if interaction.message is None:
            return

        await interaction.message.edit(
            content=f"Successfully marked `{cover_id}` as NSFW.",
            attachments=[
                await interaction.message.attachments[0].to_file(spoiler=True)
            ],
        )
        await interaction.response.defer()

    @discord.ui.button(label="Unmark NSFW", style=discord.ButtonStyle.green)
    async def unmark_nsfw(self, interaction: discord.Interaction, __):
        sql = """DELETE FROM nsfw_covers WHERE album_id = $1"""
        cover_id = self.data["albums"]["items"][0]["id"]
        bot = self.ctx.bot
        await bot.pool.execute(sql, cover_id)
        await bot.redis.srem("nsfw_covers", cover_id)

        if interaction.message is None:
            return

        await interaction.message.edit(
            content=f"Successfully unmarked `{cover_id}` as NSFW.",
            attachments=[
                await interaction.message.attachments[0].to_file(spoiler=False)
            ],
        )
        await interaction.response.defer()
