from __future__ import annotations

import re
import shlex
import zipfile
from io import BytesIO
from typing import TYPE_CHECKING, Any, List, Optional, Union

import discord
import emoji as emoji_lib
from discord import app_commands
from discord.ext import commands

from core import Cog
from utils import (
    EMOJI_RE,
    SimplePages,
    TwemojiConverter,
    get_or_fetch_user,
    human_join,
    plural,
    to_image,
    to_thread,
)

CUSTOM_EMOJI_STATS_RE = re.compile(r"^<a?:[A-Za-z0-9_~]+:(\d+)>$")


def _emoji_occurrences(content: str) -> list[tuple[str, bool]]:
    """Return custom and Twemoji-supported Unicode emoji occurrences."""
    occurrences: list[tuple[str, bool]] = []
    custom_spans: list[tuple[int, int]] = []
    for match in EMOJI_RE.finditer(content):
        custom_spans.append((match.start(), match.end()))
        occurrences.append((match.group(0).split(":")[-1][:-1], False))

    for item in emoji_lib.emoji_list(content):
        start = int(item["match_start"])
        end = int(item["match_end"])
        if any(
            start < custom_end and end > custom_start
            for custom_start, custom_end in custom_spans
        ):
            continue
        value = str(item["emoji"])
        if TwemojiConverter.is_unicode_emoji(value):
            occurrences.append((value, True))
    return occurrences


if TYPE_CHECKING:
    from extensions.context import Context, GuildContext


@to_thread
def _create_emoji_zip(emoji_data: list[tuple[str, bytes]]) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in emoji_data:
            zf.writestr(name, data)
    return buf.getvalue()


