from __future__ import annotations

import asyncio
import datetime
import difflib
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Literal, Optional, Union, cast
from uuid import uuid4

import discord
from discord.abc import Messageable
from discord.ext import commands

from core import Cog
from core.currency import (
    EVERYTHING_AMOUNT,
    BalanceOverflow,
    CoinAmountError,
    InsufficientFunds,
    InvalidAmount,
    parse_coin_amount,
)
from extensions.fun.burger import add_burger_asset, remove_burger_asset
from extensions.fun.minigames import add_word_bomb_words, normalize_word_bomb_words
from utils import (
    AllMsgbleChannels,
    TwemojiConverter,
    fish_owner,
    fish_x,
    greenTick,
    remove_user_badge,
    remove_user_badge_entry,
    render_user_badge,
    set_user_badge,
)

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


class GuildSnapshot:
    def __init__(
        self,
        guild: discord.Guild,
        *,
        command_count: int = 0,
        last_command: datetime.datetime | None = None,
        last_download: datetime.datetime | None = None,
        last_download_auto: bool | None = None,
        auto_download_channel: int | None = None,
        poketwo: bool = False,
        joined_at: datetime.datetime | None = None,
    ) -> None:
        self.guild = guild
        self.command_count = command_count
        self.last_command = last_command
        self.last_download = last_download
        self.last_download_auto = last_download_auto
        self.auto_download_channel = auto_download_channel
        self.poketwo = poketwo
        self.joined_at = joined_at


class GuildRemovalNoticeView(discord.ui.LayoutView):
    """Components V2 notice sent to a guild before Fishie leaves."""

    def __init__(
        self,
        guild: discord.Guild,
        *,
        reason: str,
        invite_url: str,
        support_url: str,
        colour: discord.Colour | int,
    ) -> None:
        super().__init__(timeout=None)
        safe_reason = discord.utils.escape_mentions(
            discord.utils.escape_markdown(reason)
        )
        text = [
            "## Fishie was removed by the developers",
            f"Fishie is leaving **{discord.utils.escape_markdown(guild.name)}**.",
        ]
        if safe_reason:
            text.append(f"**Reason:** {safe_reason[:1_000]}")
        text.append("You can reinvite Fishie or join the Fishie Discord server below.")
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("\n\n".join(text)),
                accent_color=colour,
            )
        )
        self.add_item(
            discord.ui.ActionRow(
                discord.ui.Button(
                    label="Invite Fishie",
                    style=discord.ButtonStyle.link,
                    url=invite_url,
                ),
                discord.ui.Button(
                    label="Fishie Discord",
                    style=discord.ButtonStyle.link,
                    url=support_url,
                ),
            )
        )


class GuildLeaveReasonModal(discord.ui.Modal, title="Leave reason"):
    reason = discord.ui.TextInput(
        label="Reason (optional)",
        placeholder="Leave blank if you do not want to provide one.",
        required=False,
        max_length=1_000,
        style=discord.TextStyle.paragraph,
    )

    def __init__(self, view: "GuildDirectoryPaginator") -> None:
        super().__init__()
        self.view_ref = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self.view_ref.leave_with_notice(
            interaction, str(self.reason.value).strip()
        )


