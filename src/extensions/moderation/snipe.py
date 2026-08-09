from __future__ import annotations

import datetime
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from core import Cog

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import GuildContext


SNIPE_TTL_SECONDS = 10 * 60
SNIPE_CACHE_LIMIT = 5_000


@dataclass(frozen=True, slots=True)
class CachedMessage:
    guild_id: int
    channel_id: int
    message_id: int
    author_id: int
    author_name: str
    author_avatar_url: str | None
    content: str
    attachment_urls: tuple[str, ...]
    created_at: datetime.datetime


class Snipe(Cog):
    """Temporary deleted and edited message snipes for enabled servers."""

    _snipe_enabled_guilds: set[int]
    _editsnipe_enabled_guilds: set[int]
    _snipe_messages: dict[int, CachedMessage]
    _last_snipes: dict[tuple[int, int], tuple[CachedMessage, float]]
    _last_editsnipes: dict[tuple[int, int], tuple[CachedMessage, CachedMessage, float]]
    _snipe_last_prune: float

    async def _load_snipe_settings(self) -> None:
        rows = await self.bot.pool.fetch(
            "SELECT guild_id, snipe_enabled, editsnipe_enabled FROM guild_settings "
            "WHERE snipe_enabled = TRUE OR editsnipe_enabled = TRUE"
        )
        self._snipe_enabled_guilds = {
            int(row["guild_id"]) for row in rows if row["snipe_enabled"]
        }
        self._editsnipe_enabled_guilds = {
            int(row["guild_id"]) for row in rows if row["editsnipe_enabled"]
        }

    @staticmethod
    def _safe_text(value: str | None, *, fallback: str = "None") -> str:
        if not value:
            return fallback
        escaped = discord.utils.escape_mentions(discord.utils.escape_markdown(value))
        return escaped[:4093] + "..." if len(escaped) > 4096 else escaped

    @staticmethod
    def _snapshot(message: discord.Message) -> CachedMessage:
        if message.guild is None:
            raise ValueError("Snipe snapshots require a guild message")
        author = message.author
        created_at = message.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=datetime.timezone.utc)
        return CachedMessage(
            guild_id=message.guild.id,
            channel_id=message.channel.id,
            message_id=message.id,
            author_id=author.id,
            author_name=str(author),
            author_avatar_url=getattr(author.display_avatar, "url", None),
            content=message.content,
            attachment_urls=tuple(attachment.url for attachment in message.attachments),
            created_at=created_at,
        )

    def _prune_snipe_cache(self, *, force: bool = False) -> None:
        now = time.monotonic()
        wall_now = time.time()
        if (
            not force
            and now - self._snipe_last_prune < 30
            and len(self._snipe_messages) <= SNIPE_CACHE_LIMIT
        ):
            return

        cutoff = now - SNIPE_TTL_SECONDS
        self._snipe_messages = {
            message_id: message
            for message_id, message in self._snipe_messages.items()
            if wall_now - message.created_at.timestamp() < SNIPE_TTL_SECONDS
        }
        self._last_snipes = {
            key: value for key, value in self._last_snipes.items() if value[1] >= cutoff
        }
        self._last_editsnipes = {
            key: value
            for key, value in self._last_editsnipes.items()
            if value[2] >= cutoff
        }
        if len(self._snipe_messages) > SNIPE_CACHE_LIMIT:
            newest = sorted(
                self._snipe_messages.values(),
                key=lambda message: message.created_at,
                reverse=True,
            )[:SNIPE_CACHE_LIMIT]
            self._snipe_messages = {message.message_id: message for message in newest}
        self._snipe_last_prune = now

    def _author_opted_out(self, author_id: int) -> bool:
        return self.bot.db_cache.user_tracking_opted_out(author_id, "snipe")

    async def _set_snipe_enabled(
        self, ctx: GuildContext, *, kind: str, enabled: bool
    ) -> None:
        if kind == "snipe":
            column = "snipe_enabled"
            enabled_guilds = self._snipe_enabled_guilds
            label = "Deleted-message sniping"
        else:
            column = "editsnipe_enabled"
            enabled_guilds = self._editsnipe_enabled_guilds
            label = "Edited-message sniping"

        await self.bot.pool.execute(
            f"""
            INSERT INTO guild_settings (guild_id, {column})
            VALUES ($1, $2)
            ON CONFLICT (guild_id) DO UPDATE SET {column} = EXCLUDED.{column}
            """,
            ctx.guild.id,
            enabled,
        )
        if enabled:
            enabled_guilds.add(ctx.guild.id)
            await ctx.send(f"{label} is now enabled for this server.")
            return

        enabled_guilds.discard(ctx.guild.id)
        self._snipe_messages = {
            message_id: message
            for message_id, message in self._snipe_messages.items()
            if message.guild_id != ctx.guild.id
        }
        self._last_snipes = {
            key: value
            for key, value in self._last_snipes.items()
            if key[0] != ctx.guild.id
        }
        self._last_editsnipes = {
            key: value
            for key, value in self._last_editsnipes.items()
            if key[0] != ctx.guild.id
        }
        await ctx.send(f"{label} is now disabled for this server.")

    @staticmethod
    def _target_channel(
        ctx: GuildContext, channel: discord.TextChannel | None
    ) -> discord.abc.GuildChannel | None:
        target = channel or ctx.channel
        guild = getattr(target, "guild", None)
        if guild is None or guild.id != ctx.guild.id:
            return None
        permissions_for = getattr(target, "permissions_for", None)
        if permissions_for is None:
            return None
        permissions = permissions_for(ctx.author)
        if not permissions.view_channel or not permissions.read_message_history:
            return None
        return target

    @staticmethod
    def _message_view(
        message: CachedMessage,
        *,
        color: discord.Colour,
        description: str,
        attachments: tuple[str, ...] = (),
    ) -> discord.ui.LayoutView:
        author_name = discord.utils.escape_mentions(
            discord.utils.escape_markdown(message.author_name)
        )
        children: list[discord.ui.Item] = [
            discord.ui.TextDisplay(
                f"**{author_name}** (`ID: {message.author_id}`)\n\n{description}"
            )
        ]
        if attachments:
            children.append(
                discord.ui.MediaGallery(
                    *(discord.MediaGalleryItem(url) for url in attachments)
                )
            )
        children.append(
            discord.ui.TextDisplay(
                "-# "
                f"Message ID: {message.message_id} • "
                f"{discord.utils.format_dt(message.created_at, 'R')}"
            )
        )
        container = discord.ui.Container(*children, accent_color=color)
        view_type = type("SnipeView", (discord.ui.LayoutView,), {})
        view = view_type(timeout=None)
        view.add_item(container)
        return view

    async def _show_snipe(
        self,
        ctx: GuildContext,
        *,
        edited: bool,
        channel: discord.TextChannel | None = None,
    ) -> None:
        guild_id = ctx.guild.id
        target_channel = self._target_channel(ctx, channel)
        if target_channel is None:
            await ctx.send(
                "I cannot read that channel, or it does not belong to this server."
            )
            return
        channel_id = target_channel.id
        if edited:
            if guild_id not in self._editsnipe_enabled_guilds:
                await ctx.send("Edited-message sniping is disabled for this server.")
                return
            stored = self._last_editsnipes.get((guild_id, channel_id))
            if stored is None or time.monotonic() - stored[2] > SNIPE_TTL_SECONDS:
                await ctx.send("There are no recently edited messages to show.")
                return
            before, after, _ = stored
            if self._author_opted_out(after.author_id):
                await ctx.send("There are no recently edited messages to show.")
                return
            description = (
                "**Before**\n"
                f"{self._safe_text(before.content, fallback='No text content.')[:1800]}\n\n"
                "**After**\n"
                f"{self._safe_text(after.content, fallback='No text content.')[:1800]}"
            )
            view = self._message_view(
                after,
                color=discord.Colour.orange(),
                description=description,
                attachments=after.attachment_urls,
            )
        else:
            if guild_id not in self._snipe_enabled_guilds:
                await ctx.send("Deleted-message sniping is disabled for this server.")
                return
            stored = self._last_snipes.get((guild_id, channel_id))
            if stored is None or time.monotonic() - stored[1] > SNIPE_TTL_SECONDS:
                await ctx.send("There are no recently deleted messages to show.")
                return
            message, _ = stored
            if self._author_opted_out(message.author_id):
                await ctx.send("There are no recently deleted messages to show.")
                return
            view = self._message_view(
                message,
                color=discord.Colour.red(),
                description=self._safe_text(
                    message.content, fallback="No text content."
                ),
                attachments=message.attachment_urls,
            )

        await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener("on_message")
    async def snipe_message(self, message: discord.Message) -> None:
        guild = message.guild
        if (
            guild is None
            or message.author.bot
            or guild.id
            not in self._snipe_enabled_guilds | self._editsnipe_enabled_guilds
            or self._author_opted_out(message.author.id)
        ):
            return
        self._prune_snipe_cache()
        self._snipe_messages[message.id] = self._snapshot(message)

    @commands.Cog.listener("on_message_edit")
    async def snipe_message_edit(
        self, before: discord.Message, after: discord.Message
    ) -> None:
        guild = before.guild or after.guild
        if (
            guild is None
            or before.guild is None
            or after.guild is None
            or before.author.bot
        ):
            return
        if self._author_opted_out(before.author.id):
            self._snipe_messages.pop(before.id, None)
            return

        if guild.id in self._snipe_enabled_guilds:
            self._snipe_messages[after.id] = self._snapshot(after)
        if guild.id not in self._editsnipe_enabled_guilds:
            return
        before_attachments = tuple(attachment.url for attachment in before.attachments)
        after_attachments = tuple(attachment.url for attachment in after.attachments)
        if before.content == after.content and before_attachments == after_attachments:
            return
        self._prune_snipe_cache()
        self._last_editsnipes[(guild.id, after.channel.id)] = (
            self._snapshot(before),
            self._snapshot(after),
            time.monotonic(),
        )

    @commands.Cog.listener("on_raw_message_delete")
    async def snipe_message_delete(
        self, payload: discord.RawMessageDeleteEvent
    ) -> None:
        if (
            payload.guild_id is None
            or payload.guild_id not in self._snipe_enabled_guilds
        ):
            self._snipe_messages.pop(payload.message_id, None)
            return
        message = self._snipe_messages.pop(payload.message_id, None)
        if message is None and payload.cached_message is not None:
            message = self._snapshot(payload.cached_message)
        if message is None or self._author_opted_out(message.author_id):
            return
        self._prune_snipe_cache()
        self._last_snipes[(payload.guild_id, payload.channel_id)] = (
            message,
            time.monotonic(),
        )

    @commands.Cog.listener("on_raw_bulk_message_delete")
    async def snipe_bulk_message_delete(
        self, payload: discord.RawBulkMessageDeleteEvent
    ) -> None:
        snapshots = [
            self._snipe_messages.pop(message_id, None)
            for message_id in payload.message_ids
        ]
        if (
            payload.guild_id is None
            or payload.guild_id not in self._snipe_enabled_guilds
        ):
            return
        eligible = [
            message
            for message in snapshots
            if message is not None and not self._author_opted_out(message.author_id)
        ]
        if not eligible:
            return
        message = max(eligible, key=lambda item: item.created_at)
        self._prune_snipe_cache()
        self._last_snipes[(payload.guild_id, payload.channel_id)] = (
            message,
            time.monotonic(),
        )

    @commands.hybrid_group(name="snipe", fallback="show")
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    @app_commands.describe(
        channel="The channel to snipe in. Defaults to the current channel."
    )
    async def snipe(
        self,
        ctx: GuildContext,
        channel: discord.TextChannel | None = None,
    ) -> None:
        """Show the most recently deleted message in this channel."""
        await self._show_snipe(ctx, edited=False, channel=channel)

    @snipe.command(name="enable")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def snipe_enable(self, ctx: GuildContext) -> None:
        """Enable deleted-message sniping for this server."""
        await self._set_snipe_enabled(ctx, kind="snipe", enabled=True)

    @snipe.command(name="disable")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def snipe_disable(self, ctx: GuildContext) -> None:
        """Disable deleted-message sniping for this server."""
        await self._set_snipe_enabled(ctx, kind="snipe", enabled=False)

    @commands.hybrid_group(name="editsnipe", aliases=("esnipe",), fallback="show")
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    @app_commands.describe(
        channel="The channel to snipe in. Defaults to the current channel."
    )
    async def editsnipe(
        self,
        ctx: GuildContext,
        channel: discord.TextChannel | None = None,
    ) -> None:
        """Show the most recently edited message in this channel."""
        await self._show_snipe(ctx, edited=True, channel=channel)

    @editsnipe.command(name="enable")
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def editsnipe_enable(self, ctx: GuildContext) -> None:
        """Enable edited-message sniping for this server."""
        await self._set_snipe_enabled(ctx, kind="editsnipe", enabled=True)

    @editsnipe.command(name="disable")
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def editsnipe_disable(self, ctx: GuildContext) -> None:
        """Disable edited-message sniping for this server."""
        await self._set_snipe_enabled(ctx, kind="editsnipe", enabled=False)