class Emojis(Cog):
    @commands.Cog.listener("on_message")
    async def _record_emoji_stats(self, message: discord.Message) -> None:
        """Record emoji identifiers without retaining message content."""
        if message.guild is None or message.author.bot or not message.content:
            return
        if self.bot.db_cache.user_tracking_opted_out(message.author.id, "emoji"):
            return
        if "emoji" in self.bot.db_cache.get_opted_out(message.guild.id):
            return

        occurrences = _emoji_occurrences(message.content)
        if not occurrences:
            return

        await self.bot.pool.executemany(
            "INSERT INTO emoji_stats (author_id, emoji_id, guild_id, unicode) "
            "VALUES ($1, $2, $3, $4)",
            [
                (message.author.id, emoji_id, message.guild.id, is_unicode)
                for emoji_id, is_unicode in occurrences
            ],
        )

    async def steal_stickers(
        self, ctx: GuildContext, stickers: List[discord.StickerItem]
    ):
        """Function for stealing stickers"""
        message = await ctx.send("Stealing sticker")

        for StickerItem in stickers:
            sticker = await StickerItem.fetch()
            if isinstance(sticker, discord.StandardSticker):
                await message.edit(content="Cannot steal that type of sticker.")
                return

            image = await to_image(ctx.session, sticker.url)
            file = discord.File(fp=image)
            sticker = await ctx.guild.create_sticker(
                name=sticker.name,
                description=sticker.description or "Not provided",
                emoji=sticker.emoji or "wave",  # type: ignore # dumb false error
                file=file,
            )

        await message.edit(content="Successfully stole sticker.")
        await ctx.send(stickers=[sticker])

    async def steal_emojis(self, ctx: GuildContext, emoji_results: List[Any]):
        """Function for stealing emojis"""

        message = await ctx.send("Stealing emojis...")

        completed_emojis = []
        for result in emoji_results:
            emoji = await commands.PartialEmojiConverter().convert(ctx, result)

            if emoji is None:
                continue

            try:
                e = await ctx.guild.create_custom_emoji(
                    name=emoji.name, image=await emoji.read()
                )
                completed_emojis.append(str(e))
            except discord.HTTPException:
                pass

            await message.edit(
                content=f"Successfully stole {human_join(completed_emojis, final='and')} *({len(completed_emojis)}/{len(emoji_results)})*."
            )

    @commands.group(name="emoji", invoke_without_command=True)
    async def emoji_group(
        self,
        ctx: Context,
        emoji: Union[discord.Emoji, discord.PartialEmoji, TwemojiConverter],
    ):
        """Gets information about an emoji"""
        if isinstance(emoji, BytesIO):
            return await ctx.send(file=discord.File(emoji, "emoji.png"))

        if emoji is None:
            if not ctx.message.reference:
                raise commands.BadArgument("No emoji provided.")

            ref = ctx.message.reference.resolved

            if ref is None or isinstance(ref, discord.DeletedReferencedMessage):
                raise commands.BadArgument("No emoji found.")

            try:
                emoji = await commands.PartialEmojiConverter().convert(ctx, ref.content)
            except (commands.CommandError, commands.BadArgument):
                raise commands.BadArgument("No emoji found.")

        if isinstance(emoji, TwemojiConverter):
            raise commands.BadArgument("No emoji found.")

        embed = discord.Embed(
            timestamp=emoji.created_at,
            title=emoji.name,
            color=self.bot.embedcolor,
            description="**Raw text**: `<:{emoji.name}\u200b:{emoji.id}>`",
        )
        embed.description = f"**Raw text**: `<:{emoji.name}\u200b:{emoji.id}>`"

        if isinstance(emoji, discord.Emoji):
            if emoji.guild:
                assert embed.title

                embed.title += f" - {emoji.guild} ({emoji.guild_id})"

                try:
                    femoji = await emoji.guild.fetch_emoji(emoji.id)
                except discord.HTTPException:
                    femoji = None

                if femoji:
                    if femoji.user:
                        embed.description += (
                            f"\n**Author**: `{femoji.user} (ID: {femoji.user.id})`"
                        )

        embed.set_footer(text=f"ID: {emoji.id}\nCreated at")
        file = await emoji.to_file()
        embed.set_image(url=f"attachment://{file.filename}")

        await ctx.send(embed=embed, file=file)

    async def _resolve_stats_user(
        self, ctx: Context, value: str | None
    ) -> discord.User | discord.Member:
        if value is None:
            return ctx.author

        if ctx.guild is not None:
            try:
                return await commands.MemberConverter().convert(ctx, value)
            except commands.CommandError:
                pass

        try:
            return await commands.UserConverter().convert(ctx, value)
        except commands.CommandError as exc:
            raise commands.BadArgument("I could not find that Discord user.") from exc

    async def _resolve_stats_emoji(
        self, ctx: Context, value: str
    ) -> tuple[str, bool] | None:
        """Resolve a custom or Unicode emoji to the values stored in ``emoji_stats``."""
        match = CUSTOM_EMOJI_STATS_RE.fullmatch(value)
        if match:
            return match.group(1), False

        if emoji_lib.is_emoji(value):
            return value, True

        # Accept a custom emoji name as a convenience.  Prefer the current
        # guild, then fall back to any emoji the bot can currently see.
        name = value.strip(":")
        candidates = (
            list(getattr(ctx.guild, "emojis", ())) if ctx.guild is not None else []
        )
        candidates.extend(getattr(self.bot, "emojis", ()))
        for candidate in candidates:
            if candidate.name and candidate.name.casefold() == name.casefold():
                return str(candidate.id), False
        return None

    async def _parse_emoji_stats_arguments(
        self, ctx: Context, arguments: str | None
    ) -> tuple[
        bool,
        discord.User | discord.Member | None,
        discord.Guild | None,
        tuple[str, bool] | None,
    ]:
        """Parse global, user, server, and emoji filters in any order."""
        try:
            tokens = shlex.split(arguments or "")
        except ValueError as exc:
            raise commands.BadArgument(
                "I could not parse those emoji-stat arguments."
            ) from exc

        global_scope = False
        user: discord.User | discord.Member | None = None
        guild: discord.Guild | None = None
        emoji_filter: tuple[str, bool] | None = None
        unknown: list[str] = []
        pending_user = False
        pending_guild = False
        pending_emoji = False

        for token in tokens:
            lowered = token.casefold()
            if pending_emoji:
                parsed_emoji = await self._resolve_stats_emoji(ctx, token)
                if parsed_emoji is None:
                    raise commands.BadArgument(
                        f"I could not find an emoji for `{token}`."
                    )
                emoji_filter = parsed_emoji
                pending_emoji = False
                continue
            if pending_user:
                if user is not None:
                    raise commands.BadArgument("Please provide only one user.")
                user = await self._resolve_stats_user(ctx, token)
                pending_user = False
                continue
            if pending_guild:
                if guild is not None:
                    raise commands.BadArgument("Please provide only one server.")
                try:
                    guild = await commands.GuildConverter().convert(ctx, token)
                except commands.CommandError as exc:
                    raise commands.BadArgument(
                        f"I could not find a server for `{token}`."
                    ) from exc
                pending_guild = False
                continue
            if lowered == "global":
                global_scope = True
                continue
            if lowered in {"user", "me", "self"}:
                if user is not None:
                    raise commands.BadArgument("Please provide only one user.")
                pending_user = True
                continue
            if lowered in {"server", "guild", "here"}:
                if guild is not None:
                    raise commands.BadArgument("Please provide only one server.")
                pending_guild = True
                continue
            # Accept an explicit ``emoji`` marker so the dynamic syntax can
            # be written as ``emoji stats global emoji :name:`` without
            # changing how a bare emoji is resolved.
            if lowered == "emoji":
                pending_emoji = True
                continue

            parsed_emoji = await self._resolve_stats_emoji(ctx, token)
            if parsed_emoji is not None:
                if emoji_filter is not None:
                    raise commands.BadArgument("Please provide only one emoji.")
                emoji_filter = parsed_emoji
                continue

            mention_or_id = bool(re.fullmatch(r"<@!?\d+>", token)) or token.isdigit()
            if mention_or_id and user is None:
                # Numeric IDs can identify either a server or a user.  As with
                # reaction stats, prefer a guild that Fishie is currently in.
                if not token.startswith("<@"):
                    try:
                        guild_candidate = await commands.GuildConverter().convert(
                            ctx, token
                        )
                    except commands.CommandError:
                        guild_candidate = None
                    if guild_candidate is not None:
                        guild = guild_candidate
                        continue
                user = await self._resolve_stats_user(ctx, token)
                continue

            try:
                guild_candidate = await commands.GuildConverter().convert(ctx, token)
            except commands.CommandError:
                guild_candidate = None
            if guild_candidate is not None:
                if guild is not None:
                    raise commands.BadArgument("Please provide only one server.")
                guild = guild_candidate
                continue

            if user is None:
                user = await self._resolve_stats_user(ctx, token)
                continue
            unknown.append(token)

        if unknown:
            raise commands.BadArgument(f"I did not understand `{unknown[0]}`.")
        if pending_user:
            user = ctx.author
        if pending_guild:
            guild = ctx.guild
        if pending_emoji:
            raise commands.BadArgument("Please provide an emoji to filter by.")
        # A global query always wins over a supplied server. Keep the scope
        # unambiguous when users provide both filters in either order.
        if global_scope:
            guild = None
        elif guild is None:
            guild = ctx.guild
        if not global_scope and guild is None:
            raise commands.BadArgument("Choose a server or use `global` in DMs.")
        return global_scope, user, guild, emoji_filter

    @staticmethod
    def _emoji_stats_scope_label(
        global_scope: bool, guild: discord.Guild | None
    ) -> str:
        return "Global" if global_scope else (guild.name if guild else "Server")

    def _emoji_stats_title(
        self,
        *,
        global_scope: bool,
        guild: discord.Guild | None,
        user: discord.User | discord.Member | None,
        emoji_filter: tuple[str, bool] | None,
    ) -> str:
        scope = self._emoji_stats_scope_label(global_scope, guild)
        if user is not None:
            return f"Emoji stats for {user.name} · {scope}"
        if emoji_filter is not None:
            emoji_id, is_unicode = emoji_filter
            display = emoji_id if is_unicode else f"<:emoji:{emoji_id}>"
            if not is_unicode:
                custom = self.bot.get_emoji(int(emoji_id))
                if custom is not None:
                    display = str(custom)
            return f"{display} Emoji stats · {scope}"
        return f"Emoji stats · {scope}"

    async def _send_emoji_stats(
        self,
        ctx: Context,
        *,
        title: str,
        guild_id: int | None = None,
        author_id: int | None = None,
        emoji_filter: tuple[str, bool] | None = None,
    ) -> None:
        clauses: list[str] = []
        arguments: list[object] = []
        if guild_id is not None:
            arguments.append(guild_id)
            clauses.append(f"guild_id = ${len(arguments)}")
        if author_id is not None:
            arguments.append(author_id)
            clauses.append(f"author_id = ${len(arguments)}")
        if emoji_filter is not None:
            emoji_id, is_unicode = emoji_filter
            arguments.append(emoji_id)
            emoji_index = len(arguments)
            arguments.append(is_unicode)
            unicode_index = len(arguments)
            clauses.append(f"emoji_id = ${emoji_index} AND unicode = ${unicode_index}")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        if emoji_filter is not None and author_id is None:
            rows = await self.bot.pool.fetch(
                "SELECT author_id, COUNT(*) AS uses FROM emoji_stats "
                f"{where} GROUP BY author_id ORDER BY uses DESC, author_id LIMIT 50",
                *arguments,
            )
            entries: list[str] = []
            for row in rows:
                user = await get_or_fetch_user(self.bot, int(row["author_id"]))
                name = getattr(user, "name", None) or str(row["author_id"])
                entries.append(
                    f"**{discord.utils.escape_markdown(name)}** ({int(row['uses']):,} uses)"
                )
        else:
            rows = await self.bot.pool.fetch(
                "SELECT emoji_id, unicode, COUNT(*) AS uses FROM emoji_stats "
                f"{where} GROUP BY emoji_id, unicode ORDER BY uses DESC, emoji_id LIMIT 50",
                *arguments,
            )
            entries = []
            for row in rows:
                emoji_id = str(row["emoji_id"])
                if row["unicode"]:
                    display = emoji_id
                else:
                    try:
                        custom = self.bot.get_emoji(int(emoji_id))
                    except (TypeError, ValueError):
                        custom = None
                    display = (
                        str(custom)
                        if custom is not None
                        else f"Custom emoji `{emoji_id}`"
                    )
                entries.append(f"{display} ({int(row['uses']):,} uses)")

        if not entries:
            await ctx.send(f"{title}\nNo emoji usage has been recorded yet.")
            return

        pages = SimplePages(entries=entries, per_page=10, ctx=ctx)
        pages.embed.title = title
        pages.embed.colour = self.bot.embedcolor
        pages.embed.set_footer(
            text="Only valid Twemoji emoji and custom emoji are counted."
        )
        await pages.start(ctx)

    async def send_emoji_stats(
        self, ctx: Context, target: Optional[str] = None
    ) -> None:
        """Send emoji usage with dynamic global/user/server/emoji filters."""
        (
            global_scope,
            user,
            guild,
            emoji_filter,
        ) = await self._parse_emoji_stats_arguments(ctx, target)
        await self._send_emoji_stats(
            ctx,
            title=self._emoji_stats_title(
                global_scope=global_scope,
                guild=guild,
                user=user,
                emoji_filter=emoji_filter,
            ),
            guild_id=guild.id if guild is not None else None,
            author_id=user.id if user is not None else None,
            emoji_filter=emoji_filter,
        )

    async def send_global_emoji_stats(
        self, ctx: Context, target: Optional[str] = None
    ) -> None:
        """Send a user's emoji usage across all servers."""
        # Preserve the legacy ``emoji stats global`` subcommand behavior,
        # which defaults to the invoking user's global history. The dynamic
        # parent command still uses bare ``global`` for an all-user view.
        arguments = "global user" if target is None else f"global {target}"
        await self.send_emoji_stats(ctx, arguments)

    @emoji_group.group(
        name="stats",
        invoke_without_command=True,
        extras={"usage": "[global] [user] [server] [emoji]"},
    )
    async def emoji_stats(self, ctx: Context, *, arguments: str | None = None) -> None:
        """Show emoji usage with global, user, server, or emoji filters."""
        await self.send_emoji_stats(ctx, arguments)

    @emoji_stats.command(name="global")
    async def emoji_stats_global(
        self, ctx: Context, target: Optional[str] = None
    ) -> None:
        """Show a user's emoji usage across all servers."""
        await self.send_global_emoji_stats(ctx, target)

    @emoji_group.command(name="create")
    @commands.has_permissions(manage_emojis=True)
    @commands.bot_has_permissions(manage_emojis=True)
    async def emoji_create(self, ctx: GuildContext, name: str, url: str):
        """Create a custom emoji from an image URL."""
        try:
            image = await to_image(ctx.session, url, bytes=True)
            if isinstance(image, BytesIO):
                raise commands.BadArgument("Invalid image.")

            emoji = await ctx.guild.create_custom_emoji(
                name=name,
                image=image,
                reason=f"Created by {ctx.author} ({ctx.author.id})",
            )
        except discord.HTTPException as e:
            raise commands.BadArgument(f"Couldn't create emoji. \n{e}")

        await ctx.send(f"Successfully created {emoji}")

    @emoji_group.command(name="delete")
    @commands.has_permissions(manage_emojis=True)
    @commands.bot_has_permissions(manage_emojis=True)
    async def emoji_delete(self, ctx: GuildContext, *emojis: discord.Emoji):
        """Delete one or more custom emojis from the server."""
        value = await ctx.prompt(
            f"Are you sure you want to delete {plural(len(emojis)):emoji}?"
        )

        if not value:
            return

        message = await ctx.send(f"Deleting {plural(len(emojis)):emoji}...")

        deleted_emojis = []

        for emoji in emojis:
            deleted_emojis.append(f"`{emoji}`")
            await emoji.delete()
            await message.edit(
                content=f"Successfully deleted {human_join(deleted_emojis, final='and')} *({len(deleted_emojis)}/{len(emojis)})*."
            )

    @emoji_group.command(name="rename")
    @commands.has_permissions(manage_emojis=True)
    @commands.bot_has_permissions(manage_emojis=True)
    async def emoji_rename(self, ctx: GuildContext, emoji: discord.Emoji, *, name: str):
        """Rename a custom emoji in the server."""
        try:
            emoji = await emoji.edit(name=name)
            await ctx.send(f"Renamed {emoji}")
        except Exception as e:
            raise commands.BadArgument(f"Failed to rename emoji\n{e}")

    @commands.command(name="steal", aliases=("copy", "clone"))
    @commands.has_permissions(manage_emojis_and_stickers=True)
    @commands.bot_has_permissions(manage_emojis_and_stickers=True)
    async def steal(self, ctx: GuildContext, *, emojis: Optional[str]):
        """Clones emojis/stickers to the current server"""

        if ctx.ref:
            if ctx.ref.stickers:
                return await self.steal_stickers(ctx, ctx.ref.stickers)

            emojis = ctx.ref.content

        if not emojis:
            raise commands.BadArgument("No emojis found.")

        emoji_results = EMOJI_RE.findall(emojis)

        if not bool(emoji_results):
            raise commands.BadArgument("No emojis found.")

        await self.steal_emojis(ctx, emoji_results)

    @commands.group(name="emojis", invoke_without_command=True)
    @app_commands.describe(
        guild="Server whose emojis should be listed.",
        name="Show emoji names instead of the emoji itself.",
        ids="Include each emoji ID in the results.",
    )
    async def emojis(
        self,
        ctx: Context,
        guild: discord.Guild = commands.CurrentGuild,
        name: Optional[bool] = commands.param(
            default=False, description="Shows the name rather than emoji"
        ),
        ids: Optional[bool] = commands.param(
            default=False, description="Shows IDs next to the emoji"
        ),
    ):
        """Get the server emojis."""
        if not guild.emojis:
            raise commands.GuildNotFound("Guild has no emojis")

        order = sorted(guild.emojis, key=lambda e: e.created_at)

        data = [
            f"{f'`{e.name}`' if name else str(e)} {f'`{e.id}`' if ids else ''} *{discord.utils.format_dt(e.created_at, 'd')}*"
            for e in order
        ]
        pages = SimplePages(entries=data, per_page=10, ctx=ctx)
        pages.embed.title = f"Emojis for {guild.name}"
        await pages.start(ctx)

    @emojis.command(name="download")
    @app_commands.describe(disabled="Include emojis that are currently disabled.")
    @commands.has_permissions(manage_emojis=True)
    @commands.bot_has_permissions(manage_emojis=True)
    async def emoji_download(
        self,
        ctx: GuildContext,
        disabled: Optional[bool] = commands.param(
            default=True, description="Download includes disabled emojis"
        ),
    ):
        """Download all server emojis as a zip file."""

        async with ctx.typing():
            emojis = [e for e in ctx.guild.emojis if disabled or e.available]

            if not emojis:
                raise commands.BadArgument("No emojis found in this server.")

            emoji_data: list[tuple[str, bytes]] = []
            for emoji in emojis:
                ext = "gif" if emoji.animated else "png"
                try:
                    data = await emoji.read()
                    emoji_data.append((f"{emoji.name}.{ext}", data))
                except discord.HTTPException:
                    continue

            if not emoji_data:
                raise commands.BadArgument("Failed to download any emoji data.")

            zip_bytes = await _create_emoji_zip(emoji_data)

            max_size = ctx.guild.filesize_limit if ctx.guild else 25 * 1024 * 1024

            if len(zip_bytes) > max_size:
                from utils import litterbox

                url = await litterbox(ctx.session, zip_bytes, "emojis.zip")
                await ctx.send(
                    f"Emoji zip was too large for Discord, uploaded [here]({url})."
                )
            else:
                await ctx.send(file=discord.File(BytesIO(zip_bytes), "emojis.zip"))