class GuildDirectoryPaginator(discord.ui.LayoutView):
    """Owner-only Components V2 paginator for the bot's guild directory."""

    def __init__(
        self,
        ctx: Context,
        cog: "Owner",
        snapshots: list[GuildSnapshot],
    ) -> None:
        super().__init__(timeout=900)
        self.ctx = ctx
        self.cog = cog
        self.snapshots = snapshots
        self.index = 0
        self.message: discord.Message | None = None
        self.status: str | None = None
        self.status_index: int | None = None
        self.details = discord.ui.TextDisplay("")
        self.container = discord.ui.Container(
            self.details, accent_color=self.ctx.bot.embedcolor
        )
        self.previous = discord.ui.Button(label="<", style=discord.ButtonStyle.primary)
        self.next = discord.ui.Button(label=">", style=discord.ButtonStyle.primary)
        self.leave_quiet = discord.ui.Button(
            label="Leave quietly", style=discord.ButtonStyle.danger
        )
        self.leave_notify = discord.ui.Button(
            label="Leave & notify", style=discord.ButtonStyle.danger
        )
        self.previous.callback = self._previous
        self.next.callback = self._next
        self.leave_quiet.callback = self._leave_quiet
        self.leave_notify.callback = self._leave_notify
        self.add_item(self.container)
        self.add_item(
            discord.ui.ActionRow(
                self.previous,
                self.next,
                self.leave_quiet,
                self.leave_notify,
            )
        )
        self._render()

    @staticmethod
    def _when(value: datetime.datetime | None) -> str:
        return discord.utils.format_dt(value, "R") if value else "Never"

    def _render(self, *, status: str | None = None) -> None:
        if status is not None:
            self.status = status
            self.status_index = self.index
        snapshot = self.snapshots[self.index]
        guild = snapshot.guild
        member_count = guild.member_count or len(guild.members)
        bot_count = sum(member.bot for member in guild.members)
        owner = guild.owner
        owner_text = (
            f"{discord.utils.escape_markdown(owner.name)} (`{owner.id}`)"
            if owner is not None
            else f"Unknown (`{guild.owner_id}`)"
        )
        auto_channel = snapshot.auto_download_channel
        channel = guild.get_channel(auto_channel) if auto_channel else None
        auto_text = (
            channel.mention
            if isinstance(channel, discord.TextChannel)
            else (f"`{auto_channel}`" if auto_channel else "Not configured")
        )
        lines = [
            f"## Guild {self.index + 1}/{len(self.snapshots)} · {discord.utils.escape_markdown(guild.name)}",
            f"**Members:** {member_count:,} ({bot_count:,} bots)",
            f"**Owner:** {owner_text}",
            f"**Created:** {discord.utils.format_dt(guild.created_at, 'F')}",
            f"**Fishie added:** {self._when(snapshot.joined_at)}",
            f"**Commands:** {snapshot.command_count:,} · Last used: {self._when(snapshot.last_command)}",
            f"**Downloads:** {self._when(snapshot.last_download)}"
            + (" (auto download)" if snapshot.last_download_auto else ""),
            f"**Pokétwo solver:** {'Enabled' if snapshot.poketwo else 'Disabled'}",
            f"**Auto-download channel:** {auto_text}",
            f"**Guild ID:** `{guild.id}`",
        ]
        if self.status and self.status_index == self.index:
            lines.append(f"\n-# {self.status}")
        self.details.content = "\n".join(lines)
        removed = (
            self.status is not None
            and self.status_index == self.index
            and self.status.startswith("Removed")
        )
        self.leave_quiet.disabled = removed
        self.leave_notify.disabled = removed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            "Only the owner who opened this guild list can use these controls.",
            ephemeral=True,
        )
        return False

    async def on_timeout(self) -> None:
        for item in (self.previous, self.next, self.leave_quiet, self.leave_notify):
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                pass

    async def _edit(self, interaction: discord.Interaction) -> None:
        self._render()
        await interaction.response.edit_message(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _previous(self, interaction: discord.Interaction) -> None:
        if self.snapshots:
            self.index = (self.index - 1) % len(self.snapshots)
        await self._edit(interaction)

    async def _next(self, interaction: discord.Interaction) -> None:
        if self.snapshots:
            self.index = (self.index + 1) % len(self.snapshots)
        await self._edit(interaction)

    async def _leave_quiet(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        guild = self.snapshots[self.index].guild
        try:
            await guild.leave()
        except discord.HTTPException:
            self._render(status="Could not remove this guild. Try again later.")
        else:
            self._render(status=f"Removed {guild.name} quietly.")
        if self.message:
            await self.message.edit(
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _leave_notify(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(GuildLeaveReasonModal(self))

    async def leave_with_notice(
        self, interaction: discord.Interaction, reason: str
    ) -> None:
        guild = self.snapshots[self.index].guild
        sent = await self.cog._notify_before_leave(guild, reason)
        if sent:
            try:
                await guild.leave()
            except discord.HTTPException:
                self._render(status="The notice was sent, but Fishie could not leave.")
            else:
                self._render(status=f"Removed {guild.name} after sending a notice.")
        else:
            self._render(
                status="Could not find a channel where the notice could be sent."
            )
        if self.message:
            await self.message.edit(
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )


class Owner(Cog):
    emoji = fish_owner
    hidden: bool = True

    # Userinfo renders badges directly into a Components V2 text block. Keep
    # the owner-editable label deliberately small and single-line so a custom
    # badge cannot take over the whole profile view or alter its layout.
    _BADGE_NAME_LIMIT = 100
    _BADGE_EMOJI_LIMIT = 100
    _CUSTOM_EMOJI_RE = re.compile(
        r"^<(?P<animated>a?):(?P<name>[A-Za-z0-9_~]{1,32}):(?P<id>[0-9]{1,20})>$"
    )

    def __init__(self, bot: Fishie):
        super().__init__()
        self.bot = bot
        self._last_reload: float = time.time()

    @classmethod
    def _badge_emoji(cls, value: str) -> tuple[str, int | None, bool, str, bool]:
        """Validate and normalize a badge emoji.

        The database and JSON catalog retain the parsed emoji fields so
        animated custom emojis can be rendered correctly after a restart.
        """

        value = str(value).strip()
        if not value:
            raise commands.BadArgument("Provide one Unicode or Discord emoji.")
        if len(value) > cls._BADGE_EMOJI_LIMIT:
            raise commands.BadArgument(
                f"Badge emojis cannot be longer than {cls._BADGE_EMOJI_LIMIT} characters."
            )

        match = cls._CUSTOM_EMOJI_RE.fullmatch(value)
        if match:
            name = match.group("name")
            emoji_id = int(match.group("id"))
            if not 0 < emoji_id <= 9_223_372_036_854_775_807:
                raise commands.BadArgument("That Discord emoji ID is out of range.")
            animated = bool(match.group("animated"))
            prefix = "a" if animated else ""
            rendered = f"<{prefix}:{name}:{emoji_id}>"
            return name, emoji_id, True, rendered, animated

        # PartialEmoji.from_str accepts arbitrary text as a name, so use the
        # project converter's strict one-Unicode-emoji check as well.
        if not TwemojiConverter.is_unicode_emoji(value):
            raise commands.BadArgument(
                "That is not a valid single Unicode or Discord custom emoji."
            )
        return value, None, False, value, False

    @classmethod
    def _badge_name(cls, value: str) -> str:
        value = str(value).strip()
        if not value:
            raise commands.BadArgument("Badge names cannot be empty.")
        if "\n" in value or "\r" in value:
            raise commands.BadArgument("Badge names must fit on one line.")
        if len(value) > cls._BADGE_NAME_LIMIT:
            raise commands.BadArgument(
                f"Badge names cannot be longer than {cls._BADGE_NAME_LIMIT} characters."
            )
        if "\x00" in value:
            raise commands.BadArgument("Badge names cannot contain null characters.")
        return value

    @staticmethod
    def _badge_row_value(
        row: object,
    ) -> tuple[int, str, int | None, bool, bool, str, object]:
        """Read a badge row from either asyncpg.Record or a test mapping."""

        def get(name: str, default: object = None) -> object:
            if isinstance(row, dict):
                return row.get(name, default)
            try:
                return row[name]  # type: ignore[index]
            except (KeyError, IndexError, TypeError):
                return getattr(row, name, default)

        user_id = int(cast(Any, get("user_id", 0)))
        emoji_name = str(get("emoji_name", ""))
        emoji_id_value = get("emoji_id")
        emoji_id = (
            int(cast(Any, emoji_id_value)) if emoji_id_value is not None else None
        )
        is_custom = bool(get("is_custom", emoji_id is not None))
        animated = bool(get("animated", False)) if is_custom else False
        text = str(get("text", ""))
        rendered = render_user_badge(
            {
                "emoji_name": emoji_name,
                "emoji_id": emoji_id,
                "is_custom": is_custom,
                "animated": animated,
                "text": "",
            }
        )
        return user_id, rendered, emoji_id, is_custom, animated, text, get("created_at")

    def _cache_badge(self, row: object) -> None:
        """Update the runtime cache and editable JSON badge catalog."""

        user_id, rendered, emoji_id, is_custom, animated, text, created_at = (
            self._badge_row_value(row)
        )
        emoji_name = ""
        if is_custom and emoji_id is not None:
            match = self._CUSTOM_EMOJI_RE.fullmatch(rendered)
            if match:
                emoji_name = match.group("name")
        else:
            emoji_name = rendered
        if isinstance(row, dict):
            badge_key = str(row.get("badge_key") or "custom")
        else:
            try:
                badge_key = str(row["badge_key"])  # type: ignore[index]
            except (KeyError, IndexError, TypeError):
                badge_key = "custom"
        set_user_badge(
            user_id,
            emoji_name=emoji_name,
            emoji_id=emoji_id,
            is_custom=is_custom,
            animated=animated,
            text=text,
            badge_key=badge_key,
        )
        cache = getattr(self.bot, "db_cache", None)
        user_badges = getattr(cache, "user_badges", None)
        if isinstance(user_badges, dict):
            entry = {
                "emoji_name": emoji_name,
                "emoji_id": emoji_id,
                "is_custom": is_custom,
                "animated": animated,
                "text": text,
                "created_at": created_at,
                "badge_key": badge_key,
            }
            current = user_badges.get(user_id)
            if isinstance(current, list):
                current[:] = [
                    item for item in current if item.get("badge_key") != badge_key
                ]
                current.append(entry)
            else:
                user_badges[user_id] = [entry]

    def _uncache_badge(self, user_id: int) -> None:
        remove_user_badge(int(user_id))
        cache = getattr(self.bot, "db_cache", None)
        user_badges = getattr(cache, "user_badges", None)
        if isinstance(user_badges, dict):
            user_badges.pop(int(user_id), None)

    @staticmethod
    def _badge_user_text(user: discord.User) -> str:
        return f"{user} (`{user.id}`)"

    async def _badge_for_user(self, user_id: int) -> object | None:
        return await self.bot.pool.fetchrow(
            """
            SELECT user_id, emoji_name, emoji_id, is_custom, animated, badge_key,
                   text, created_at
            FROM user_badges
            WHERE user_id = $1 AND badge_key LIKE 'custom:%'
            ORDER BY id DESC
            LIMIT 1
            """,
            int(user_id),
        )

    async def _badges_for_user(self, user_id: int) -> list[object]:
        return list(
            await self.bot.pool.fetch(
                """
                SELECT id, user_id, emoji_name, emoji_id, is_custom, animated,
                       badge_key, text, created_at
                FROM user_badges
                WHERE user_id = $1 AND badge_key LIKE 'custom:%'
                ORDER BY id
                """,
                int(user_id),
            )
        )

    async def _select_badge(self, user_id: int, selector: str) -> object:
        rows = await self._badges_for_user(user_id)
        if not rows:
            raise commands.BadArgument(
                f"{self._badge_user_text(await self.bot.fetch_user(user_id))} "
                "does not have any custom badges."
            )
        value = str(selector).strip()
        matches: list[object] = []
        if value.isdigit():
            number = int(value)
            matches = [
                row for row in rows if int(row["id"]) == number  # type: ignore[index]
            ]
            if not matches and 1 <= number <= len(rows):
                matches = [rows[number - 1]]
        else:
            folded = value.casefold()
            matches = [
                row
                for row in rows
                if str(row["text"]).casefold() == folded  # type: ignore[index]
                or str(row["emoji_name"]).casefold() == folded  # type: ignore[index]
                or str(row["badge_key"]).casefold() == folded  # type: ignore[index]
            ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise commands.BadArgument(
                "That selector matches multiple badges; use the badge ID."
            )
        descriptions = [
            f"{index}. {row['text']} (ID: {row['id']})"  # type: ignore[index]
            for index, row in enumerate(rows, 1)
        ]
        raise commands.BadArgument(
            "Could not find that badge. Available badges: " + "; ".join(descriptions)
        )

    @commands.group(name="badge", invoke_without_command=True)
    async def badge(self, ctx: Context) -> None:
        """Give, edit, or clear a user's custom userinfo badge."""
        await ctx.send_help(ctx.command)

    @badge.command(name="give")
    async def badge_give(
        self,
        ctx: Context,
        user: discord.User,
        emoji: str,
        *,
        name: str,
    ) -> None:
        """Give a user a custom badge with one emoji and a display name."""

        emoji_name, emoji_id, is_custom, rendered, animated = self._badge_emoji(emoji)
        badge_name = self._badge_name(name)
        badge_key = f"custom:{uuid4().hex}"
        row = await self.bot.pool.fetchrow(
            """
            INSERT INTO user_badges (
                user_id, emoji_name, emoji_id, is_custom, unicode, animated,
                badge_key, text
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING id, user_id, emoji_name, emoji_id, is_custom, animated,
                      badge_key, text, created_at
            """,
            user.id,
            emoji_name,
            emoji_id,
            is_custom,
            not is_custom,
            animated,
            badge_key,
            badge_name,
        )
        if row is None:
            raise commands.BadArgument("Could not save that badge.")
        self._cache_badge(row)
        display_name = discord.utils.escape_markdown(badge_name)
        await ctx.send(
            f"Gave {self._badge_user_text(user)} the {rendered} {display_name} "
            f"badge (ID: `{row['id']}`).",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @badge.group(name="edit", invoke_without_command=True)
    async def badge_edit(self, ctx: Context) -> None:
        """Edit the emoji or name of an existing custom badge."""
        await ctx.send_help(ctx.command)

    @badge_edit.command(name="emoji")
    async def badge_edit_emoji(
        self,
        ctx: Context,
        user: discord.User,
        emoji: str,
    ) -> None:
        """Change the emoji on a user's custom badge."""

        emoji_name, emoji_id, is_custom, rendered, animated = self._badge_emoji(emoji)
        selected = await self._select_badge(user.id, "1")
        selected_key = str(selected["badge_key"])  # type: ignore[index]
        row = await self.bot.pool.fetchrow(
            """
            UPDATE user_badges
            SET emoji_name = $3, emoji_id = $4, is_custom = $5,
                unicode = $6, animated = $7
            WHERE user_id = $1 AND badge_key = $2
            RETURNING user_id, emoji_name, emoji_id, is_custom, animated,
                      badge_key, text, created_at
            """,
            user.id,
            selected_key,
            emoji_name,
            emoji_id,
            is_custom,
            not is_custom,
            animated,
        )
        if row is None:
            raise commands.BadArgument(
                f"{self._badge_user_text(user)} does not have a custom badge yet."
            )
        self._cache_badge(row)
        _, _, _, _, _, badge_name, _ = self._badge_row_value(row)
        display_name = discord.utils.escape_markdown(badge_name)
        await ctx.send(
            f"Updated {self._badge_user_text(user)}'s badge emoji to "
            f"{rendered} {display_name}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @badge_edit.command(name="name")
    async def badge_edit_name(
        self,
        ctx: Context,
        user: discord.User,
        *,
        name: str,
    ) -> None:
        """Change the display name on a user's custom badge."""

        badge_name = self._badge_name(name)
        selected = await self._select_badge(user.id, "1")
        selected_key = str(selected["badge_key"])  # type: ignore[index]
        row = await self.bot.pool.fetchrow(
            """
            UPDATE user_badges
            SET text = $3
            WHERE user_id = $1 AND badge_key = $2
            RETURNING user_id, emoji_name, emoji_id, is_custom, animated,
                      badge_key, text, created_at
            """,
            user.id,
            selected_key,
            badge_name,
        )
        if row is None:
            raise commands.BadArgument(
                f"{self._badge_user_text(user)} does not have a custom badge yet."
            )
        self._cache_badge(row)
        _, rendered, _, _, _, _, _ = self._badge_row_value(row)
        display_name = discord.utils.escape_markdown(badge_name)
        await ctx.send(
            f"Updated {self._badge_user_text(user)}'s badge name to "
            f"{rendered} {display_name}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @badge.command(name="clear")
    async def badge_clear(self, ctx: Context, user: discord.User) -> None:
        """Clear a user's custom userinfo badge."""

        row = await self.bot.pool.fetchrow(
            """
            DELETE FROM user_badges
            WHERE user_id = $1 AND badge_key LIKE 'custom:%'
            RETURNING user_id
            """,
            user.id,
        )
        if row is None:
            raise commands.BadArgument(
                f"{self._badge_user_text(user)} does not have a custom badge."
            )
        self._uncache_badge(user.id)
        await ctx.send(
            f"Cleared the custom badge for {self._badge_user_text(user)}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @badge.command(name="remove")
    async def badge_remove(
        self,
        ctx: Context,
        user: discord.User,
        *,
        badge: str,
    ) -> None:
        """Remove one custom badge by its ID, number, name, or emoji."""

        selected = await self._select_badge(user.id, badge)
        badge_id = int(selected["id"])  # type: ignore[index]
        row = await self.bot.pool.fetchrow(
            """
            DELETE FROM user_badges
            WHERE id = $1 AND user_id = $2 AND badge_key LIKE 'custom:%'
            RETURNING id, badge_key, text
            """,
            badge_id,
            user.id,
        )
        if row is None:
            raise commands.BadArgument("That badge no longer exists.")
        remove_user_badge_entry(user.id, str(row["badge_key"]))
        cache = getattr(self.bot, "db_cache", None)
        user_badges = getattr(cache, "user_badges", None)
        if isinstance(user_badges, dict):
            entries = user_badges.get(user.id)
            if isinstance(entries, list):
                entries[:] = [
                    entry
                    for entry in entries
                    if entry.get("badge_key") != str(row["badge_key"])
                ]
                if not entries:
                    user_badges.pop(user.id, None)
        await ctx.send(
            f"Removed badge `{badge_id}` from {self._badge_user_text(user)}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.group(name="dev", invoke_without_command=True)
    async def dev(self, ctx: Context) -> None:
        """Owner-only development and maintenance commands."""
        await ctx.send_help(ctx.command)

    @dev.group(name="burger", invoke_without_command=True)
    async def dev_burger(self, ctx: Context) -> None:
        """Manage the anime burger image catalogue."""
        await ctx.send_help(ctx.command)

    @dev_burger.command(name="add")
    async def dev_burger_add(
        self,
        ctx: Context,
        image_url: str,
        source_url: str | None = None,
    ) -> None:
        """Download an image and add it to the burger catalogue."""

        try:
            async with ctx.typing():
                asset = await add_burger_asset(self.bot, image_url, source_url)
        except commands.BadArgument:
            raise
        except Exception as error:
            self.bot.logger.exception("Failed to add burger image")
            detail = str(error).strip()
            if detail:
                detail = detail[:300]
                raise commands.BadArgument(
                    f"I couldn't add that burger image right now: {detail}"
                ) from error
            raise commands.BadArgument(
                "I couldn't add that burger image right now."
            ) from error
        await ctx.send(
            f"Added burger image **#{asset.id}**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @dev_burger.command(name="remove")
    async def dev_burger_remove(self, ctx: Context, asset_id: int) -> None:
        """Remove one image from the burger catalogue by ID."""

        if asset_id <= 0:
            raise commands.BadArgument("Burger image IDs must be positive.")
        if not await remove_burger_asset(asset_id):
            raise commands.BadArgument(f"No burger image with ID `{asset_id}` exists.")
        await ctx.send(
            f"Removed burger image **#{asset_id}**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @dev.group(
        name="wordbomb",
        aliases=("wb", "word-bomb"),
        invoke_without_command=True,
    )
    async def dev_wordbomb(self, ctx: Context) -> None:
        """Manage custom valid words for Word Bomb."""
        await ctx.send_help(ctx.command)

    @dev_wordbomb.command(name="add", aliases=("allow", "word"))
    async def dev_wordbomb_add(self, ctx: Context, *, words: str) -> None:
        """Add one or more alphabetic words to the Word Bomb dictionary."""

        values = re.split(r"[\s,;]+", words)
        normalized = normalize_word_bomb_words(values)
        if not normalized:
            raise commands.BadArgument(
                "Provide one or more alphabetic words at least two letters long."
            )
        if len(normalized) > 100:
            raise commands.BadArgument("You can add at most 100 words at a time.")

        added: list[str] = []
        for word in normalized:
            row = await self.bot.pool.fetchrow(
                """
                INSERT INTO wordbomb_custom_words (word, added_by)
                VALUES ($1, $2)
                ON CONFLICT (word) DO NOTHING
                RETURNING word
                """,
                word,
                ctx.author.id,
            )
            if row is not None:
                added.append(str(row["word"]))
        add_word_bomb_words(added)

        if added:
            result = ", ".join(f"`{word}`" for word in added)
            await ctx.send(
                f"Added {len(added)} custom Word Bomb word(s): {result}.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await ctx.send(
                "Those words are already in the Word Bomb dictionary.",
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @dev.group(name="currency", aliases=("coins",), invoke_without_command=True)
    async def dev_currency(self, ctx: Context) -> None:
        """Add or remove Coins from a user's wallet."""
        await ctx.send_help(ctx.command)

    @dev_currency.command(name="add", aliases=("give",))
    async def dev_currency_add(
        self,
        ctx: Context,
        user: discord.User,
        amount: object,
        *extra_amount: str,
    ) -> None:
        """Add Coins to a user's wallet."""
        amount_expression = " ".join((str(amount), *extra_amount))
        try:
            parsed_amount = parse_coin_amount(amount_expression)
        except CoinAmountError as error:
            raise commands.BadArgument(str(error)) from error
        if parsed_amount == EVERYTHING_AMOUNT:
            raise commands.BadArgument(
                "Use a numeric amount for developer Coin grants."
            )
        amount = int(parsed_amount)
        try:
            wallet = await self.bot.currency.credit(
                user.id,
                amount,
                f"dev_add:{ctx.author.id}",
            )
        except InvalidAmount as error:
            raise commands.BadArgument(str(error)) from error
        except BalanceOverflow as error:
            raise commands.BadArgument(str(error)) from error
        await ctx.send(
            f"Added **{amount:,} Coins** to {user}.\n"
            f"-# Wallet balance: {wallet.balance:,} Coins",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @dev_currency.command(name="remove", aliases=("take", "subtract"))
    async def dev_currency_remove(
        self,
        ctx: Context,
        user: discord.User,
        amount: object,
        *extra_amount: str,
    ) -> None:
        """Remove Coins from a user's wallet."""
        amount_expression = " ".join((str(amount), *extra_amount))
        try:
            parsed_amount = parse_coin_amount(amount_expression)
        except CoinAmountError as error:
            raise commands.BadArgument(str(error)) from error
        if parsed_amount == EVERYTHING_AMOUNT:
            wallet = await self.bot.currency.get_wallet(user.id)
            amount = int(wallet.balance)
            if amount <= 0:
                raise commands.BadArgument(f"{user} has no Coins to remove.")
            if (
                await ctx.prompt(
                    f"Are you sure you want to remove everything from {user.mention}?",
                    confirm_label="Yes",
                    cancel_label="No",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                is None
            ):
                return
        else:
            amount = int(parsed_amount)
        try:
            wallet = await self.bot.currency.debit(
                user.id,
                amount,
                f"dev_remove:{ctx.author.id}",
            )
        except InvalidAmount as error:
            raise commands.BadArgument(str(error)) from error
        except InsufficientFunds as error:
            raise commands.BadArgument(
                f"{user} only has **{error.balance:,} Coins**."
            ) from error
        await ctx.send(
            f"Removed **{amount:,} Coins** from {user}.\n"
            f"-# Wallet balance: {wallet.balance:,} Coins",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @dev.group(name="video", invoke_without_command=True)
    async def dev_video(self, ctx: Context) -> None:
        """Manage the users allowed to submit videos for review."""
        await ctx.send_help(ctx.command)

    @dev_video.command(name="block")
    async def dev_video_block(self, ctx: Context, user: discord.User) -> None:
        """Block a user from submitting videos through `fish video upload`."""
        await self.bot.pool.execute(
            "INSERT INTO video_upload_blocks (user_id, blocked_by) VALUES ($1, $2) "
            "ON CONFLICT (user_id) DO UPDATE SET blocked_by = EXCLUDED.blocked_by, "
            "blocked_at = now()",
            user.id,
            ctx.author.id,
        )
        await ctx.send(
            f"{user} is now blocked from submitting videos.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @dev_video.command(name="unblock")
    async def dev_video_unblock(self, ctx: Context, user: discord.User) -> None:
        """Allow a blocked user to submit videos again."""
        result = await self.bot.pool.execute(
            "DELETE FROM video_upload_blocks WHERE user_id = $1", user.id
        )
        await ctx.send(
            (
                f"{user} can submit videos again."
                if result.endswith(" 1")
                else f"{user} was not blocked."
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @dev.group(name="post", invoke_without_command=True)
    async def dev_post(self, ctx: Context) -> None:
        """Manage the users allowed to submit images and GIFs for review."""
        await ctx.send_help(ctx.command)

    @dev_post.command(name="block")
    async def dev_post_block(self, ctx: Context, user: discord.User) -> None:
        """Block a user from submitting images or GIFs through `fish post upload`."""
        await self.bot.pool.execute(
            "INSERT INTO post_upload_blocks (user_id, blocked_by) VALUES ($1, $2) "
            "ON CONFLICT (user_id) DO UPDATE SET blocked_by = EXCLUDED.blocked_by, "
            "blocked_at = now()",
            user.id,
            ctx.author.id,
        )
        await ctx.send(
            f"{user} is now blocked from submitting posts.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @dev_post.command(name="unblock")
    async def dev_post_unblock(self, ctx: Context, user: discord.User) -> None:
        """Allow a blocked user to submit images and GIFs again."""
        result = await self.bot.pool.execute(
            "DELETE FROM post_upload_blocks WHERE user_id = $1", user.id
        )
        await ctx.send(
            (
                f"{user} can submit posts again."
                if result.endswith(" 1")
                else f"{user} was not blocked."
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @staticmethod
    def _normalise_dev_target(value: str) -> str:
        value = value.strip().strip("`").casefold()
        # Accept a copied invocation such as ``fish effect blur`` as a small
        # convenience while keeping the stored target canonical.
        value = re.sub(r"^(?:fish|/)[\s]+", "", value)
        return re.sub(r"[\s_.:/-]+", " ", value).strip()

    def _resolve_dev_target(self, value: str) -> tuple[str, str] | None:
        """Resolve a command alias or cog name to its canonical target."""

        normalised = self._normalise_dev_target(value)
        if not normalised:
            return None

        command = self.bot.get_command(value.strip().strip("`"))
        if command is None:
            command = next(
                (
                    candidate
                    for candidate in self.bot.commands
                    if self._normalise_dev_target(candidate.qualified_name)
                    == normalised
                ),
                None,
            )
        if command is not None:
            canonical = command.qualified_name.casefold()
            if self.bot._global_disable_excluded(command):
                return None
            return "command", canonical

        for cog_name, cog in self.bot.cogs.items():
            candidates = {
                self._normalise_dev_target(cog_name),
                self._normalise_dev_target(cog.__class__.__name__),
                self._normalise_dev_target(
                    getattr(cog.__class__, "__module__", "").rsplit(".", 1)[-1]
                ),
            }
            if normalised in candidates:
                if cog.__class__.__module__.startswith("extensions.owner"):
                    return None
                return "cog", cog_name.casefold()
        return None

    @dev.command(name="disable")
    async def dev_disable(self, ctx: Context, *, target: str) -> None:
        """Disable a command or cog globally until an owner enables it."""

        resolved = self._resolve_dev_target(target)
        if resolved is None:
            raise commands.BadArgument(
                "Provide the name of a loaded command or cog. Owner commands "
                "cannot be disabled."
            )
        target_type, canonical = resolved
        await self.bot.pool.execute(
            """
            INSERT INTO global_command_disables (target, target_type, disabled_by)
            VALUES ($1, $2, $3)
            ON CONFLICT (target) DO UPDATE
            SET target_type = EXCLUDED.target_type,
                disabled_by = EXCLUDED.disabled_by,
                disabled_at = now()
            """,
            canonical,
            target_type,
            ctx.author.id,
        )
        if target_type == "cog":
            self.bot.db_cache.globally_disabled_cogs.add(canonical)
            label = f"{canonical} cog"
        else:
            self.bot.db_cache.globally_disabled_commands.add(canonical)
            label = canonical
        await ctx.send(
            f"Globally disabled `{label}`.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @dev.command(name="enable")
    async def dev_enable(self, ctx: Context, *, target: str) -> None:
        """Re-enable a command or cog that was disabled globally."""

        resolved = self._resolve_dev_target(target)
        if resolved is None:
            raise commands.BadArgument("Provide the name of a loaded command or cog.")
        target_type, canonical = resolved
        result = await self.bot.pool.execute(
            "DELETE FROM global_command_disables WHERE target = $1", canonical
        )
        if target_type == "cog":
            self.bot.db_cache.globally_disabled_cogs.discard(canonical)
            label = f"{canonical} cog"
        else:
            self.bot.db_cache.globally_disabled_commands.discard(canonical)
            label = canonical
        await ctx.send(
            (
                f"Globally enabled `{label}`."
                if result.endswith(" 1")
                else f"`{label}` was not globally disabled."
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @dev.command(name="block")
    async def dev_block(self, ctx: Context, user: discord.User) -> None:
        """Block a user from invoking any Fishie command."""

        if await self.bot.is_owner(user):
            raise commands.BadArgument("Bot owners cannot be blocked.")
        await self.bot.pool.execute(
            """
            INSERT INTO global_user_blocks (user_id, blocked_by)
            VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE
            SET blocked_by = EXCLUDED.blocked_by, blocked_at = now()
            """,
            user.id,
            ctx.author.id,
        )
        self.bot.db_cache.globally_blocked_users.add(user.id)
        await ctx.send(
            f"{user} is now blocked from using Fishie commands.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @dev.command(name="unblock")
    async def dev_unblock(self, ctx: Context, user: discord.User) -> None:
        """Allow a blocked user to invoke Fishie commands again."""

        result = await self.bot.pool.execute(
            "DELETE FROM global_user_blocks WHERE user_id = $1", user.id
        )
        self.bot.db_cache.globally_blocked_users.discard(user.id)
        await ctx.send(
            (
                f"{user} can use Fishie commands again."
                if result.endswith(" 1")
                else f"{user} was not globally blocked."
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _guild_snapshots(self) -> list[GuildSnapshot]:
        guilds = list(self.bot.guilds)
        if not guilds:
            return []

        guild_ids = [guild.id for guild in guilds]
        command_rows = await self.bot.pool.fetch(
            """
            SELECT guild_id, COUNT(*) AS total, MAX(created_at) AS last_command
            FROM command_logs
            WHERE guild_id = ANY($1::bigint[])
            GROUP BY guild_id
            """,
            guild_ids,
        )
        download_rows = await self.bot.pool.fetch(
            """
            SELECT DISTINCT ON (guild_id)
                guild_id, downloaded_at, auto_download
            FROM download_events
            WHERE guild_id = ANY($1::bigint[])
            ORDER BY guild_id, downloaded_at DESC
            """,
            guild_ids,
        )
        join_rows = await self.bot.pool.fetch(
            """
            SELECT DISTINCT ON (guild_id) guild_id, time
            FROM guild_join_logs
            WHERE guild_id = ANY($1::bigint[])
            ORDER BY guild_id, time ASC
            """,
            guild_ids,
        )
        setting_rows = await self.bot.pool.fetch(
            """
            SELECT guild_id, auto_download, poketwo
            FROM guild_settings
            WHERE guild_id = ANY($1::bigint[])
            """,
            guild_ids,
        )

        command_by_guild = {int(row["guild_id"]): row for row in command_rows}
        download_by_guild = {int(row["guild_id"]): row for row in download_rows}
        joined_by_guild = {int(row["guild_id"]): row for row in join_rows}
        settings_by_guild = {int(row["guild_id"]): row for row in setting_rows}
        snapshots: list[GuildSnapshot] = []
        for guild in guilds:
            command = command_by_guild.get(guild.id)
            download = download_by_guild.get(guild.id)
            joined = joined_by_guild.get(guild.id)
            settings = settings_by_guild.get(guild.id)
            auto_channel = settings["auto_download"] if settings else None
            snapshots.append(
                GuildSnapshot(
                    guild,
                    command_count=int(command["total"]) if command else 0,
                    last_command=command["last_command"] if command else None,
                    last_download=download["downloaded_at"] if download else None,
                    last_download_auto=(
                        bool(download["auto_download"]) if download else None
                    ),
                    auto_download_channel=(int(auto_channel) if auto_channel else None),
                    poketwo=bool(settings["poketwo"]) if settings else False,
                    joined_at=joined["time"] if joined else None,
                )
            )
        snapshots.sort(
            key=lambda snapshot: (
                (
                    snapshot.last_command.timestamp()
                    if snapshot.last_command is not None
                    else float("-inf")
                ),
                snapshot.guild.name.casefold(),
            )
        )
        return snapshots

    @staticmethod
    def _notice_channel(guild: discord.Guild) -> discord.TextChannel | None:
        me = guild.me
        if me is None:
            return None

        candidates = []
        for channel in guild.text_channels:
            permissions = channel.permissions_for(me)
            if permissions.send_messages and permissions.embed_links:
                candidates.append(channel)

        preferred = (
            "general",
            "chat",
            "lobby",
            "main",
            "community",
            "talk",
            "welcome",
            "social",
            "discussion",
        )

        if candidates:

            def score(channel: discord.TextChannel) -> float:
                name = re.sub(r"[^a-z0-9]+", " ", channel.name.casefold()).strip()
                if name in preferred:
                    return 100.0 - preferred.index(name)
                return max(
                    difflib.SequenceMatcher(None, name, value).ratio()
                    for value in preferred
                )

            general_candidates = [
                channel for channel in candidates if score(channel) >= 0.55
            ]
            if general_candidates:
                return max(general_candidates, key=score)

        private = []
        for channel in guild.text_channels:
            permissions = channel.permissions_for(me)
            everyone = channel.permissions_for(guild.default_role)
            if (
                permissions.send_messages
                and permissions.embed_links
                and not everyone.view_channel
            ):
                private.append(channel)
        if private:
            return private[0]
        return candidates[0] if candidates else None

    async def _notify_before_leave(self, guild: discord.Guild, reason: str) -> bool:
        channel = self._notice_channel(guild)
        if channel is None:
            me = guild.me
            if me is None or not me.guild_permissions.manage_channels:
                return False
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                me: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    embed_links=True,
                    read_message_history=True,
                ),
            }
            try:
                channel = await guild.create_text_channel(
                    "fishie-notice",
                    overwrites=overwrites,
                    reason="Send Fishie removal notice",
                )
            except discord.HTTPException:
                return False

        application_id = getattr(self.bot, "active_application_id", None)
        if callable(application_id):
            application_id = application_id()
        if application_id is None:
            application_id = self.bot.config["ids"]["bot_id"]
        invite_url = discord.utils.oauth_url(
            int(cast(Any, application_id)), permissions=self.bot.bot_permissions
        )
        view = GuildRemovalNoticeView(
            guild,
            reason=reason,
            invite_url=invite_url,
            support_url=self.bot.support_invite,
            colour=self.bot.embedcolor,
        )
        try:
            message = await channel.send(
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            try:
                await message.pin(reason="Fishie removal notice")
            except discord.HTTPException:
                self.bot.logger.debug(
                    "Could not pin Fishie removal notice in guild %s", guild.id
                )
        except discord.HTTPException:
            return False
        return True

    # ``guilds`` is reserved for the user avatar command's server-history
    # alias. Keep the owner command available as ``servers`` without
    # registering a conflicting alias during cog loading.
    @commands.command(name="servers")
    async def servers(self, ctx: Context) -> None:
        """Browse guild details and manage Fishie's guild membership."""
        async with ctx.typing():
            snapshots = await self._guild_snapshots()
        if not snapshots:
            await ctx.send("Fishie is not currently in any guilds.")
            return
        view = GuildDirectoryPaginator(ctx, self, snapshots)
        view.message = await ctx.send(
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _add_reaction(
        self, ctx: Context, msg: discord.Message, check: bool = True
    ):
        try:
            await ctx.message.add_reaction(greenTick if check else fish_x)
        except discord.HTTPException:
            pass

    @commands.command(name="reply")
    async def reply(
        self,
        ctx: Context,
        message: Union[str, int],
        channel: Optional[Messageable] = None,
        *,
        text: str,
    ):
        """Reply to a message"""
        _message = await ctx.bot.fetch_message(message=message, channel=channel)

        await _message.reply(text)

        await self._add_reaction(ctx, ctx.message)

    @commands.command(name="message", aliases=("send", "msg", "dm"))
    async def message(
        self,
        ctx: Context,
        channel: Optional[
            Union[AllMsgbleChannels, discord.User]
        ] = commands.CurrentChannel,
        *,
        text: str,
    ):
        """Send a message"""
        target = cast(Messageable | discord.User, channel or ctx.channel)
        await target.send(text, allowed_mentions=discord.AllowedMentions.all())

        await self._add_reaction(ctx, ctx.message)

    @commands.command(name="banip")
    async def banip(self, ctx: Context, ip: str):
        """Ban an IP from sending messages through the website."""
        sql = "INSERT INTO banned_ips (ip) VALUES ($1) ON CONFLICT DO NOTHING"
        await self.bot.pool.execute(sql, ip)
        self.bot.cached_banned_ips.add(ip)
        await self._add_reaction(ctx, ctx.message)

    @commands.command(name="botstatus")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def status(self, ctx: Context, *, text: str):
        """Updates the bot's custom status."""
        text = text.strip()
        if not text:
            raise commands.BadArgument("The custom status cannot be empty.")
        if len(text) > 128:
            raise commands.BadArgument(
                "The custom status cannot be longer than 128 characters."
            )
        if self.bot.user is None:
            raise commands.BadArgument("The bot user is not available.")

        activity = discord.CustomActivity(name=text)
        self.bot.activity = activity
        await self.bot.change_presence(activity=activity)
        await ctx.send(f"Custom status updated to: {text}")

    @commands.command(name="shutdown")
    async def shutdown(self, ctx: Context) -> None:
        """Gracefully stop Fishie so Docker can restart it."""
        message = await ctx.send("Restarting Fishie...")
        await self.bot.pool.execute(
            """
            INSERT INTO bot_restart_state (
                singleton,
                channel_id,
                message_id,
                requested_at
            )
            VALUES (TRUE, $1, $2, now())
            ON CONFLICT (singleton) DO UPDATE
            SET channel_id = EXCLUDED.channel_id,
                message_id = EXCLUDED.message_id,
                requested_at = EXCLUDED.requested_at
            """,
            message.channel.id,
            message.id,
        )
        await asyncio.sleep(1)
        await self.bot.close()

    @commands.command(name="reload")
    async def reload(self, ctx: Context, *extensions: str):
        """Reload extensions. '~' reloads all. No args reloads recently modified."""

        if extensions == ("~",):
            extensions = tuple(self.bot._extensions)

        if not extensions:
            modified = []
            extension_root = Path(__file__).resolve().parents[1]
            for ext in self.bot._extensions:
                pkg_dir = extension_root / ext.split(".", 1)[1]
                if not pkg_dir.is_dir():
                    continue
                for py_file in pkg_dir.rglob("*.py"):
                    if py_file.stat().st_mtime > self._last_reload:
                        modified.append(ext)
                        break

            if not modified:
                await ctx.send("No extensions modified since last reload.")
                return

            extensions = tuple(modified)

        results = []
        for ext in extensions:
            if ext not in self.bot.extensions:
                results.append(f"\u274c `{ext}` not loaded")
                continue
            try:
                await self.bot.reload_extension(ext)
                results.append(f"\U0001f504 `{ext}`")
            except Exception as e:
                results.append(f"\u274c `{ext}` \n```{e}```")

        self._last_reload = time.time()
        e = discord.Embed(color=self.bot.embedcolor, description="\n".join(results))
        await ctx.send(embed=e)

    async def cog_check(self, ctx: commands.Context[Fishie]) -> bool:
        if await ctx.bot.is_owner(ctx.author):
            return True

        raise commands.BadArgument("You are not allowed to use this command.")


async def setup(bot: Fishie):
    await bot.add_cog(Owner(bot))
