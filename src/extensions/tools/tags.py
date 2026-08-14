from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

import discord
from discord.ext import commands
from discord.utils import escape_markdown

from core import Cog
from utils import SimplePages, extract, ratio

if TYPE_CHECKING:
    from extensions.context import Context, GuildContext


TAG_NAME_LIMIT = 64
TAG_CONTENT_LIMIT = 2000


class Tags(Cog):
    """Guild-scoped user tags."""

    @staticmethod
    def _key(value: str) -> str:
        return " ".join(value.split()).casefold()

    @staticmethod
    def _clean_name(value: str, *, label: str = "Tag name") -> str:
        value = " ".join(value.split())
        if not value:
            raise commands.BadArgument(f"{label} cannot be empty.")
        if len(value) > TAG_NAME_LIMIT:
            raise commands.BadArgument(
                f"{label} must be {TAG_NAME_LIMIT} characters or fewer."
            )
        if any(character.isspace() for character in value):
            raise commands.BadArgument(f"{label} cannot contain spaces.")
        return value

    @classmethod
    def _clean_content(cls, content: str) -> str:
        content = content.strip()
        if not content:
            raise commands.BadArgument("Tag content cannot be empty.")
        if len(content) > TAG_CONTENT_LIMIT:
            raise commands.BadArgument(
                f"Tag content must be {TAG_CONTENT_LIMIT} characters or fewer."
            )
        return content

    @staticmethod
    def _is_manager(ctx: GuildContext) -> bool:
        permissions = ctx.author.guild_permissions
        return (
            permissions.administrator
            or permissions.manage_guild
            or permissions.manage_messages
        )

    async def _tag_rows(self, guild_id: int) -> list[Any]:
        return await self.bot.pool.fetch(
            "SELECT id, guild_id, name, content, aliases, author_id, claimed, "
            "created_at, claimed_at, uses FROM tags WHERE guild_id = $1 "
            "ORDER BY lower(name), id",
            guild_id,
        )

    async def _resolve_tag(self, guild_id: int, value: str) -> tuple[Any, bool] | None:
        key = self._key(value)
        if not key:
            return None
        rows = await self._tag_rows(guild_id)
        for row in rows:
            if self._key(str(row["name"])) == key:
                return row, False
        for row in rows:
            if any(self._key(str(alias)) == key for alias in (row["aliases"] or [])):
                return row, True
        return None

    @staticmethod
    def _owner_id(row: Any) -> int | None:
        value = row["author_id"]
        return int(value) if value is not None else None

    def _can_manage(self, ctx: GuildContext, row: Any) -> bool:
        if not row["claimed"]:
            return True
        owner_id = self._owner_id(row)
        return owner_id is None or owner_id == ctx.author.id or self._is_manager(ctx)

    @staticmethod
    def _owner_text(row: Any) -> str:
        owner_id = row["author_id"]
        if owner_id is None or not row["claimed"]:
            return "Unclaimed"
        return f"<@{int(owner_id)}> (`{int(owner_id)}`)"

    @staticmethod
    def _tag_line(row: Any) -> str:
        return f"**{escape_markdown(str(row['name']))}** (ID: {int(row['id'])})"

    async def _name_taken(
        self, guild_id: int, value: str, *, ignore_id: int | None = None
    ) -> bool:
        key = self._key(value)
        for row in await self._tag_rows(guild_id):
            if ignore_id is not None and int(row["id"]) == ignore_id:
                continue
            if self._key(str(row["name"])) == key:
                return True
            if any(self._key(str(alias)) == key for alias in (row["aliases"] or [])):
                return True
        return False

    async def _send_tag(self, ctx: Context, name: str) -> None:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        result = await self._resolve_tag(ctx.guild.id, name)
        if result is None:
            raise commands.BadArgument("That tag does not exist.")
        row, _ = result
        await self.bot.pool.execute(
            "UPDATE tags SET uses = uses + 1 WHERE id = $1", row["id"]
        )
        await ctx.send(
            str(row["content"]),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.group(name="tag", aliases=("tags",), invoke_without_command=True)
    @commands.guild_only()
    async def tag(self, ctx: GuildContext, *, name: Optional[str] = None) -> None:
        """Create, manage, and use guild tags."""
        if ctx.invoked_subcommand is not None:
            return
        if name:
            await self._send_tag(ctx, name)
        else:
            await ctx.send_help(ctx.command)

    @tag.command(name="create")
    @commands.guild_only()
    async def tag_create(self, ctx: GuildContext, name: str, *, content: str) -> None:
        """Create a tag owned by you."""
        name = self._clean_name(name)
        content = self._clean_content(content)
        if await self._name_taken(ctx.guild.id, name):
            raise commands.BadArgument("That tag name or alias is already in use.")
        await self.bot.pool.execute(
            "INSERT INTO tags (guild_id, name, content, author_id, claimed, claimed_at) "
            "VALUES ($1, $2, $3, $4, TRUE, now())",
            ctx.guild.id,
            name,
            content,
            ctx.author.id,
        )
        await ctx.send(
            f"Created tag **{escape_markdown(name)}**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @tag.command(name="alias")
    @commands.guild_only()
    async def tag_alias(self, ctx: GuildContext, alias: str, tag_name: str) -> None:
        """Add an alias to a tag."""
        alias = self._clean_name(alias, label="Alias")
        result = await self._resolve_tag(ctx.guild.id, tag_name)
        if result is None:
            raise commands.BadArgument("That tag does not exist.")
        row, matched_alias = result
        if matched_alias:
            raise commands.BadArgument("Use the tag's main name when adding an alias.")
        if not self._can_manage(ctx, row):
            raise commands.CheckFailure(
                "Only the tag owner or a moderator can edit it."
            )
        if await self._name_taken(ctx.guild.id, alias, ignore_id=int(row["id"])):
            raise commands.BadArgument("That tag name or alias is already in use.")
        aliases = list(row["aliases"] or [])
        aliases.append(alias)
        await self.bot.pool.execute(
            "UPDATE tags SET aliases = $1 WHERE id = $2", aliases, row["id"]
        )
        await ctx.send(
            f"Added **{escape_markdown(alias)}** as an alias for **{escape_markdown(str(row['name']))}**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @tag.command(name="delete", aliases=("remove",))
    @commands.guild_only()
    async def tag_delete(self, ctx: GuildContext, *, tag_name: str) -> None:
        """Delete a tag or, when an alias is supplied, only that alias."""
        result = await self._resolve_tag(ctx.guild.id, tag_name)
        if result is None:
            raise commands.BadArgument("That tag does not exist.")
        row, matched_alias = result
        if not self._can_manage(ctx, row):
            raise commands.CheckFailure(
                "Only the tag owner or a moderator can delete it."
            )
        if matched_alias:
            key = self._key(tag_name)
            aliases = [
                alias
                for alias in (row["aliases"] or [])
                if self._key(str(alias)) != key
            ]
            await self.bot.pool.execute(
                "UPDATE tags SET aliases = $1 WHERE id = $2", aliases, row["id"]
            )
            await ctx.send(
                f"Removed alias **{escape_markdown(tag_name)}**.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await self.bot.pool.execute("DELETE FROM tags WHERE id = $1", row["id"])
            await ctx.send(
                f"Deleted tag **{escape_markdown(str(row['name']))}**.",
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @tag.command(name="rename")
    @commands.guild_only()
    async def tag_rename(self, ctx: GuildContext, new_name: str, old_name: str) -> None:
        """Rename a tag's main name."""
        new_name = self._clean_name(new_name)
        result = await self._resolve_tag(ctx.guild.id, old_name)
        if result is None:
            raise commands.BadArgument("That tag does not exist.")
        row, matched_alias = result
        if matched_alias:
            raise commands.BadArgument("Use the tag's main name when renaming it.")
        if not self._can_manage(ctx, row):
            raise commands.CheckFailure(
                "Only the tag owner or a moderator can rename it."
            )
        if await self._name_taken(ctx.guild.id, new_name, ignore_id=int(row["id"])):
            raise commands.BadArgument("That tag name or alias is already in use.")
        await self.bot.pool.execute(
            "UPDATE tags SET name = $1 WHERE id = $2", new_name, row["id"]
        )
        await ctx.send(
            f"Renamed **{escape_markdown(str(row['name']))}** to **{escape_markdown(new_name)}**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @tag.command(name="info")
    @commands.guild_only()
    async def tag_info(self, ctx: GuildContext, *, tag_name: str) -> None:
        """Show information about a tag."""
        result = await self._resolve_tag(ctx.guild.id, tag_name)
        if result is None:
            raise commands.BadArgument("That tag does not exist.")
        row, _ = result
        aliases = (
            ", ".join(
                f"`{escape_markdown(str(alias))}`" for alias in (row["aliases"] or [])
            )
            or "None"
        )
        embed = discord.Embed(title=str(row["name"]), color=self.bot.embedcolor)
        embed.description = escape_markdown(str(row["content"]))
        embed.add_field(name="Author", value=self._owner_text(row))
        embed.add_field(name="Aliases", value=aliases, inline=False)
        embed.add_field(name="Uses", value=f"{int(row['uses']):,}")
        embed.set_footer(text=f"ID: {int(row['id'])}")
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @tag.command(name="purge", aliases=("clear",))
    @commands.guild_only()
    @commands.has_guild_permissions(manage_messages=True)
    async def tag_purge(self, ctx: GuildContext, member: discord.Member) -> None:
        """Delete every tag owned by a member."""
        status = await self.bot.pool.execute(
            "DELETE FROM tags WHERE guild_id = $1 AND author_id = $2",
            ctx.guild.id,
            member.id,
        )
        count = int(status.rsplit(" ", 1)[-1])
        await ctx.send(
            f"Deleted {count:,} tag(s) owned by {member.mention}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @tag.command(name="list")
    @commands.guild_only()
    async def tag_list(
        self, ctx: GuildContext, member: Optional[discord.Member] = None
    ) -> None:
        """List tags owned by a member."""
        member = member or ctx.author
        rows = [
            row
            for row in await self._tag_rows(ctx.guild.id)
            if row["author_id"] == member.id
        ]
        if not rows:
            await ctx.send(
                f"{member.mention} does not own any tags.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        entries = [self._tag_line(row) for row in rows]
        pages = SimplePages(entries=entries, per_page=20, ctx=ctx)
        pages.embed.title = f"Tags owned by {member}"
        await pages.start(ctx)

    @tag.command(name="all")
    @commands.guild_only()
    async def tag_all(self, ctx: GuildContext) -> None:
        """List every tag in this server."""
        rows = await self._tag_rows(ctx.guild.id)
        if not rows:
            await ctx.send("This server does not have any tags.")
            return
        entries = [self._tag_line(row) for row in rows]
        pages = SimplePages(entries=entries, per_page=20, ctx=ctx)
        pages.embed.title = f"All tags in {ctx.guild.name}"
        await pages.start(ctx)

    @tag.command(name="raw")
    @commands.guild_only()
    async def tag_raw(self, ctx: GuildContext, *, tag_name: str) -> None:
        """Show a tag's content without rendering Markdown."""
        result = await self._resolve_tag(ctx.guild.id, tag_name)
        if result is None:
            raise commands.BadArgument("That tag does not exist.")
        await ctx.send(
            escape_markdown(str(result[0]["content"])),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @tag.command(name="remove_id")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_messages=True)
    async def tag_remove_id(self, ctx: GuildContext, tag_id: int) -> None:
        """Delete a tag by database ID."""
        status = await self.bot.pool.execute(
            "DELETE FROM tags WHERE guild_id = $1 AND id = $2", ctx.guild.id, tag_id
        )
        if status.endswith(" 0"):
            raise commands.BadArgument("No tag with that ID exists in this server.")
        await ctx.send(
            f"Deleted tag `{tag_id}`.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @tag.command(name="stats")
    @commands.guild_only()
    async def tag_stats(
        self, ctx: GuildContext, member: Optional[discord.Member] = None
    ) -> None:
        """Show tag counts and usage for a member."""
        member = member or ctx.author
        row = await self.bot.pool.fetchrow(
            "SELECT COUNT(*) AS tags, COALESCE(SUM(uses), 0) AS uses "
            "FROM tags WHERE guild_id = $1 AND author_id = $2",
            ctx.guild.id,
            member.id,
        )
        if row is None:
            raise commands.BadArgument("Tag statistics are unavailable right now.")
        await ctx.send(
            f"**Tag stats for {escape_markdown(member.name)}**\n"
            f"Tags: `{int(row['tags']):,}`\nUses: `{int(row['uses']):,}`",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @tag.command(name="transfer")
    @commands.guild_only()
    async def tag_transfer(
        self, ctx: GuildContext, member: discord.Member, tag_name: str
    ) -> None:
        """Transfer a tag to another member."""
        result = await self._resolve_tag(ctx.guild.id, tag_name)
        if result is None:
            raise commands.BadArgument("That tag does not exist.")
        row, matched_alias = result
        if matched_alias:
            raise commands.BadArgument("Use the tag's main name when transferring it.")
        if not self._can_manage(ctx, row):
            raise commands.CheckFailure(
                "Only the tag owner or a moderator can transfer it."
            )
        await self.bot.pool.execute(
            "UPDATE tags SET author_id = $1, claimed = TRUE, claimed_at = now() WHERE id = $2",
            member.id,
            row["id"],
        )
        await ctx.send(
            f"Transferred **{escape_markdown(str(row['name']))}** to {member.mention}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @tag.command(name="search")
    @commands.guild_only()
    async def tag_search(self, ctx: GuildContext, *, name: str) -> None:
        """Search tag names and aliases."""
        query = self._key(name)
        if not query:
            raise commands.BadArgument("Provide a tag name to search for.")
        rows = await self._tag_rows(ctx.guild.id)
        choices: dict[str, Any] = {}
        for row in rows:
            choices[str(row["name"])] = row
            for alias in row["aliases"] or []:
                choices[str(alias)] = row
        matches = extract(query, choices, scorer=ratio, score_cutoff=35, limit=20)
        if not matches:
            await ctx.send("No matching tags found.")
            return
        entries: list[str] = []
        seen: set[int] = set()
        for _, _, row in matches:
            tag_id = int(row["id"])
            if tag_id in seen:
                continue
            seen.add(tag_id)
            entries.append(self._tag_line(row))
        pages = SimplePages(entries=entries, per_page=20, ctx=ctx)
        pages.embed.title = f"Tag search results for {escape_markdown(name)}"
        await pages.start(ctx)

    @tag.command(name="claim")
    @commands.guild_only()
    async def tag_claim(
        self, ctx: GuildContext, *, tag_name: Optional[str] = None
    ) -> None:
        """Claim an unclaimed tag, or list tags available to claim."""
        if not tag_name:
            rows = [
                row for row in await self._tag_rows(ctx.guild.id) if not row["claimed"]
            ]
            if not rows:
                await ctx.send("There are no unclaimed tags in this server.")
                return
            names = ", ".join(f"`{escape_markdown(str(row['name']))}`" for row in rows)
            await ctx.send(
                f"Unclaimed tags: {names}\nUse `fish tag claim <name>` to claim one.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        result = await self._resolve_tag(ctx.guild.id, tag_name)
        if result is None:
            raise commands.BadArgument("That tag does not exist.")
        row, _ = result
        if row["claimed"]:
            raise commands.BadArgument("That tag is already claimed.")
        updated = await self.bot.pool.fetchrow(
            "UPDATE tags SET author_id = $1, claimed = TRUE, claimed_at = now() "
            "WHERE id = $2 AND claimed = FALSE RETURNING name",
            ctx.author.id,
            row["id"],
        )
        if updated is None:
            raise commands.BadArgument("That tag was claimed by someone else first.")
        await ctx.send(
            f"You claimed **{escape_markdown(str(updated['name']))}**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @tag.command(name="edit")
    @commands.guild_only()
    async def tag_edit(self, ctx: GuildContext, name: str, *, new_content: str) -> None:
        """Edit a tag's content."""
        content = self._clean_content(new_content)
        result = await self._resolve_tag(ctx.guild.id, name)
        if result is None:
            raise commands.BadArgument("That tag does not exist.")
        row, matched_alias = result
        if matched_alias:
            raise commands.BadArgument("Use the tag's main name when editing it.")
        if not self._can_manage(ctx, row):
            raise commands.CheckFailure(
                "Only the tag owner or a moderator can edit it."
            )
        await self.bot.pool.execute(
            "UPDATE tags SET content = $1 WHERE id = $2", content, row["id"]
        )
        await ctx.send(
            f"Updated **{escape_markdown(str(row['name']))}**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.Cog.listener("on_member_remove")
    async def _tag_member_remove(self, member: discord.Member) -> None:
        """Release tags when their owner leaves instead of deleting them."""
        await self.bot.pool.execute(
            "UPDATE tags SET claimed = FALSE, claimed_at = NULL "
            "WHERE guild_id = $1 AND author_id = $2",
            member.guild.id,
            member.id,
        )
