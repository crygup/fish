from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Literal

import discord
import emoji as emoji_lib
from discord import app_commands
from discord.ext import commands

from core import Cog
from core.cache import DEFAULT_BOARD_EMOJIS, BoardConfig
from utils import AuthorLayoutView
from utils.converters import TwemojiConverter

if TYPE_CHECKING:
    from extensions.context import Context, GuildContext


BoardType = Literal["starboard", "clownboard"]

BOARD_LABELS: dict[BoardType, str] = {
    "starboard": "Starboard",
    "clownboard": "Clownboard",
}
OTHER_BOARD: dict[BoardType, BoardType] = {
    "starboard": "clownboard",
    "clownboard": "starboard",
}
CUSTOM_EMOJI_RE = re.compile(
    r"^<(?P<animated>a?):(?P<name>[A-Za-z0-9_~]{1,32}):(?P<id>\d{1,20})>$"
)
CHANNEL_ID_RE = re.compile(r"(?:<#)?(?P<id>\d{15,25})>?")


def _board_label(board_type: BoardType) -> str:
    return BOARD_LABELS[board_type]


def _board_default(board_type: BoardType) -> str:
    return DEFAULT_BOARD_EMOJIS[board_type]


def _board_manager_only():
    """Apply the board permission boundary to every leaf subcommand."""

    def decorator(callback: Any) -> Any:
        callback = commands.has_guild_permissions(manage_guild=True)(callback)
        return commands.guild_only()(callback)

    return decorator


