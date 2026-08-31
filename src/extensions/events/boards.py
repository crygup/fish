from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal, cast
from weakref import WeakValueDictionary

import discord
from discord.ext import commands

from core import Cog, is_operational_guild
from core.cache import DEFAULT_BOARD_EMOJIS
from core.handoff import is_legacy_instance

if TYPE_CHECKING:
    from core import Fishie


BoardType = Literal["starboard", "clownboard"]


def board_emoji(settings: Mapping[str, Any]) -> str:
    """Return the configured emoji in a form Discord can render."""

    emoji_id = settings.get("emoji_id")
    emoji_name = str(settings.get("emoji_name") or "")
    if emoji_id is None:
        return emoji_name
    prefix = "a" if settings.get("emoji_animated") else ""
    return f"<{prefix}:{emoji_name}:{int(emoji_id)}>"


def reaction_matches(
    settings: Mapping[str, Any], emoji: discord.PartialEmoji | discord.Emoji | str
) -> bool:
    """Match a raw reaction to a board's configured Unicode/custom emoji."""

    emoji_id = settings.get("emoji_id")
    if emoji_id is not None:
        return not isinstance(emoji, str) and emoji.id == int(emoji_id)
    if isinstance(emoji, str):
        return emoji == settings.get("emoji_name")
    return emoji.id is None and emoji.name == settings.get("emoji_name")


def _channel_is_nsfw(channel: Any) -> bool:
    is_nsfw = getattr(channel, "is_nsfw", None)
    return bool(is_nsfw()) if callable(is_nsfw) else False


def _attachment_items(
    attachments: Sequence[discord.Attachment],
) -> tuple[list[discord.MediaGalleryItem], list[str]]:
    gallery: list[discord.MediaGalleryItem] = []
    links: list[str] = []
    for attachment in attachments:
        content_type = attachment.content_type or ""
        if len(gallery) < 10 and content_type.startswith(("image/", "video/")):
            gallery.append(
                discord.MediaGalleryItem(
                    attachment.url,
                    description=attachment.filename,
                )
            )
        else:
            safe_name = discord.utils.escape_mentions(
                discord.utils.escape_markdown(attachment.filename)
            )
            if len(links) < 2:
                links.append(f"[{safe_name}]({attachment.url})")
    return gallery, links


def _safe_board_content(value: object, *, limit: int = 2_400) -> str:
    """Render user message text without allowing Markdown or mentions through.

    Board messages are posted by the bot in a public channel.  Treat the
    original message as untrusted input: Markdown could spoof the board
    layout or create links, while ``@everyone``/role/user mentions could
    notify an unrelated audience.  Escape first, then apply the display limit
    so the escaped representation still fits the Components V2 text limit.
    """

    content = discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value or ""))
    )
    # ``escape_markdown`` intentionally leaves angle brackets untouched, but
    # Discord treats ``<https://...>`` as an automatic link (and ``<@...>`` as
    # a mention).  Full-width brackets preserve the visual text while making
    # both syntaxes inert.
    content = content.replace("<", "＜").replace(">", "＞")
    if len(content) <= limit:
        return content
    # Avoid leaving a trailing escape character when shortening an escaped
    # string.  The suffix is deliberately plain text and cannot be parsed as
    # Markdown or a mention.
    shortened = content[: max(0, limit - 3)].rstrip("\\")
    return f"{shortened}..."


