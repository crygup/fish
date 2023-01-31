from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Optional

import asyncpg
import discord
from discord.ext import commands

from utils import FieldPageSource, Pager, get_or_fetch_user

from ._base import CogBase

if TYPE_CHECKING:
    from bot import Bot
    from cogs.context import Context


class TagName(commands.clean_content):
    def __init__(self, *, lower: bool = False):
        self.lower: bool = lower
        super().__init__()

    async def convert(self, ctx: Context, argument: str) -> str:
        converted = await super().convert(ctx, argument)
        lower = converted.lower().strip()

        if not lower:
            raise commands.BadArgument("Missing tag name.")

        if len(lower) > 100:
            raise commands.BadArgument("Tag name is a maximum of 100 characters.")

        first_word, _, _ = lower.partition(" ")

        # get tag command.
        root: commands.GroupMixin = ctx.bot.get_command("tag")  # type: ignore
        if first_word in root.all_commands:
            raise commands.BadArgument("This tag name starts with a reserved word.")

        return converted if not self.lower else lower


class TagCommands(CogBase):
    def __init__(self, bot: Bot):
        self.bot = bot

    @commands.group(name="tag", invoke_without_command=True)
    async def tag(self, ctx: Context, *, name: Annotated[str, TagName(lower=True)]):
        """Allows you to tag text for later retrieval."""
        sql = """
        SELECT content FROM tags WHERE guild_id = $1 AND LOWER(name)=$2
        """
        result = await self.bot.pool.fetchval(sql, ctx.guild.id, name)
        if not result:
            await ctx.send(f"A tag with the name `{name}` does not exist.")
            return

        await ctx.send(result, reference=ctx.replied_reference)

        # updating use count
        sql = """
        UPDATE tags SET uses = uses + 1 WHERE guild_id = $1 AND LOWER(name)=$2"""
        await self.bot.pool.execute(sql, ctx.guild.id, name)

    @tag.command(name="create")
    async def tag_create(
        self,
        ctx: Context,
        name: Annotated[str, TagName],
        *,
        content: Optional[Annotated[str, commands.clean_content]],
    ):
        sql = """
        INSERT INTO tags (guild_id, author_id, name, content, created_at) 
        VALUES ($1, $2, $3, $4, $5)
        """

        if content is None:
            if ctx.message.attachments:
                content = ctx.message.attachments[0].url
            else:
                await ctx.send("content is a required argument that is missing.")
                return

        try:
            await self.bot.pool.execute(
                sql, ctx.guild.id, ctx.author.id, name, content, discord.utils.utcnow()
            )
        except asyncpg.exceptions.UniqueViolationError:
            await ctx.send(f"A tag with the name `{name}` already exists.")
            return

        await ctx.send(f"Tag `{name}` created.")

    @tag.command(name="delete")
    async def tag_delete(
        self, ctx: Context, *, name: Annotated[str, TagName(lower=True)]
    ):

        bypass_owner_check = (
            ctx.author.id == self.bot.owner_id
            or ctx.author.guild_permissions.manage_messages
        )
        method = "WHERE LOWER(name) = $1 AND guild_id = $2"

        if bypass_owner_check:
            args = [name, ctx.guild.id]
        else:
            args = [name, ctx.guild.id, ctx.author.id]
            method += " AND author_id = $3"

        sql = f"""
        DELETE FROM tags {method} RETURNING name;
        """

        deleted = await self.bot.pool.fetchrow(sql, *args)
        if deleted is None:
            await ctx.send(
                "Could not delete tag. Either it does not exist or you do not have permissions to do so."
            )
            return

        await ctx.send(f"Tag `{name}` deleted.")

    @tag.command(name="edit")
    async def tag_edit(
        self,
        ctx: Context,
        name: Annotated[str, TagName(lower=True)],
        *,
        content: Annotated[str, commands.clean_content],
    ):
        sql = """
        UPDATE tags SET content = $3 WHERE LOWER(name) = $1 AND guild_id = $2 AND author_id = $4 RETURNING name;
        """

        updated = await self.bot.pool.fetchrow(
            sql, name, ctx.guild.id, content, ctx.author.id
        )

        if updated is None:
            await ctx.send(
                "Could not update tag. Either it does not exist or you do not have permissions to do so."
            )
            return

        await ctx.send(f"Tag `{name}` updated.")

    @tag.command(name="list")
    async def tag_list(self, ctx: Context, member: discord.Member = commands.Author):
        sql = """
        SELECT * FROM tags WHERE guild_id = $1 AND author_id = $2
        """
        results = await self.bot.pool.fetch(sql, ctx.guild.id, member.id)

        if not results:
            await ctx.send(f"{member.mention} has no tags.")
            return

        entries = [
            (
                r["name"],
                f'{discord.utils.format_dt(r["created_at"], "R")}  |  {discord.utils.format_dt(r["created_at"], "d")}',
            )
            for r in results
        ]

        source = FieldPageSource(entries=entries)
        source.embed.title = f"Tags for {member} in {ctx.guild}"
        source.embed.color = self.bot.embedcolor
        pager = Pager(source, ctx=ctx)
        await pager.start(ctx)

    @tag.command(name="all")
    async def tag_all(self, ctx: Context):
        sql = """
        SELECT * FROM tags WHERE guild_id = $1
        """
        results = await self.bot.pool.fetch(sql, ctx.guild.id)

        if not results:
            await ctx.send("This server has no tags.")
            return

        entries = [
            (
                r["name"],
                f'{(await get_or_fetch_user(self.bot, r["author_id"])).mention} | {discord.utils.format_dt(r["created_at"], "R")}  |  {discord.utils.format_dt(r["created_at"], "d")}',
            )
            for r in results
        ]

        source = FieldPageSource(entries=entries)
        source.embed.title = f"Tags in {ctx.guild}"
        source.embed.color = self.bot.embedcolor
        pager = Pager(source, ctx=ctx)
        await pager.start(ctx)

    @tag.command(name="info")
    async def tag_info(
        self, ctx: Context, *, name: Annotated[str, TagName(lower=True)]
    ):
        sql = """
        SELECT * FROM tags WHERE LOWER(name) = $1 AND guild_id = $2
        """

        tag = await self.bot.pool.fetchrow(sql, name, ctx.guild.id)

        if tag is None:
            await ctx.send("Tag not found.")
            return

        embed = discord.Embed(color=self.bot.embedcolor)
        author = await get_or_fetch_user(self.bot, tag["author_id"])
        embed.set_author(name=str(author), icon_url=author.display_avatar.url)
        embed.add_field(
            name="Created",
            value=f'{discord.utils.format_dt(tag["created_at"], "R")}\n{discord.utils.format_dt(tag["created_at"], "d")}',
        )
        embed.add_field(name="Uses", value=f'{int(tag["uses"]):,}')

        await ctx.send(embed=embed)

    @tag.command(name="raw")
    async def tag_raw(self, ctx: Context, *, name: Annotated[str, TagName(lower=True)]):
        sql = """
        SELECT * FROM tags WHERE LOWER(name) = $1 AND guild_id = $2
        """

        tag = await self.bot.pool.fetchrow(sql, name, ctx.guild.id)

        if tag is None:
            await ctx.send("Tag not found.")
            return

        await ctx.send(
            discord.utils.escape_markdown(tag["content"]),
            reference=ctx.replied_reference,
        )

    @commands.command(name="tags")
    async def tags_command(
        self, ctx: Context, member: discord.Member = commands.Author
    ):
        """Shows a members tags.

        Alias for tag list"""
        sql = """
        SELECT * FROM tags WHERE guild_id = $1 AND author_id = $2 ORDER BY created_at DESC
        """

        results = await self.bot.pool.fetch(sql, ctx.guild.id, member.id)

        if not results:
            await ctx.send(f"{member.mention} has no tags.")
            return

        entries = [
            (
                r["name"],
                f'{discord.utils.format_dt(r["created_at"], "R")}  |  {discord.utils.format_dt(r["created_at"], "d")}',
            )
            for r in results
        ]

        source = FieldPageSource(entries=entries)
        source.embed.title = f"Tags for {member} in {ctx.guild}"
        source.embed.color = self.bot.embedcolor
        pager = Pager(source, ctx=ctx)
        await pager.start(ctx)