async def _authorize_board_interaction(
    interaction: discord.Interaction, ctx: Context
) -> bool:
    """Re-check board ownership and Manage Server before a settings mutation."""

    guild = ctx.guild
    if guild is None or interaction.guild_id != guild.id:
        message = "These board settings can only be used in their original server."
    elif interaction.user.id != ctx.author.id:
        message = "Only the person who opened these board settings can use them."
    else:
        member: discord.Member | None = (
            interaction.user if isinstance(interaction.user, discord.Member) else None
        )
        if member is None or member.guild.id != guild.id:
            member = guild.get_member(interaction.user.id)
        if member and (
            getattr(guild, "owner_id", None) == member.id
            or bool(
                getattr(
                    getattr(member, "guild_permissions", None), "manage_guild", False
                )
            )
        ):
            return True
        message = "You no longer have Manage Server permission to change this board."

    if interaction.response.is_done():
        await interaction.followup.send(
            message,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    else:
        await interaction.response.send_message(
            message,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    return False


class _BoardView(AuthorLayoutView):
    """Author-only board view which also keeps its permission boundary live."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _authorize_board_interaction(interaction, self.ctx)


class BoardChannelModal(discord.ui.Modal, title="Board channel"):
    channel_input = discord.ui.TextInput(
        label="Channel name, ID, or mention",
        placeholder="#starboard, starboard, or 123456789012345678",
        required=True,
        max_length=100,
    )

    def __init__(self, picker: BoardChannelPicker) -> None:
        super().__init__()
        self.picker = picker

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _authorize_board_interaction(interaction, self.picker.ctx):
            return
        try:
            channel = await self.picker.cog._resolve_board_channel(
                self.picker.ctx, str(self.channel_input.value)
            )
            await self.picker.cog._set_board_channel(
                self.picker.ctx, self.picker.board_type, channel
            )
        except (commands.CommandError, discord.HTTPException) as exc:
            await interaction.response.send_message(
                str(exc),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.send_message(
            view=await BoardSettingsView.create(
                self.picker.cog, self.picker.ctx, self.picker.board_type
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class BoardChannelPicker(_BoardView):
    """Button-first destination picker shared by both reaction boards."""

    def __init__(
        self,
        cog: Boards,
        ctx: GuildContext,
        board_type: BoardType,
        *,
        configured: bool,
    ) -> None:
        super().__init__(ctx, timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.board_type: BoardType = board_type
        self.configured = configured
        self._render()

    def _render(self) -> None:
        self.clear_items()
        enter = discord.ui.Button(
            label="Enter channel", style=discord.ButtonStyle.secondary
        )
        enter.callback = self._open_modal
        choose = discord.ui.Button(
            label="Choose channels", style=discord.ButtonStyle.secondary
        )
        choose.callback = self._open_picker
        current = discord.ui.Button(
            label="Set current channel", style=discord.ButtonStyle.secondary
        )
        current.callback = self._set_current
        create = discord.ui.Button(
            label="Create channel", style=discord.ButtonStyle.secondary
        )
        create.callback = self._create_channel
        back = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary)
        back.callback = self._back
        quit_button = discord.ui.Button(
            label="Quit", style=discord.ButtonStyle.secondary
        )
        quit_button.callback = self._quit
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    f"## Configure {_board_label(self.board_type)} channel"
                ),
                discord.ui.TextDisplay(
                    "Enter a channel name, ID, or mention, choose from the paginated "
                    "channel list, use this channel, or create a new one."
                ),
                discord.ui.ActionRow(enter, choose),
                discord.ui.ActionRow(current, create, back, quit_button),
                accent_color=self.ctx.bot.embedcolor,
            )
        )

    async def _open_modal(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(BoardChannelModal(self))

    async def _open_picker(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            view=BoardChannelPages(self),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _set_current(self, interaction: discord.Interaction) -> None:
        channel = self.ctx.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "The command must be run in a text channel to use this option.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            await self.cog._set_board_channel(self.ctx, self.board_type, channel)
        except commands.CommandError as exc:
            await interaction.response.send_message(
                str(exc),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.edit_message(
            view=await BoardSettingsView.create(self.cog, self.ctx, self.board_type),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _create_channel(self, interaction: discord.Interaction) -> None:
        guild = self.ctx.guild
        try:
            channel = await guild.create_text_channel(
                self.board_type,
                reason=f"{_board_label(self.board_type)} created by {interaction.user}",
            )
            await self.cog._set_board_channel(self.ctx, self.board_type, channel)
        except (discord.Forbidden, discord.HTTPException, commands.CommandError) as exc:
            await interaction.response.send_message(
                str(exc) or "I could not create that channel.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.edit_message(
            view=await BoardSettingsView.create(self.cog, self.ctx, self.board_type),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _back(self, interaction: discord.Interaction) -> None:
        if not self.configured:
            self.stop()
            await interaction.response.edit_message(view=None)
            return
        await interaction.response.edit_message(
            view=await BoardSettingsView.create(self.cog, self.ctx, self.board_type),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _quit(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(view=None)


class BoardChannelPages(_BoardView):
    """Three-select, 75-channel pages matching logger/server settings UX."""

    def __init__(self, picker: BoardChannelPicker) -> None:
        super().__init__(picker.ctx, timeout=300)
        self.picker = picker
        self.cog = picker.cog
        self.ctx = picker.ctx
        self.board_type: BoardType = picker.board_type
        self.channels = sorted(
            self.ctx.guild.text_channels, key=lambda channel: channel.position
        )
        self.page = 0
        self.page_count = max(1, (len(self.channels) + 74) // 75)
        self._render()

    def _render(self) -> None:
        self.clear_items()
        channels = self.channels[self.page * 75 : (self.page + 1) * 75]
        rows: list[discord.ui.Item[Any]] = []
        for offset in range(0, len(channels), 25):
            options = [
                discord.SelectOption(
                    label=channel.name[:100],
                    value=str(channel.id),
                    description=f"Position {channel.position}",
                )
                for channel in channels[offset : offset + 25]
            ]
            if not options:
                continue
            select = discord.ui.Select(
                placeholder="Choose a text channel",
                min_values=1,
                max_values=1,
                options=options,
            )
            select.callback = self._select_channel  # type: ignore[assignment]
            rows.append(discord.ui.ActionRow(select))

        previous = discord.ui.Button(label="<", style=discord.ButtonStyle.secondary)
        previous.callback = self._previous
        next_button = discord.ui.Button(label=">", style=discord.ButtonStyle.secondary)
        next_button.callback = self._next
        current = discord.ui.Button(
            label="Set current channel", style=discord.ButtonStyle.secondary
        )
        current.callback = self._set_current
        back = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary)
        back.callback = self._back
        quit_button = discord.ui.Button(
            label="Quit", style=discord.ButtonStyle.secondary
        )
        quit_button.callback = self._quit
        rows.append(
            discord.ui.ActionRow(previous, next_button, current, back, quit_button)
        )
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    f"## Choose {_board_label(self.board_type)} channel\n"
                    f"Page {self.page + 1}/{self.page_count} · "
                    f"{len(self.channels)} channels"
                ),
                *rows,
                accent_color=self.ctx.bot.embedcolor,
            )
        )

    async def _select_channel(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values", [])
        try:
            channel_id = int(values[0])
        except (IndexError, TypeError, ValueError):
            await interaction.response.send_message(
                "Please choose a text channel.", ephemeral=True
            )
            return
        channel = self.ctx.guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "Please choose a text channel.", ephemeral=True
            )
            return
        try:
            await self.cog._set_board_channel(self.ctx, self.board_type, channel)
        except commands.CommandError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.edit_message(
            view=await BoardSettingsView.create(self.cog, self.ctx, self.board_type),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _previous(self, interaction: discord.Interaction) -> None:
        self.page = (self.page - 1) % self.page_count
        self._render()
        await interaction.response.edit_message(
            view=self, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _next(self, interaction: discord.Interaction) -> None:
        self.page = (self.page + 1) % self.page_count
        self._render()
        await interaction.response.edit_message(
            view=self, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _set_current(self, interaction: discord.Interaction) -> None:
        await self.picker._set_current(interaction)

    async def _back(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            view=self.picker, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _quit(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(view=None)


class BoardThresholdModal(discord.ui.Modal, title="Required reactions"):
    count = discord.ui.TextInput(
        label="Reaction count",
        placeholder="3",
        min_length=1,
        max_length=4,
    )

    def __init__(self, view: BoardSettingsView) -> None:
        super().__init__()
        self.board_view = view
        self.count.default = str(view.config.threshold)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _authorize_board_interaction(interaction, self.board_view.ctx):
            return
        try:
            count = int(str(self.count.value))
            await self.board_view.cog._set_board_threshold(
                self.board_view.ctx, self.board_view.board_type, count
            )
        except (ValueError, commands.CommandError) as exc:
            message = (
                str(exc)
                if isinstance(exc, commands.CommandError)
                else "Enter a whole number."
            )
            await interaction.response.send_message(message, ephemeral=True)
            return
        await interaction.response.edit_message(
            view=await BoardSettingsView.create(
                self.board_view.cog,
                self.board_view.ctx,
                self.board_view.board_type,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )


class BoardEmojiModal(discord.ui.Modal, title="Board emoji"):
    emoji = discord.ui.TextInput(
        label="Unicode or server emoji",
        placeholder="⭐, :star:, or <:star:123456789012345678>",
        min_length=1,
        max_length=100,
    )

    def __init__(self, view: BoardSettingsView) -> None:
        super().__init__()
        self.board_view = view
        self.emoji.default = view.config.emoji_display

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _authorize_board_interaction(interaction, self.board_view.ctx):
            return
        try:
            await self.board_view.cog._set_board_emoji(
                self.board_view.ctx,
                self.board_view.board_type,
                str(self.emoji.value),
                interaction.user.id,
            )
        except commands.CommandError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.edit_message(
            view=await BoardSettingsView.create(
                self.board_view.cog,
                self.board_view.ctx,
                self.board_view.board_type,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )


class BoardSettingsView(_BoardView):
    def __init__(
        self,
        cog: Boards,
        ctx: GuildContext,
        board_type: BoardType,
        config: BoardConfig,
        block_counts: tuple[int, int],
    ) -> None:
        super().__init__(ctx, timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.board_type: BoardType = board_type
        self.config = config
        self.block_counts = block_counts
        self._render()

    @classmethod
    async def create(
        cls, cog: Boards, ctx: GuildContext, board_type: BoardType
    ) -> BoardSettingsView:
        config = await cog._get_board(ctx, board_type, normalize=True)
        if config is None:
            raise commands.BadArgument(f"{_board_label(board_type)} is not configured.")
        rows = await ctx.bot.pool.fetch(
            """
            SELECT target_type, COUNT(*) AS count
            FROM guild_board_blocks
            WHERE guild_id = $1 AND board_type = $2
            GROUP BY target_type
            """,
            ctx.guild.id,
            board_type,
        )
        counts = {str(row["target_type"]): int(row["count"]) for row in rows}
        return cls(
            cog,
            ctx,
            board_type,
            config,
            (counts.get("user", 0), counts.get("channel", 0)),
        )

    def _render(self) -> None:
        self.clear_items()
        status = "Enabled" if self.config.enabled else "Disabled"
        nsfw = "Allowed" if self.config.allow_nsfw else "Blocked"
        setter = (
            f" · Set by <@{self.config.emoji_set_by}>"
            if self.config.emoji_set_by
            else " · Set by default"
        )
        users, channels = self.block_counts
        channel_button = discord.ui.Button(
            label="Change channel", style=discord.ButtonStyle.secondary
        )
        channel_button.callback = self._channel
        enabled_button = discord.ui.Button(
            label="Disable" if self.config.enabled else "Enable",
            style=(
                discord.ButtonStyle.danger
                if self.config.enabled
                else discord.ButtonStyle.secondary
            ),
        )
        enabled_button.callback = self._enabled
        stars_button = discord.ui.Button(
            label="Edit required reactions", style=discord.ButtonStyle.secondary
        )
        stars_button.callback = self._stars
        emoji_button = discord.ui.Button(
            label="Edit emoji", style=discord.ButtonStyle.secondary
        )
        emoji_button.callback = self._emoji
        nsfw_button = discord.ui.Button(
            label="Block NSFW" if self.config.allow_nsfw else "Allow NSFW",
            style=discord.ButtonStyle.secondary,
        )
        nsfw_button.callback = self._nsfw
        blocks_button = discord.ui.Button(
            label="View blocks", style=discord.ButtonStyle.secondary
        )
        blocks_button.callback = self._blocks
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    f"## {_board_label(self.board_type)} settings · {self.ctx.guild.name}"
                ),
                discord.ui.TextDisplay(f"**Status:** {status}"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"**Channel:** <#{self.config.channel_id}>\n"
                    f"**Required reactions:** {self.config.threshold:,}\n"
                    f"**Emoji:** {self.config.emoji_display}{setter}\n"
                    f"**NSFW channels:** {nsfw}\n"
                    f"**Blocked:** {users:,} users · {channels:,} channels"
                ),
                discord.ui.Separator(),
                discord.ui.ActionRow(channel_button, enabled_button),
                discord.ui.ActionRow(stars_button, emoji_button, nsfw_button),
                discord.ui.ActionRow(blocks_button),
                accent_color=self.ctx.bot.embedcolor,
            )
        )

    async def _channel(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            view=BoardChannelPicker(
                self.cog, self.ctx, self.board_type, configured=True
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _enabled(self, interaction: discord.Interaction) -> None:
        try:
            await self.cog._set_board_enabled(
                self.ctx, self.board_type, not self.config.enabled
            )
        except commands.CommandError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.edit_message(
            view=await self.create(self.cog, self.ctx, self.board_type),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _stars(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(BoardThresholdModal(self))

    async def _emoji(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(BoardEmojiModal(self))

    async def _nsfw(self, interaction: discord.Interaction) -> None:
        await self.cog._set_board_nsfw(
            self.ctx, self.board_type, not self.config.allow_nsfw
        )
        await interaction.response.edit_message(
            view=await self.create(self.cog, self.ctx, self.board_type),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _blocks(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            view=await BoardBlocksView.create(self.cog, self.ctx, self.board_type),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class BoardBlocksView(_BoardView):
    @classmethod
    async def create(
        cls, cog: Boards, ctx: GuildContext, board_type: BoardType
    ) -> BoardBlocksView:
        self = cls(ctx, timeout=180)
        rows = await ctx.bot.pool.fetch(
            """
            SELECT target_type, target_id
            FROM guild_board_blocks
            WHERE guild_id = $1 AND board_type = $2
            ORDER BY target_type, target_id
            """,
            ctx.guild.id,
            board_type,
        )
        users = [
            f"<@{row['target_id']}>" for row in rows if row["target_type"] == "user"
        ]
        channels = [
            f"<#{row['target_id']}>" for row in rows if row["target_type"] == "channel"
        ]
        lines = [f"## {_board_label(board_type)} blocks"]
        if users:
            suffix = f"\n-# And {len(users) - 50:,} more." if len(users) > 50 else ""
            lines.append("### Users\n" + ", ".join(users[:50]) + suffix)
        if channels:
            suffix = (
                f"\n-# And {len(channels) - 50:,} more." if len(channels) > 50 else ""
            )
            lines.append("### Channels\n" + ", ".join(channels[:50]) + suffix)
        if not users and not channels:
            lines.append("No users or channels are blocked.")
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("\n\n".join(lines)),
                accent_color=ctx.bot.embedcolor,
            )
        )
        return self


class Boards(Cog):
    """Starboard and clownboard configuration commands."""

    async def _refresh_board_cache(
        self, guild_id: int, board_type: BoardType
    ) -> BoardConfig | None:
        row = await self.bot.pool.fetchrow(
            """
            SELECT guild_id, board_type, channel_id, enabled, threshold, allow_nsfw,
                   emoji_name, emoji_id, emoji_animated, emoji_set_by
            FROM guild_boards
            WHERE guild_id = $1 AND board_type = $2
            """,
            guild_id,
            board_type,
        )
        if row is None:
            self.bot.db_cache.remove_board(guild_id, board_type)
            return None
        return self.bot.db_cache.set_board(
            int(row["guild_id"]),
            str(row["board_type"]),
            int(row["channel_id"]),
            enabled=bool(row["enabled"]),
            threshold=int(row["threshold"]),
            allow_nsfw=bool(row["allow_nsfw"]),
            emoji_name=str(row["emoji_name"] or _board_default(board_type)),
            emoji_id=int(row["emoji_id"]) if row["emoji_id"] is not None else None,
            emoji_animated=bool(row["emoji_animated"]),
            emoji_set_by=(
                int(row["emoji_set_by"]) if row["emoji_set_by"] is not None else None
            ),
        )

    async def _get_board(
        self,
        ctx: GuildContext,
        board_type: BoardType,
        *,
        normalize: bool = False,
    ) -> BoardConfig | None:
        config = await self._refresh_board_cache(ctx.guild.id, board_type)
        if config is None or not normalize or config.emoji_id is None:
            return config
        if ctx.guild.get_emoji(config.emoji_id) is not None:
            return config

        if self.bot.db_cache.board_default_emoji_is_available(ctx.guild.id, board_type):
            await self.bot.pool.execute(
                """
                UPDATE guild_boards
                SET emoji_name = $3, emoji_id = NULL, emoji_animated = FALSE,
                    emoji_set_by = NULL, updated_at = NOW()
                WHERE guild_id = $1 AND board_type = $2
                """,
                ctx.guild.id,
                board_type,
                _board_default(board_type),
            )
        else:
            await self.bot.pool.execute(
                """
                UPDATE guild_boards
                SET enabled = FALSE, updated_at = NOW()
                WHERE guild_id = $1 AND board_type = $2
                """,
                ctx.guild.id,
                board_type,
            )
        return await self._refresh_board_cache(ctx.guild.id, board_type)

    async def _resolve_board_channel(
        self, ctx: GuildContext, value: str
    ) -> discord.TextChannel:
        raw = value.strip()
        match = CHANNEL_ID_RE.fullmatch(raw)
        channel: discord.abc.GuildChannel | None = None
        if match:
            channel = ctx.guild.get_channel(int(match.group("id")))
        else:
            name = raw.removeprefix("#").casefold()
            channel = next(
                (
                    item
                    for item in ctx.guild.text_channels
                    if item.name.casefold() == name
                ),
                None,
            )
        if not isinstance(channel, discord.TextChannel):
            raise commands.BadArgument(
                "That text channel could not be found in this server."
            )
        return channel

    @staticmethod
    def _check_board_channel(ctx: GuildContext, channel: discord.TextChannel) -> None:
        me = ctx.guild.me
        if me is None:
            raise commands.BotMissingPermissions(["send_messages", "embed_links"])
        permissions = channel.permissions_for(me)
        missing = [
            name.replace("_", " ").title()
            for name in ("view_channel", "send_messages", "embed_links", "attach_files")
            if not getattr(permissions, name)
        ]
        if missing:
            raise commands.BadArgument(
                f"I need {', '.join(missing)} in {channel.mention} before it can be used."
            )

    async def _set_board_channel(
        self, ctx: GuildContext, board_type: BoardType, channel: discord.TextChannel
    ) -> None:
        self._check_board_channel(ctx, channel)
        current = await self._get_board(ctx, board_type)
        if current is None and not self.bot.db_cache.board_default_emoji_is_available(
            ctx.guild.id, board_type
        ):
            other = _board_label(OTHER_BOARD[board_type])
            raise commands.BadArgument(
                f"{_board_label(board_type)} cannot use its default emoji because "
                f"{other} already uses it. Change or remove the {other} emoji first."
            )
        await self.bot.pool.execute(
            """
            INSERT INTO guild_boards (
                guild_id, board_type, channel_id, enabled, threshold, allow_nsfw,
                emoji_name, emoji_id, emoji_animated, emoji_set_by, updated_at
            )
            VALUES ($1, $2, $3, TRUE, 3, TRUE, $4, NULL, FALSE, NULL, NOW())
            ON CONFLICT (guild_id, board_type) DO UPDATE
            SET channel_id = EXCLUDED.channel_id, updated_at = NOW()
            """,
            ctx.guild.id,
            board_type,
            channel.id,
            _board_default(board_type),
        )
        await self._refresh_board_cache(ctx.guild.id, board_type)

    async def _require_board(
        self, ctx: GuildContext, board_type: BoardType
    ) -> BoardConfig:
        config = await self._get_board(ctx, board_type, normalize=True)
        if config is None:
            raise commands.BadArgument(
                f"Set up {_board_label(board_type)} first with `{ctx.clean_prefix}{board_type}`."
            )
        return config

    async def _set_board_enabled(
        self, ctx: GuildContext, board_type: BoardType, enabled: bool
    ) -> None:
        config = await self._require_board(ctx, board_type)
        if enabled:
            available = (
                self.bot.db_cache.board_default_emoji_is_available(
                    ctx.guild.id, board_type
                )
                if config.emoji_id is not None
                and ctx.guild.get_emoji(config.emoji_id) is None
                else self.bot.db_cache.board_emoji_is_available(
                    ctx.guild.id,
                    board_type,
                    config.emoji_name,
                    config.emoji_id,
                )
            )
            if not available:
                other = _board_label(OTHER_BOARD[board_type])
                raise commands.BadArgument(
                    f"{_board_label(board_type)} cannot be enabled because {other} "
                    "uses the emoji it requires. Change or remove the other board's "
                    "emoji first."
                )
        await self.bot.pool.execute(
            """
            UPDATE guild_boards SET enabled = $3, updated_at = NOW()
            WHERE guild_id = $1 AND board_type = $2
            """,
            ctx.guild.id,
            board_type,
            enabled,
        )
        await self._refresh_board_cache(ctx.guild.id, board_type)

    async def _set_board_threshold(
        self, ctx: GuildContext, board_type: BoardType, count: int
    ) -> None:
        await self._require_board(ctx, board_type)
        if not 1 <= count <= 10_000:
            raise commands.BadArgument(
                "Required reactions must be between 1 and 10,000."
            )
        await self.bot.pool.execute(
            """
            UPDATE guild_boards SET threshold = $3, updated_at = NOW()
            WHERE guild_id = $1 AND board_type = $2
            """,
            ctx.guild.id,
            board_type,
            count,
        )
        await self._refresh_board_cache(ctx.guild.id, board_type)

    def _parse_board_emoji(
        self, ctx: GuildContext, value: str
    ) -> tuple[str, int | None, bool]:
        value = value.strip()
        match = CUSTOM_EMOJI_RE.fullmatch(value)
        if match:
            emoji_id = int(match.group("id"))
            emoji = ctx.guild.get_emoji(emoji_id)
            if emoji is None:
                raise commands.BadArgument(
                    "Custom board emojis must belong to this server."
                )
            return emoji.name, emoji.id, emoji.animated
        # Accept Discord-style shortcode input such as ``:star:`` while
        # storing the actual Unicode emoji used by reaction payloads.
        resolved = emoji_lib.emojize(value, language="alias")
        if resolved == value and value.startswith(":") and value.endswith(":"):
            resolved = emoji_lib.emojize(value.casefold(), language="alias")
        value = resolved
        if not TwemojiConverter.is_unicode_emoji(value):
            raise commands.BadArgument(
                "Provide one Unicode emoji or a custom emoji from this server."
            )
        return value, None, False

    async def _set_board_emoji(
        self,
        ctx: GuildContext,
        board_type: BoardType,
        value: str,
        setter_id: int,
    ) -> None:
        await self._require_board(ctx, board_type)
        name, emoji_id, animated = self._parse_board_emoji(ctx, value)
        if not self.bot.db_cache.board_emoji_is_available(
            ctx.guild.id, board_type, name, emoji_id
        ):
            raise commands.BadArgument(
                f"{_board_label(OTHER_BOARD[board_type])} already uses that emoji. "
                "Each board must use a different emoji."
            )
        await self.bot.pool.execute(
            """
            UPDATE guild_boards
            SET emoji_name = $3, emoji_id = $4, emoji_animated = $5,
                emoji_set_by = $6, updated_at = NOW()
            WHERE guild_id = $1 AND board_type = $2
            """,
            ctx.guild.id,
            board_type,
            name,
            emoji_id,
            animated,
            setter_id,
        )
        await self._refresh_board_cache(ctx.guild.id, board_type)

    async def _set_board_nsfw(
        self, ctx: GuildContext, board_type: BoardType, allowed: bool
    ) -> None:
        await self._require_board(ctx, board_type)
        await self.bot.pool.execute(
            """
            UPDATE guild_boards SET allow_nsfw = $3, updated_at = NOW()
            WHERE guild_id = $1 AND board_type = $2
            """,
            ctx.guild.id,
            board_type,
            allowed,
        )
        await self._refresh_board_cache(ctx.guild.id, board_type)

    async def _send_board(self, ctx: GuildContext, board_type: BoardType) -> None:
        config = await self._get_board(ctx, board_type, normalize=True)
        if config is None:
            view: discord.ui.LayoutView = BoardChannelPicker(
                self, ctx, board_type, configured=False
            )
        else:
            view = await BoardSettingsView.create(self, ctx, board_type)
        view.message = await ctx.send(
            view=view,
            ephemeral=ctx.interaction is not None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _send_board_value(
        self, ctx: GuildContext, board_type: BoardType, value: str
    ) -> None:
        await ctx.send(
            view=discord.ui.LayoutView().add_item(
                discord.ui.Container(
                    discord.ui.TextDisplay(
                        f"## {_board_label(board_type)} settings\n{value}"
                    ),
                    accent_color=ctx.bot.embedcolor,
                )
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _resolve_block_target(
        self, ctx: GuildContext, value: str
    ) -> tuple[str, int]:
        raw = value.strip()
        channel_match = CHANNEL_ID_RE.fullmatch(raw)
        if channel_match:
            target_id = int(channel_match.group("id"))
            channel = ctx.guild.get_channel(target_id)
            if isinstance(channel, discord.abc.GuildChannel):
                return "channel", channel.id
            member = ctx.guild.get_member(target_id)
            if member is not None:
                return "user", member.id
        try:
            channel = await commands.TextChannelConverter().convert(ctx, raw)
        except commands.CommandError:
            channel = None
        if channel is not None:
            return "channel", channel.id
        try:
            member = await commands.MemberConverter().convert(ctx, raw)
        except commands.CommandError as exc:
            raise commands.BadArgument(
                "I could not find that user or channel in this server."
            ) from exc
        return "user", member.id

    async def _board_block(
        self, ctx: GuildContext, board_type: BoardType, target: str | None
    ) -> None:
        await self._require_board(ctx, board_type)
        if target is None:
            await ctx.send(
                view=await BoardBlocksView.create(self, ctx, board_type),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        target_type, target_id = await self._resolve_block_target(ctx, target)
        await self.bot.pool.execute(
            """
            INSERT INTO guild_board_blocks (
                guild_id, board_type, target_type, target_id, blocked_by
            ) VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (guild_id, board_type, target_type, target_id) DO NOTHING
            """,
            ctx.guild.id,
            board_type,
            target_type,
            target_id,
            ctx.author.id,
        )
        self.bot.db_cache.add_board_block(
            ctx.guild.id, board_type, target_type, target_id
        )
        label = f"<@{target_id}>" if target_type == "user" else f"<#{target_id}>"
        await self._send_board_value(ctx, board_type, f"Blocked {label}.")

    async def _board_unblock(
        self, ctx: GuildContext, board_type: BoardType, target: str
    ) -> None:
        await self._require_board(ctx, board_type)
        target_type, target_id = await self._resolve_block_target(ctx, target)
        result = await self.bot.pool.execute(
            """
            DELETE FROM guild_board_blocks
            WHERE guild_id = $1 AND board_type = $2
              AND target_type = $3 AND target_id = $4
            """,
            ctx.guild.id,
            board_type,
            target_type,
            target_id,
        )
        self.bot.db_cache.remove_board_block(
            ctx.guild.id, board_type, target_type, target_id
        )
        if result == "DELETE 0":
            raise commands.BadArgument("That user or channel is not blocked.")
        label = f"<@{target_id}>" if target_type == "user" else f"<#{target_id}>"
        await self._send_board_value(ctx, board_type, f"Unblocked {label}.")

    @commands.hybrid_group(name="starboard", fallback="info")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def starboard(self, ctx: GuildContext) -> None:
        """Set up or manage this server's starboard."""
        await self._send_board(ctx, "starboard")

    @starboard.group(
        name="edit",
        invoke_without_command=True,  # pyright: ignore[reportCallIssue]
    )
    @_board_manager_only()
    async def starboard_edit(self, ctx: GuildContext) -> None:
        """View and edit starboard settings."""
        await self._send_board(ctx, "starboard")

    @starboard_edit.command(name="stars", aliases=("count", "reactions"))
    @_board_manager_only()
    async def starboard_edit_stars(
        self, ctx: GuildContext, count: int | None = None
    ) -> None:
        """View or change the reactions required for starboard."""
        config = await self._require_board(ctx, "starboard")
        if count is None:
            await self._send_board_value(
                ctx, "starboard", f"Required reactions: **{config.threshold:,}**"
            )
            return
        await self._set_board_threshold(ctx, "starboard", count)
        await self._send_board(ctx, "starboard")

    @starboard_edit.command(name="emoji")
    @_board_manager_only()
    async def starboard_edit_emoji(
        self, ctx: GuildContext, *, emoji: str | None = None
    ) -> None:
        """View or change the starboard emoji."""
        config = await self._require_board(ctx, "starboard")
        if emoji is None:
            setter = (
                f" · Set by <@{config.emoji_set_by}>"
                if config.emoji_set_by
                else " · Set by default"
            )
            await self._send_board_value(
                ctx, "starboard", f"Emoji: {config.emoji_display}{setter}"
            )
            return
        await self._set_board_emoji(ctx, "starboard", emoji, ctx.author.id)
        await self._send_board(ctx, "starboard")

    @starboard_edit.command(name="nsfw")
    @_board_manager_only()
    async def starboard_edit_nsfw(
        self, ctx: GuildContext, allowed: bool | None = None
    ) -> None:
        """View or change whether NSFW messages may appear on starboard."""
        config = await self._require_board(ctx, "starboard")
        if allowed is None:
            await self._send_board_value(
                ctx,
                "starboard",
                f"NSFW channels: **{'Allowed' if config.allow_nsfw else 'Blocked'}**",
            )
            return
        await self._set_board_nsfw(ctx, "starboard", allowed)
        await self._send_board(ctx, "starboard")

    @starboard_edit.command(name="channel")
    @_board_manager_only()
    async def starboard_edit_channel(
        self, ctx: GuildContext, channel: discord.TextChannel | None = None
    ) -> None:
        """View or change the starboard channel."""
        config = await self._require_board(ctx, "starboard")
        if channel is None:
            view = BoardChannelPicker(self, ctx, "starboard", configured=True)
            view.message = await ctx.send(
                view=view,
                ephemeral=ctx.interaction is not None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await self._set_board_channel(ctx, "starboard", channel)
        await self._send_board_value(
            ctx, "starboard", f"Channel: <#{config.channel_id}> → {channel.mention}"
        )

    @starboard.command(name="block")
    @_board_manager_only()
    async def starboard_block(
        self, ctx: GuildContext, *, target: str | None = None
    ) -> None:
        """Block a user or channel from starboard."""
        await self._board_block(ctx, "starboard", target)

    @starboard.command(name="unblock")
    @_board_manager_only()
    async def starboard_unblock(self, ctx: GuildContext, *, target: str) -> None:
        """Unblock a user or channel from starboard."""
        await self._board_unblock(ctx, "starboard", target)

    @commands.hybrid_group(name="clownboard", fallback="info", with_app_command=False)
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def clownboard(self, ctx: GuildContext) -> None:
        """Set up or manage this server's clownboard."""
        await self._send_board(ctx, "clownboard")

    @clownboard.group(
        name="edit",
        invoke_without_command=True,  # pyright: ignore[reportCallIssue]
        with_app_command=False,
    )
    @_board_manager_only()
    async def clownboard_edit(self, ctx: GuildContext) -> None:
        """View and edit clownboard settings."""
        await self._send_board(ctx, "clownboard")

    @clownboard_edit.command(
        name="stars", aliases=("count", "reactions"), with_app_command=False
    )
    @_board_manager_only()
    async def clownboard_edit_stars(
        self, ctx: GuildContext, count: int | None = None
    ) -> None:
        """View or change the reactions required for clownboard."""
        config = await self._require_board(ctx, "clownboard")
        if count is None:
            await self._send_board_value(
                ctx, "clownboard", f"Required reactions: **{config.threshold:,}**"
            )
            return
        await self._set_board_threshold(ctx, "clownboard", count)
        await self._send_board(ctx, "clownboard")

    @clownboard_edit.command(name="emoji", with_app_command=False)
    @_board_manager_only()
    async def clownboard_edit_emoji(
        self, ctx: GuildContext, *, emoji: str | None = None
    ) -> None:
        """View or change the clownboard emoji."""
        config = await self._require_board(ctx, "clownboard")
        if emoji is None:
            setter = (
                f" · Set by <@{config.emoji_set_by}>"
                if config.emoji_set_by
                else " · Set by default"
            )
            await self._send_board_value(
                ctx, "clownboard", f"Emoji: {config.emoji_display}{setter}"
            )
            return
        await self._set_board_emoji(ctx, "clownboard", emoji, ctx.author.id)
        await self._send_board(ctx, "clownboard")

    @clownboard_edit.command(name="nsfw", with_app_command=False)
    @_board_manager_only()
    async def clownboard_edit_nsfw(
        self, ctx: GuildContext, allowed: bool | None = None
    ) -> None:
        """View or change whether NSFW messages may appear on clownboard."""
        config = await self._require_board(ctx, "clownboard")
        if allowed is None:
            await self._send_board_value(
                ctx,
                "clownboard",
                f"NSFW channels: **{'Allowed' if config.allow_nsfw else 'Blocked'}**",
            )
            return
        await self._set_board_nsfw(ctx, "clownboard", allowed)
        await self._send_board(ctx, "clownboard")

    @clownboard_edit.command(name="channel", with_app_command=False)
    @_board_manager_only()
    async def clownboard_edit_channel(
        self, ctx: GuildContext, channel: discord.TextChannel | None = None
    ) -> None:
        """View or change the clownboard channel."""
        config = await self._require_board(ctx, "clownboard")
        if channel is None:
            view = BoardChannelPicker(self, ctx, "clownboard", configured=True)
            view.message = await ctx.send(
                view=view,
                ephemeral=ctx.interaction is not None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await self._set_board_channel(ctx, "clownboard", channel)
        await self._send_board_value(
            ctx, "clownboard", f"Channel: <#{config.channel_id}> → {channel.mention}"
        )

    @clownboard.command(name="block", with_app_command=False)
    @_board_manager_only()
    async def clownboard_block(
        self, ctx: GuildContext, *, target: str | None = None
    ) -> None:
        """Block a user or channel from clownboard."""
        await self._board_block(ctx, "clownboard", target)

    @clownboard.command(name="unblock", with_app_command=False)
    @_board_manager_only()
    async def clownboard_unblock(self, ctx: GuildContext, *, target: str) -> None:
        """Unblock a user or channel from clownboard."""
        await self._board_unblock(ctx, "clownboard", target)