async def build_board_view(
    bot: Fishie,
    message: discord.Message,
    *,
    emoji: str,
    count: int,
) -> discord.ui.LayoutView:
    """Build the shared Components V2 message used by both boards."""

    username = discord.utils.escape_mentions(
        discord.utils.escape_markdown(message.author.name)
    )
    text_parts = [f"### {emoji} {count:,} · {username} ({message.author.id})"]
    if message.content:
        # Leave room for the heading and attachment links within TextDisplay's
        # 4,000-character limit. AllowedMentions.none prevents quoted content
        # from pinging anyone.
        text_parts.append(_safe_board_content(message.content))

    gallery, attachment_links = _attachment_items(message.attachments)
    if attachment_links:
        text_parts.append("\n".join(attachment_links))

    children: list[discord.ui.Item[Any]] = [
        discord.ui.TextDisplay("\n".join(text_parts))
    ]
    if gallery:
        children.append(discord.ui.MediaGallery(*gallery))
    children.append(discord.ui.Separator())

    reference = message.reference
    replied_to: discord.Message | None = None
    if reference is not None:
        resolved = reference.resolved
        if isinstance(resolved, discord.Message):
            replied_to = resolved
        elif reference.message_id is not None:
            try:
                replied_to = await message.channel.fetch_message(reference.message_id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                pass
    if replied_to is not None:
        reply_author = discord.utils.escape_mentions(
            discord.utils.escape_markdown(replied_to.author.name)
        )
        children.append(
            discord.ui.TextDisplay(
                f"Replying to: [{reply_author}]({replied_to.jump_url})"
            )
        )

    channel_name = discord.utils.escape_mentions(
        discord.utils.escape_markdown(
            getattr(message.channel, "name", "unknown-channel")
        )
    )
    children.append(
        discord.ui.TextDisplay(
            f"-# #{channel_name} · [Message]({message.jump_url}) · "
            f"{discord.utils.format_dt(message.created_at, 'f')}"
        )
    )

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(
        discord.ui.Container(*children, accent_color=getattr(bot, "embedcolor", None))
    )
    return view


class BoardEvents(Cog):
    """Shared raw-reaction event processor for starboard and clownboard."""

    def _board_lock(
        self, guild_id: int, board_type: BoardType, message_id: int
    ) -> asyncio.Lock:
        if not hasattr(self, "_board_locks"):
            self._board_locks: WeakValueDictionary[
                tuple[int, BoardType, int], asyncio.Lock
            ] = WeakValueDictionary()
        key = (guild_id, board_type, message_id)
        return self._board_locks.setdefault(key, asyncio.Lock())

    @staticmethod
    def _cached_settings(config: Any) -> dict[str, Any]:
        return {
            "guild_id": config.guild_id,
            "board_type": config.board_type,
            "channel_id": config.channel_id,
            "enabled": config.enabled,
            "threshold": config.threshold,
            "allow_nsfw": config.allow_nsfw,
            "emoji_name": config.emoji_name,
            "emoji_id": config.emoji_id,
            "emoji_animated": config.emoji_animated,
            "emoji_set_by": config.emoji_set_by,
        }

    async def _repair_deleted_emoji(
        self,
        guild: discord.Guild,
        settings: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        current = dict(settings)
        emoji_id = current.get("emoji_id")
        if emoji_id is None or guild.get_emoji(int(emoji_id)) is not None:
            return current

        board_type = cast(BoardType, current["board_type"])
        fallback = DEFAULT_BOARD_EMOJIS[board_type]
        if not self.bot.db_cache.board_default_emoji_is_available(guild.id, board_type):
            await self.bot.pool.execute(
                "UPDATE guild_boards SET enabled = FALSE, updated_at = NOW() "
                "WHERE guild_id = $1 AND board_type = $2",
                guild.id,
                board_type,
            )
            self.bot.logger.warning(
                "Disabled %s in guild %s because its deleted custom emoji's "
                "fallback is used by the other board",
                board_type,
                guild.id,
            )
            try:
                self.bot.db_cache.update_board(guild.id, board_type, enabled=False)
            except KeyError:
                pass
            return None

        await self.bot.pool.execute(
            "UPDATE guild_boards SET emoji_name = $3, emoji_id = NULL, "
            "emoji_animated = FALSE, emoji_set_by = NULL, updated_at = NOW() "
            "WHERE guild_id = $1 AND board_type = $2",
            guild.id,
            board_type,
            fallback,
        )
        current.update(
            emoji_name=fallback,
            emoji_id=None,
            emoji_animated=False,
            emoji_set_by=None,
        )
        try:
            self.bot.db_cache.update_board(
                guild.id,
                board_type,
                emoji_name=fallback,
                emoji_id=None,
                emoji_animated=False,
                emoji_set_by=None,
            )
        except KeyError:
            pass
        return current

    async def _settings_for_reaction(
        self, guild: discord.Guild, emoji: discord.PartialEmoji
    ) -> AsyncIterator[dict[str, Any]]:
        for board_type in ("starboard", "clownboard"):
            config = self.bot.db_cache.get_board(guild.id, board_type)
            if config is None or not config.enabled:
                continue
            settings = self._cached_settings(config)
            repaired = await self._repair_deleted_emoji(guild, settings)
            if repaired is not None and reaction_matches(repaired, emoji):
                yield repaired

    async def _fetch_message(
        self, guild: discord.Guild, channel_id: int, message_id: int
    ) -> discord.Message | None:
        channel = guild.get_channel_or_thread(channel_id)
        if channel is None:
            try:
                channel = cast(Any, await self.bot.fetch_channel(channel_id))
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                return None
        fetch_message = getattr(channel, "fetch_message", None)
        if not callable(fetch_message):
            return None
        try:
            return await cast(Any, fetch_message)(message_id)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            return None

    async def _blocks(
        self, guild_id: int, board_type: BoardType
    ) -> tuple[set[int], set[int]]:
        users = set(
            self.bot.db_cache.board_blocks.get((guild_id, board_type, "user"), set())
        )
        channels = set(
            self.bot.db_cache.board_blocks.get((guild_id, board_type, "channel"), set())
        )
        return users, channels

    async def _reaction_count(
        self,
        message: discord.Message,
        settings: Mapping[str, Any],
        blocked_users: set[int],
    ) -> int:
        reaction = next(
            (
                candidate
                for candidate in message.reactions
                if reaction_matches(settings, candidate.emoji)
            ),
            None,
        )
        if reaction is None:
            return 0

        count = 0
        try:
            async for user in reaction.users(limit=None):
                # Bot-authored messages are valid board sources, but bot
                # reactions and an author's reaction to their own message do
                # not count toward the threshold.
                if (
                    not user.bot
                    and user.id != message.author.id
                    and user.id not in blocked_users
                ):
                    count += 1
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            # Do not publish an inaccurate count that could include blocked
            # reactors when Discord refuses to return the user list.
            return 0
        return count

    async def _entry(
        self, guild_id: int, board_type: BoardType, source_message_id: int
    ) -> Any:
        return await self.bot.pool.fetchrow(
            "SELECT guild_id, board_type, source_channel_id, source_message_id, "
            "board_channel_id, board_message_id, reaction_count, source_author_id "
            "FROM guild_board_entries WHERE guild_id = $1 AND board_type = $2 "
            "AND source_message_id = $3",
            guild_id,
            board_type,
            source_message_id,
        )

    async def _delete_entry_message(self, entry: Mapping[str, Any]) -> None:
        guild = self.bot.get_guild(int(entry["guild_id"]))
        if guild is not None:
            message = await self._fetch_message(
                guild,
                int(entry["board_channel_id"]),
                int(entry["board_message_id"]),
            )
            if message is not None:
                try:
                    await message.delete()
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    pass
        await self.bot.pool.execute(
            "DELETE FROM guild_board_entries WHERE guild_id = $1 "
            "AND board_type = $2 AND source_message_id = $3",
            entry["guild_id"],
            entry["board_type"],
            entry["source_message_id"],
        )

    async def _publish_or_update(
        self,
        guild: discord.Guild,
        settings: Mapping[str, Any],
        source: discord.Message,
        count: int,
        entry: Mapping[str, Any] | None,
    ) -> None:
        board_channel_id = int(settings["channel_id"])
        board_channel = guild.get_channel_or_thread(board_channel_id)
        if board_channel is None:
            try:
                board_channel = cast(
                    Any, await self.bot.fetch_channel(board_channel_id)
                )
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                return
        send = getattr(board_channel, "send", None)
        if not callable(send):
            return

        view = await build_board_view(
            self.bot,
            source,
            emoji=board_emoji(settings),
            count=count,
        )
        board_message: discord.Message | None = None
        if entry is not None:
            board_message = await self._fetch_message(
                guild,
                int(entry["board_channel_id"]),
                int(entry["board_message_id"]),
            )
            if (
                board_message is not None
                and int(entry["board_channel_id"]) != board_channel_id
            ):
                try:
                    await board_message.delete()
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    pass
                board_message = None
        if board_message is not None:
            try:
                await board_message.edit(
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                await self.bot.pool.execute(
                    "UPDATE guild_board_entries SET reaction_count = $4 "
                    "WHERE guild_id = $1 AND board_type = $2 "
                    "AND source_message_id = $3",
                    guild.id,
                    settings["board_type"],
                    source.id,
                    count,
                )
                return
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                pass

        try:
            board_message = cast(
                discord.Message,
                await cast(Any, send)(
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                ),
            )
        except (discord.Forbidden, discord.HTTPException):
            return
        await self.bot.pool.execute(
            "INSERT INTO guild_board_entries (guild_id, board_type, "
            "source_channel_id, source_message_id, board_channel_id, "
            "board_message_id, reaction_count, source_author_id) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) "
            "ON CONFLICT (guild_id, board_type, source_message_id) DO UPDATE "
            "SET board_channel_id = EXCLUDED.board_channel_id, "
            "board_message_id = EXCLUDED.board_message_id, "
            "reaction_count = EXCLUDED.reaction_count",
            guild.id,
            settings["board_type"],
            source.channel.id,
            source.id,
            board_channel_id,
            board_message.id,
            count,
            source.author.id,
        )

    async def _sync_board(
        self,
        guild: discord.Guild,
        payload: discord.RawReactionActionEvent,
        settings: Mapping[str, Any],
    ) -> None:
        board_type = cast(BoardType, settings["board_type"])
        async with self._board_lock(guild.id, board_type, payload.message_id):
            entry = await self._entry(guild.id, board_type, payload.message_id)
            source = await self._fetch_message(
                guild, payload.channel_id, payload.message_id
            )
            if source is None:
                if entry is not None:
                    await self._delete_entry_message(entry)
                return

            blocked_users, blocked_channels = await self._blocks(guild.id, board_type)
            disallowed = (
                source.author.id in blocked_users
                or source.channel.id in blocked_channels
                or source.channel.id == int(settings["channel_id"])
                or (not settings["allow_nsfw"] and _channel_is_nsfw(source.channel))
            )
            count = (
                0
                if disallowed
                else await self._reaction_count(source, settings, blocked_users)
            )
            if count < int(settings["threshold"]):
                if entry is not None:
                    await self._delete_entry_message(entry)
                return
            await self._publish_or_update(guild, settings, source, count, entry)

    async def _on_board_reaction(self, payload: discord.RawReactionActionEvent) -> None:
        if is_legacy_instance(self.bot):
            return
        if is_operational_guild(payload):
            return
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        try:
            async for settings in self._settings_for_reaction(guild, payload.emoji):
                await self._sync_board(guild, payload, settings)
        except Exception:
            self.bot.logger.exception(
                "Failed to synchronize message boards for guild %s message %s",
                payload.guild_id,
                payload.message_id,
            )

    @commands.Cog.listener("on_raw_reaction_add")
    async def on_board_reaction_add(
        self, payload: discord.RawReactionActionEvent
    ) -> None:
        await self._on_board_reaction(payload)

    @commands.Cog.listener("on_raw_reaction_remove")
    async def on_board_reaction_remove(
        self, payload: discord.RawReactionActionEvent
    ) -> None:
        await self._on_board_reaction(payload)

    @commands.Cog.listener("on_guild_emojis_update")
    async def on_board_emoji_delete(
        self,
        guild: discord.Guild,
        _before: Sequence[discord.Emoji],
        after: Sequence[discord.Emoji],
    ) -> None:
        """Repair or disable a board as soon as its custom emoji is deleted."""

        if is_legacy_instance(self.bot):
            return
        if is_operational_guild(guild):
            return
        remaining_ids = {emoji.id for emoji in after}
        for board_type in ("starboard", "clownboard"):
            config = self.bot.db_cache.get_board(guild.id, board_type)
            if (
                config is None
                or not config.enabled
                or config.emoji_id is None
                or config.emoji_id in remaining_ids
            ):
                continue
            try:
                await self._repair_deleted_emoji(guild, self._cached_settings(config))
            except Exception:
                self.bot.logger.exception(
                    "Failed to repair the deleted %s emoji in guild %s",
                    board_type,
                    guild.id,
                )
        try:
            badges = getattr(self.bot, "badges", None)
            revoke = getattr(badges, "revoke_unavailable_custom_badges", None)
            if revoke is not None:
                await revoke({emoji.id for emoji in self.bot.emojis})
        except Exception:
            self.bot.logger.exception(
                "Failed to reconcile custom badge emojis after update in guild %s",
                guild.id,
            )

    @commands.Cog.listener("on_raw_message_delete")
    async def on_board_message_delete(
        self, payload: discord.RawMessageDeleteEvent
    ) -> None:
        if is_legacy_instance(self.bot):
            return
        if is_operational_guild(payload):
            return
        if payload.guild_id is None:
            return
        await self._handle_deleted_message(payload.guild_id, payload.message_id)

    async def _handle_deleted_message(self, guild_id: int, message_id: int) -> None:
        entries = await self.bot.pool.fetch(
            "SELECT guild_id, board_type, source_channel_id, source_message_id, "
            "board_channel_id, board_message_id FROM guild_board_entries "
            "WHERE guild_id = $1 AND (source_message_id = $2 OR board_message_id = $2)",
            guild_id,
            message_id,
        )
        for entry in entries:
            if int(entry["source_message_id"]) == message_id:
                await self._delete_entry_message(dict(entry))
            else:
                await self.bot.pool.execute(
                    "DELETE FROM guild_board_entries WHERE guild_id = $1 "
                    "AND board_type = $2 AND source_message_id = $3",
                    entry["guild_id"],
                    entry["board_type"],
                    entry["source_message_id"],
                )

    @commands.Cog.listener("on_raw_bulk_message_delete")
    async def on_board_bulk_message_delete(
        self, payload: discord.RawBulkMessageDeleteEvent
    ) -> None:
        if is_legacy_instance(self.bot):
            return
        if is_operational_guild(payload):
            return
        if payload.guild_id is None:
            return
        for message_id in payload.message_ids:
            await self._handle_deleted_message(payload.guild_id, message_id)


async def setup(bot: Fishie) -> None:
    await bot.add_cog(BoardEvents())
