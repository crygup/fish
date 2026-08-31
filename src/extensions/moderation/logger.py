from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Sequence, cast

import discord
from discord.ext import commands

from core import Cog, is_operational_guild
from utils.activities import (
    activity_identity,
    activity_image_url,
    activity_summary,
    loggable_activities,
)

if TYPE_CHECKING:
    from extensions.context import GuildContext


LOGGER_EVENTS: dict[str, str] = {
    "avatar": "Avatar changes",
    "member": "Member joins, leaves, name, and tag changes",
    "activity": "Member game and activity changes",
    "voice": "Voice channel joins, leaves, moves, mutes, deafens, and disconnects",
    "channel": "Channel, thread, and webhook changes",
    "role": "Role changes",
    "server": "Server, emoji, and sticker changes",
    "moderation": "Bans, kicks, prunes, automod, and timeouts",
    "message": "Message edits, deletions, pins, and purges",
}
LOGGER_EVENT_ALIASES = {
    "vc": "voice",
    "voice": "voice",
    "voice_channel": "voice",
    "voicechannel": "voice",
}
LOGGER_AVATAR_PATH = (
    Path(__file__).resolve().parents[2] / "files" / "images" / "pfp.jpg"
)


def _asset_key(asset: discord.Asset | None) -> str | None:
    return asset.key if asset is not None else None


def _display(value: object | None) -> str:
    if value is None or value == "":
        return "None"
    value = discord.utils.escape_mentions(discord.utils.escape_markdown(str(value)))
    return value if len(value) <= 1000 else value[:997] + "..."


def _attachment_signature(attachment: discord.Attachment) -> tuple[object, ...]:
    return (
        getattr(attachment, "id", None),
        getattr(attachment, "filename", None),
        getattr(attachment, "size", None),
        getattr(attachment, "content_type", None),
        getattr(attachment, "url", None),
    )


def _embed_signature(embed: discord.Embed) -> str:
    try:
        return repr(embed.to_dict())
    except (AttributeError, TypeError):
        return repr(embed)


def _message_sticker_signature(sticker: object) -> tuple[object, ...]:
    return (
        getattr(sticker, "id", None),
        getattr(sticker, "name", None),
        getattr(sticker, "format", None),
    )


def _attachment_lines(attachments: Sequence[discord.Attachment]) -> str:
    if not attachments:
        return "None"
    lines = []
    for attachment in attachments:
        filename = discord.utils.escape_markdown(str(attachment.filename))
        lines.append(f"[{filename}]({attachment.url})")
    return "\n".join(lines)[:1024]


def _canonical_logger_event(event: str) -> str:
    normalized = event.strip().casefold().replace("-", "_")
    return LOGGER_EVENT_ALIASES.get(normalized, normalized)


def _activity_identity_sort_key(
    identity: tuple[int, str, int | None],
) -> tuple[int, str, int]:
    """Return a consistently sortable key for an activity identity.

    Discord activities do not all have an application ID.  Sorting the raw
    identity tuple would therefore compare ``None`` with an integer when two
    activities share the same type and name.
    """

    activity_type, name, application_id = identity
    return activity_type, name, application_id if application_id is not None else -1


def _permission_name(name: str) -> str:
    aliases = {
        "read_messages": "View Channel",
        "external_emojis": "Use External Emojis",
        "external_stickers": "Use External Stickers",
        "manage_guild": "Manage Server",
    }
    if name in aliases:
        return aliases[name]
    return name.replace("_", " ").title()


def _role_permission_changes(
    before: discord.Permissions,
    after: discord.Permissions,
) -> list[str]:
    changes: list[str] = []
    for name, enabled in after:
        previous = bool(getattr(before, name))
        if previous == enabled:
            continue
        action = "Allowed" if enabled else "Removed"
        changes.append(f"**{action}:** {_permission_name(name)}")
    return changes


def _overwrite_key(
    target: discord.Role | discord.Member | discord.Object,
) -> tuple[str, int]:
    if isinstance(target, discord.Member):
        kind = "user"
    elif isinstance(target, discord.Role):
        kind = "role"
    else:
        kind = "object"
    return kind, target.id


def _channel_permission_changes(
    before: discord.abc.GuildChannel,
    after: discord.abc.GuildChannel,
) -> tuple[list[str], discord.AuditLogAction]:
    before_targets = {_overwrite_key(target): target for target in before.overwrites}
    after_targets = {_overwrite_key(target): target for target in after.overwrites}
    keys = before_targets.keys() | after_targets.keys()
    changes: list[str] = []
    created = False
    deleted = False

    for key in sorted(keys):
        previous_target = before_targets.get(key)
        current_target = after_targets.get(key)
        target = current_target or previous_target
        if target is None:
            continue
        previous = (
            before.overwrites_for(previous_target)
            if previous_target is not None
            else discord.PermissionOverwrite()
        )
        current = (
            after.overwrites_for(current_target)
            if current_target is not None
            else discord.PermissionOverwrite()
        )
        if previous == current:
            continue
        created = created or previous_target is None
        deleted = deleted or current_target is None

        if isinstance(target, discord.Role) and target.is_default():
            label = "Overall (`@everyone`)"
        elif isinstance(target, discord.Member):
            label = f"{target.mention} (User)"
        elif isinstance(target, discord.Role):
            label = f"{target.mention} (Role)"
        else:
            label = f"`{target.id}` (Unknown target)"

        previous_values = dict(previous)
        current_values = dict(current)
        target_changes: list[str] = []
        for name in sorted(previous_values.keys() | current_values.keys()):
            old_value = previous_values.get(name)
            new_value = current_values.get(name)
            if old_value == new_value:
                continue
            state = {
                True: "Allowed",
                False: "Denied",
                None: "Reset",
            }[new_value]
            target_changes.append(f"{state} {_permission_name(name)}")
        if target_changes:
            changes.append(f"**{label}**\n" + "\n".join(target_changes))

    action = discord.AuditLogAction.overwrite_update
    if created and not deleted:
        action = discord.AuditLogAction.overwrite_create
    elif deleted and not created:
        action = discord.AuditLogAction.overwrite_delete
    return changes, action


@dataclass(frozen=True, slots=True)
class _ChannelPositionChange:
    """Snapshot of one channel/category move for the short batching window."""

    guild: discord.Guild
    channel_id: int
    channel_label: str
    before_position: object | None
    after_position: object | None


@dataclass(frozen=True, slots=True)
class _RolePositionChange:
    """Snapshot of one role move during the short batching window."""

    guild: discord.Guild
    role_id: int
    role_label: str
    before_position: object | None
    after_position: object | None


CHANNEL_MOVE_BATCH_DELAY = 0.75


async def _authorize_logger_interaction(
    interaction: discord.Interaction, ctx: GuildContext
) -> bool:
    """Keep logger controls behind a live Manage Server check.

    Logger setup views can remain attached to a message for several minutes,
    so the command-level permission check is not sufficient.  The original
    author must still be the actor, and their current member permissions are
    checked immediately before changing a logger destination.
    """

    guild = ctx.guild
    if guild is None or interaction.guild_id != guild.id:
        message = "These logger controls can only be used in their original server."
    elif interaction.user.id != ctx.author.id:
        message = "Only the person who opened the logger controls can use these buttons."
    else:
        member = interaction.user
        if (
            getattr(getattr(member, "guild", None), "id", None) != guild.id
            or not hasattr(member, "guild_permissions")
        ):
            get_member = getattr(guild, "get_member", None)
            member = get_member(interaction.user.id) if callable(get_member) else None
        if member and (
            getattr(guild, "owner_id", None) == member.id
            or bool(
                getattr(
                    getattr(member, "guild_permissions", None), "manage_guild", False
                )
            )
        ):
            return True
        message = "You no longer have Manage Server permission to change logger settings."

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


class _LoggerView(discord.ui.LayoutView):
    def __init__(self, ctx: GuildContext, *, timeout: float = 300) -> None:
        super().__init__(timeout=timeout)
        self.ctx = ctx
        self.author_id = ctx.author.id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _authorize_logger_interaction(interaction, self.ctx)


class LoggerChannelModal(discord.ui.Modal, title="Logger channel"):
    channel_input = discord.ui.TextInput(
        label="Text channel name or ID",
        placeholder="#logs, logs, or 123456789012345678",
        required=True,
        max_length=100,
    )

    def __init__(self, cog: Logger, ctx: GuildContext, event: str) -> None:
        super().__init__()
        self.cog = cog
        self.ctx = ctx
        self.event = event

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _authorize_logger_interaction(interaction, self.ctx):
            return
        try:
            channel = self.cog._resolve_logger_channel(
                self.ctx.guild, str(self.channel_input.value)
            )
            await self.cog._set_logger_channel(
                self.ctx,
                self.event,
                channel,
                announce=False,
                actor_id=interaction.user.id,
            )
        except (
            commands.BadArgument,
            commands.BotMissingPermissions,
            discord.HTTPException,
        ) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"{LOGGER_EVENTS[self.event]} will now be logged in {channel.mention}.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class LoggerChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, view: LoggerChannelPickerView) -> None:
        self.logger_view = view
        super().__init__(
            placeholder="Choose a text channel",
            min_values=1,
            max_values=1,
            channel_types=[discord.ChannelType.text],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        selected = self.values[0]
        # Channel selects return AppCommandChannel objects rather than the
        # cached TextChannel instance used by the logger setup code.
        channel = selected.resolve()
        if channel is None:
            channel = self.logger_view.ctx.guild.get_channel(selected.id)
        if channel is None:
            try:
                channel = await selected.fetch()
            except discord.HTTPException:
                channel = None
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "Please choose a text channel.", ephemeral=True
            )
            return
        try:
            await self.logger_view.cog._set_logger_channel(
                self.logger_view.ctx,
                self.logger_view.event,
                channel,
                announce=False,
                actor_id=interaction.user.id,
            )
        except (
            commands.BadArgument,
            commands.BotMissingPermissions,
            discord.HTTPException,
        ) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"{LOGGER_EVENTS[self.logger_view.event]} will now be logged in {channel.mention}.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class LoggerChannelPickerView(_LoggerView):
    def __init__(self, cog: Logger, ctx: GuildContext, event: str) -> None:
        super().__init__(ctx)
        self.cog = cog
        self.event = event
        choose = discord.ui.Button(
            label="Choose channels", style=discord.ButtonStyle.secondary
        )
        choose.callback = self._open_picker
        choose_text = discord.ui.Button(
            label="Enter channel name/ID", style=discord.ButtonStyle.secondary
        )
        choose_text.callback = self._open_modal
        current = discord.ui.Button(
            label="Set to current channel", style=discord.ButtonStyle.success
        )
        current.callback = self._set_current
        back = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary)
        back.callback = self._back
        quit_button = discord.ui.Button(
            label="Quit", style=discord.ButtonStyle.secondary
        )
        quit_button.callback = self._quit
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(f"## Configure {LOGGER_EVENTS[event]}"),
                discord.ui.TextDisplay(
                    "Choose a channel from the paginated list, enter a channel name/ID, "
                    "or set this command's channel."
                ),
                discord.ui.ActionRow(choose_text, choose),
                discord.ui.ActionRow(current, back, quit_button),
            )
        )

    async def _open_modal(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            LoggerChannelModal(self.cog, self.ctx, self.event)
        )

    async def _open_picker(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            view=LoggerChannelPages(self.cog, self.ctx, self.event),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _set_current(self, interaction: discord.Interaction) -> None:
        channel = self.ctx.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "The command must be run in a text channel to use this option.",
                ephemeral=True,
            )
            return
        try:
            await self.cog._set_logger_channel(
                self.ctx,
                self.event,
                channel,
                announce=False,
                actor_id=interaction.user.id,
            )
        except (
            commands.BadArgument,
            commands.BotMissingPermissions,
            discord.HTTPException,
        ) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        configured = await self.cog._configured_logger_channels(self.ctx.guild.id)
        await interaction.response.edit_message(
            view=LoggerPanelView(self.cog, self.ctx, configured),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _back(self, interaction: discord.Interaction) -> None:
        configured = await self.cog._configured_logger_channels(self.ctx.guild.id)
        await interaction.response.edit_message(
            view=LoggerPanelView(self.cog, self.ctx, configured),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _quit(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(view=None)


class LoggerChannelPages(_LoggerView):
    """Paginated text-channel picker used by the logger controls."""

    def __init__(self, cog: Logger, ctx: GuildContext, event: str) -> None:
        super().__init__(ctx)
        self.cog = cog
        self.event = event
        self.channels = sorted(
            ctx.guild.text_channels, key=lambda channel: channel.position
        )
        self.page = 0
        self.page_count = max(1, (len(self.channels) + 74) // 75)
        self._render()

    def _render(self) -> None:
        self.clear_items()
        page_channels = self.channels[self.page * 75 : (self.page + 1) * 75]
        rows: list[discord.ui.Item[Any]] = []
        for offset in range(0, len(page_channels), 25):
            options = [
                discord.SelectOption(
                    label=channel.name[:100],
                    value=str(channel.id),
                    description=f"Position {channel.position}",
                )
                for channel in page_channels[offset : offset + 25]
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
                    f"## Choose {LOGGER_EVENTS[self.event]} channel\n"
                    f"Page {self.page + 1}/{self.page_count} · {len(self.channels)} channels"
                ),
                *rows,
            )
        )

    async def _select_channel(self, interaction: discord.Interaction) -> None:
        data = interaction.data or {}
        values = data.get("values", [])
        if not values:
            await interaction.response.send_message(
                "Please choose a channel.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            channel_id = int(values[0])
        except (TypeError, ValueError):
            await interaction.response.send_message(
                "Please choose a text channel.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        channel = self.ctx.guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "Please choose a text channel.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            await self.cog._set_logger_channel(
                self.ctx,
                self.event,
                channel,
                announce=False,
                actor_id=interaction.user.id,
            )
        except (
            commands.BadArgument,
            commands.BotMissingPermissions,
            discord.HTTPException,
        ) as exc:
            await interaction.response.send_message(
                str(exc),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        configured = await self.cog._configured_logger_channels(self.ctx.guild.id)
        await interaction.response.edit_message(
            view=LoggerPanelView(self.cog, self.ctx, configured),
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
        channel = self.ctx.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "The command must be run in a text channel to use this option.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            await self.cog._set_logger_channel(
                self.ctx,
                self.event,
                channel,
                announce=False,
                actor_id=interaction.user.id,
            )
        except (
            commands.BadArgument,
            commands.BotMissingPermissions,
            discord.HTTPException,
        ) as exc:
            await interaction.response.send_message(
                str(exc),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        configured = await self.cog._configured_logger_channels(self.ctx.guild.id)
        await interaction.response.edit_message(
            view=LoggerPanelView(self.cog, self.ctx, configured),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _back(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            view=LoggerChannelPickerView(self.cog, self.ctx, self.event),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _quit(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(view=None)


class LoggerConfigureButton(discord.ui.Button):
    def __init__(self, cog: Logger, ctx: GuildContext, event: str) -> None:
        self.cog = cog
        self.ctx = ctx
        self.event = event
        super().__init__(label="Choose channel", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            view=LoggerChannelPickerView(self.cog, self.ctx, self.event),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class LoggerSetCurrentButton(discord.ui.Button):
    def __init__(self, cog: Logger, ctx: GuildContext, event: str) -> None:
        self.cog = cog
        self.ctx = ctx
        self.event = event
        super().__init__(
            label="Set to current channel", style=discord.ButtonStyle.success
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        channel = self.ctx.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "The command must be run in a text channel to use this option.",
                ephemeral=True,
            )
            return
        try:
            await self.cog._set_logger_channel(
                self.ctx,
                self.event,
                channel,
                announce=False,
                actor_id=interaction.user.id,
            )
        except (
            commands.BadArgument,
            commands.BotMissingPermissions,
            discord.HTTPException,
        ) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        configured = await self.cog._configured_logger_channels(self.ctx.guild.id)
        await interaction.response.edit_message(
            view=LoggerPanelView(self.cog, self.ctx, configured),
        )


class LoggerPanelView(_LoggerView):
    def __init__(
        self,
        cog: Logger,
        ctx: GuildContext,
        configured: dict[str, int],
    ) -> None:
        super().__init__(ctx)
        children: list[discord.ui.Item] = [
            discord.ui.TextDisplay(f"## Event logger for {ctx.guild.name}"),
            discord.ui.TextDisplay(
                "Choose an event to select its logging channel. Each event uses a dedicated webhook."
            ),
        ]
        for event, label in LOGGER_EVENTS.items():
            destination = (
                f"Currently: <#{configured[event]}>"
                if event in configured
                else "Currently: not configured"
            )
            children.extend(
                (
                    discord.ui.TextDisplay(f"**{label}**\n{destination}"),
                    discord.ui.ActionRow(
                        LoggerConfigureButton(cog, ctx, event),
                        LoggerSetCurrentButton(cog, ctx, event),
                    ),
                )
            )
        self.add_item(discord.ui.Container(*children))


class Logger(Cog):
    """Per-server event logging to configured Discord channels."""

    # Kept as a fallback for management scripts and tests that construct a
    # lightweight bot without the runtime identity properties.
    LEGACY_BOT_ID = 876391494485950504
    REPLACEMENT_BOT_ID = 1537535633038381190

    _pending_purges: dict[tuple[int, int], float]
    _webhook_locks: dict[tuple[int, str], asyncio.Lock]
    _logged_webhook_creations: dict[tuple[int, int, str], float]
    _logged_prunes: dict[int, float]
    _logged_audit_events: dict[int, float]
    _channel_move_batches: dict[int, list[_ChannelPositionChange]]
    _channel_move_tasks: dict[int, asyncio.Task[None]]
    _role_move_batches: dict[int, list[_RolePositionChange]]
    _role_move_tasks: dict[int, asyncio.Task[None]]
    _logger_rebind_task: asyncio.Task[None] | None

    def _is_replacement_instance(self) -> bool:
        """Return whether this process is running the replacement app."""

        marker = getattr(self.bot, "is_new_bot", None)
        if marker is not None:
            try:
                return bool(marker() if callable(marker) else marker)
            except Exception:
                pass
        user = getattr(self.bot, "user", None)
        try:
            return int(getattr(user, "id", 0)) == self.REPLACEMENT_BOT_ID
        except (TypeError, ValueError):
            return False

    def _is_legacy_instance(self) -> bool:
        """Return whether this logger is running on the retiring bot."""

        marker = getattr(self.bot, "is_legacy_bot", None)
        if marker is not None:
            try:
                return bool(marker() if callable(marker) else marker)
            except Exception:
                pass
        for attribute in ("instance", "bot_instance"):
            marker = getattr(self.bot, attribute, None)
            if marker is None:
                continue
            try:
                marker = marker() if callable(marker) else marker
                return str(marker).casefold() in {"legacy", "old", "primary"}
            except Exception:
                pass
        user = getattr(self.bot, "user", None)
        try:
            return int(getattr(user, "id", 0)) == self.LEGACY_BOT_ID
        except (TypeError, ValueError):
            return False

    def _replacement_member_present(self, guild: discord.Guild) -> bool:
        """Return whether the replacement application is in *guild*.

        Bot members are normally cached even when a deployment has not
        enabled privileged member chunking.  Iterate the cache as a fallback
        for lightweight guild doubles used in tests.
        """

        replacement_id = self.REPLACEMENT_BOT_ID
        marker = getattr(self.bot, "new_bot_id", None)
        if marker is not None:
            try:
                replacement_id = int(marker() if callable(marker) else marker)
            except (TypeError, ValueError):
                pass
        try:
            if guild.get_member(replacement_id) is not None:
                return True
        except (AttributeError, TypeError):
            pass
        for member in list(getattr(guild, "members", ()) or ()):
            try:
                if int(getattr(member, "id", 0)) == replacement_id:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    async def _logger_webhook_owner_id(self, url: object) -> int | None:
        """Resolve the account that owns a logger webhook, when possible."""

        session = getattr(self.bot, "session", None)
        if session is None:
            return None
        try:
            webhook = discord.Webhook.from_url(
                str(url), session=session
            )
            fetched = await webhook.fetch()
        except (
            discord.HTTPException,
            discord.NotFound,
            discord.Forbidden,
            TypeError,
            ValueError,
            AttributeError,
        ):
            # A deleted/invalid URL is handled by the normal lazy replacement
            # path when the first event is emitted.
            return None
        owner = getattr(getattr(fetched, "user", None), "id", None)
        try:
            return int(owner) if owner is not None else None
        except (TypeError, ValueError):
            return None

    async def rebind_logger_webhooks(self) -> int:
        """Replace legacy-created logger webhooks for the new application.

        Logger webhook URLs are bearer credentials and remain executable even
        after the original bot leaves a guild.  The replacement process must
        therefore create its own webhook per configured logger row, update the
        shared URL, and then delete the legacy webhook.  Static config
        webhooks (error/images/messages/phone logs) are intentionally outside
        this table and are never touched.
        """

        if not self._is_replacement_instance():
            return 0
        rows = await self.bot.pool.fetch(
            "SELECT guild_id, event, channel_id, webhook_url "
            "FROM guild_log_channels WHERE webhook_url IS NOT NULL"
        )
        config = getattr(self.bot, "config", None)
        configured_webhooks = (
            config.get("webhooks", {}) if isinstance(config, dict) else {}
        )
        static_urls = {
            str(value)
            for value in configured_webhooks.values()
            if value
        }
        rebound = 0
        for row in rows:
            guild_id = int(row["guild_id"])
            if is_operational_guild(guild_id):
                continue
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            channel = guild.get_channel(int(row["channel_id"]))
            if not isinstance(channel, discord.TextChannel):
                continue
            stored_event = str(row["event"])
            event = _canonical_logger_event(stored_event)
            old_url = row["webhook_url"]
            if not old_url:
                continue
            # Keep deployment-wide webhooks (error/image/message/phone logs)
            # immutable even if a stale row happens to reference one.
            if str(old_url) in static_urls:
                continue
            owner_id = await self._logger_webhook_owner_id(old_url)
            current_id = getattr(self.bot, "active_bot_id", None)
            try:
                current_id = int(
                    current_id() if callable(current_id) else current_id
                )
            except (TypeError, ValueError):
                current_id = self.REPLACEMENT_BOT_ID
            if owner_id == current_id:
                continue

            async with self._webhook_lock(guild_id, event):
                current = await self.bot.pool.fetchrow(
                    "SELECT channel_id, webhook_url FROM guild_log_channels "
                    "WHERE guild_id = $1 AND event = $2",
                    guild_id,
                    stored_event,
                )
                if current is None or not current["webhook_url"]:
                    continue
                current_url = str(current["webhook_url"])
                # Another process may have completed the replacement while we
                # were fetching the owner.  Re-check the current URL before
                # creating a second webhook.
                current_owner = await self._logger_webhook_owner_id(current_url)
                if current_owner == current_id:
                    continue
                try:
                    replacement = await self._create_logger_webhook(
                        channel,
                        event,
                        created_by=current_id,
                    )
                    updated = await self.bot.pool.execute(
                        "UPDATE guild_log_channels SET webhook_url = $1 "
                        "WHERE guild_id = $2 AND event = $3 AND webhook_url = $4",
                        replacement.url,
                        guild_id,
                        stored_event,
                        current_url,
                    )
                    # ``asyncpg`` returns ``UPDATE <count>``.  If a concurrent
                    # setting change won the race, remove the orphan webhook
                    # and leave the row untouched.
                    if not updated.endswith(" 1"):
                        await replacement.delete()
                        continue
                    await self._delete_logger_webhook(
                        current_url, guild_id, event
                    )
                    rebound += 1
                except (
                    discord.HTTPException,
                    commands.BotMissingPermissions,
                    OSError,
                    TypeError,
                    ValueError,
                ):
                    self.bot.logger.warning(
                        "Could not rebind logger webhook for guild %s event %s",
                        guild_id,
                        event,
                        exc_info=True,
                    )
        return rebound

    async def _rebind_logger_webhooks_when_ready(self) -> None:
        try:
            await self.bot.wait_until_ready()
            rebound = await self.rebind_logger_webhooks()
            if rebound:
                self.bot.logger.info(
                    "Rebound %s logger webhook(s) to the replacement bot", rebound
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            self.bot.logger.exception("Logger webhook rebinding failed")

    def _queue_channel_position_update(
        self,
        before: discord.abc.GuildChannel,
        after: discord.abc.GuildChannel,
    ) -> None:
        batches: dict[int, list[_ChannelPositionChange]] = getattr(
            self, "_channel_move_batches", {}
        )
        tasks: dict[int, asyncio.Task[None]] = getattr(self, "_channel_move_tasks", {})
        self._channel_move_batches = batches
        self._channel_move_tasks = tasks

        name = str(getattr(after, "name", after.id))
        label = getattr(after, "mention", None) or f"`{_display(name)}`"
        guild_id = after.guild.id
        batches.setdefault(guild_id, []).append(
            _ChannelPositionChange(
                guild=after.guild,
                channel_id=after.id,
                channel_label=label,
                before_position=getattr(before, "position", None),
                after_position=getattr(after, "position", None),
            )
        )
        if guild_id not in tasks:
            tasks[guild_id] = asyncio.create_task(
                self._flush_channel_position_updates(guild_id)
            )

    async def _flush_channel_position_updates(self, guild_id: int) -> None:
        try:
            await asyncio.sleep(CHANNEL_MOVE_BATCH_DELAY)
            batches: dict[int, list[_ChannelPositionChange]] = getattr(
                self, "_channel_move_batches", {}
            )
            changes = batches.pop(guild_id, [])
            if not changes:
                return

            # Discord can emit more than one update for a channel during a
            # reorder. Keep its original position and the final position only.
            merged: dict[int, _ChannelPositionChange] = {}
            for change in changes:
                previous = merged.get(change.channel_id)
                if previous is None:
                    merged[change.channel_id] = change
                else:
                    merged[change.channel_id] = _ChannelPositionChange(
                        guild=change.guild,
                        channel_id=change.channel_id,
                        channel_label=change.channel_label,
                        before_position=previous.before_position,
                        after_position=change.after_position,
                    )
            changes = list(merged.values())

            # Position-only gateway updates are also emitted when Discord
            # recalculates every channel's position as a side effect (for
            # example after a category sync).  Those updates do not always
            # have an audit-log entry of their own.  If the logger can view
            # the audit log, require a recent matching channel update before
            # writing a channel-move event; otherwise a harmless cache sync
            # appears as a real moderation action with no actor.
            guild = changes[0].guild
            me = guild.me
            if me is not None and me.guild_permissions.view_audit_log:
                target_ids = {change.channel_id for change in changes}
                audit_entry = await self._recent_audit_entry(
                    guild,
                    (discord.AuditLogAction.channel_update,),
                    target_id=(changes[0].channel_id if len(changes) == 1 else None),
                    target_ids=target_ids if len(changes) > 1 else None,
                    max_age=30,
                )
                if audit_entry is None:
                    self.bot.logger.debug(
                        "Skipping channel position log for guild %s: "
                        "no matching audit-log entry",
                        guild.id,
                    )
                    return

            embed = self._channel_position_embed(changes)
            await self._emit_logger(
                guild,
                "channel",
                embed,
                audit_action=discord.AuditLogAction.channel_update,
                audit_target_id=changes[0].channel_id if len(changes) == 1 else None,
                audit_target_ids={change.channel_id for change in changes},
            )
        finally:
            tasks: dict[int, asyncio.Task[None]] = getattr(
                self, "_channel_move_tasks", {}
            )
            if tasks.get(guild_id) is asyncio.current_task():
                tasks.pop(guild_id, None)
                batches = getattr(self, "_channel_move_batches", {})
                if batches.get(guild_id):
                    tasks[guild_id] = asyncio.create_task(
                        self._flush_channel_position_updates(guild_id)
                    )

    def _queue_role_position_update(
        self,
        before: discord.Role,
        after: discord.Role,
    ) -> None:
        batches: dict[int, list[_RolePositionChange]] = getattr(
            self, "_role_move_batches", {}
        )
        tasks: dict[int, asyncio.Task[None]] = getattr(self, "_role_move_tasks", {})
        self._role_move_batches = batches
        self._role_move_tasks = tasks

        guild_id = after.guild.id
        batches.setdefault(guild_id, []).append(
            _RolePositionChange(
                guild=after.guild,
                role_id=after.id,
                role_label=after.mention or f"`{_display(after.name)}`",
                before_position=getattr(before, "position", None),
                after_position=getattr(after, "position", None),
            )
        )
        if guild_id not in tasks:
            tasks[guild_id] = asyncio.create_task(
                self._flush_role_position_updates(guild_id)
            )

    async def _flush_role_position_updates(self, guild_id: int) -> None:
        try:
            await asyncio.sleep(CHANNEL_MOVE_BATCH_DELAY)
            batches: dict[int, list[_RolePositionChange]] = getattr(
                self, "_role_move_batches", {}
            )
            changes = batches.pop(guild_id, [])
            if not changes:
                return

            # A single Discord role reorder produces update events for every
            # role that shifted around the moved role.  The first event is the
            # useful one; emit it once instead of flooding the logger.
            change = changes[0]
            embed = self._role_position_embed(change)
            await self._emit_logger(
                change.guild,
                "role",
                embed,
                audit_action=discord.AuditLogAction.role_update,
                audit_target_id=change.role_id if len(changes) == 1 else None,
                audit_target_ids={item.role_id for item in changes},
            )
        finally:
            tasks: dict[int, asyncio.Task[None]] = getattr(self, "_role_move_tasks", {})
            if tasks.get(guild_id) is asyncio.current_task():
                tasks.pop(guild_id, None)
                batches = getattr(self, "_role_move_batches", {})
                if batches.get(guild_id):
                    tasks[guild_id] = asyncio.create_task(
                        self._flush_role_position_updates(guild_id)
                    )

    @staticmethod
    def _channel_position_embed(
        changes: Sequence[_ChannelPositionChange],
    ) -> discord.Embed:
        if len(changes) == 1:
            change = changes[0]
            embed = Logger._embed(
                "Channel updated",
                f"{change.channel_label} was updated.",
                color=discord.Colour.orange(),
            )
            embed.add_field(
                name="Position",
                value=(
                    f"Before: {_display(change.before_position)}\n"
                    f"After: {_display(change.after_position)}"
                ),
                inline=False,
            )
            Logger._add_item_id(embed, discord.Object(change.channel_id))
            return embed

        entries = [
            f"{change.channel_label} (ID: {change.channel_id}): "
            f"{_display(change.before_position)} -> "
            f"{_display(change.after_position)}"
            for change in changes
        ]
        summary = "Multiple channels or categories were moved together."
        visible: list[str] = []
        for entry in entries:
            candidate = summary + "\n\n" + "\n\n".join((*visible, entry))
            if len(candidate) > 4096:
                break
            visible.append(entry)
        remaining = len(entries) - len(visible)
        details = "\n\n".join(visible)
        if remaining:
            suffix = f"\n\n… and {remaining} more."
            while visible and len(summary) + 2 + len(details) + len(suffix) > 4096:
                visible.pop()
                details = "\n\n".join(visible)
            details += suffix
        embed = Logger._embed(
            "Channels moved",
            summary + "\n\n" + details,
            color=discord.Colour.orange(),
        )
        return embed

    @staticmethod
    def _role_position_embed(change: _RolePositionChange) -> discord.Embed:
        embed = Logger._embed(
            "Roles moved",
            f"{change.role_label} was moved.",
            color=discord.Colour.orange(),
        )
        embed.add_field(
            name="Position",
            value=(
                f"Before: {_display(change.before_position)}\n"
                f"After: {_display(change.after_position)}"
            ),
            inline=False,
        )
        Logger._add_item_id(embed, discord.Object(change.role_id))
        return embed

    def _webhook_lock(self, guild_id: int, event: str) -> asyncio.Lock:
        locks: dict[tuple[int, str], asyncio.Lock] = getattr(self, "_webhook_locks", {})
        self._webhook_locks = locks
        return locks.setdefault((guild_id, event), asyncio.Lock())

    @staticmethod
    def _audit_channel_id(entry: discord.AuditLogEntry) -> int | None:
        """Return the channel ID carried by an audit entry, when available."""
        target = getattr(entry, "target", None)
        for value in (
            getattr(target, "channel_id", None),
            getattr(getattr(target, "channel", None), "id", None),
            getattr(getattr(entry, "extra", None), "channel", None),
            getattr(
                getattr(getattr(entry, "extra", None), "channel", None), "id", None
            ),
        ):
            if isinstance(value, int):
                return value
            value_id = getattr(value, "id", None)
            if isinstance(value_id, int):
                return value_id
        for state in (getattr(entry, "before", None), getattr(entry, "after", None)):
            state_channel_id = getattr(state, "channel_id", None)
            if isinstance(state_channel_id, int):
                return state_channel_id
            channel = getattr(state, "channel", None)
            channel_id = getattr(channel, "id", None)
            if isinstance(channel_id, int):
                return channel_id
        return None

    async def _recent_audit_entry(
        self,
        guild: discord.Guild,
        actions: Sequence[discord.AuditLogAction],
        *,
        target_id: int | None = None,
        target_ids: set[int] | None = None,
        channel_id: int | None = None,
        max_age: float = 20,
    ) -> discord.AuditLogEntry | None:
        """Find a recent audit entry while tolerating Discord's delayed events."""
        me = guild.me
        if me is None or not me.guild_permissions.view_audit_log:
            return None
        action_set = set(actions)
        wanted_ids = target_ids or set()
        for attempt in range(3):
            try:
                async for entry in guild.audit_logs(limit=30):
                    if entry.action not in action_set:
                        continue
                    age = (discord.utils.utcnow() - entry.created_at).total_seconds()
                    if not -2 <= age <= max_age:
                        continue
                    entry_target_id = getattr(entry.target, "id", None)
                    if target_id is not None and entry_target_id != target_id:
                        continue
                    if wanted_ids and entry_target_id not in wanted_ids:
                        continue
                    if (
                        channel_id is not None
                        and self._audit_channel_id(entry) != channel_id
                    ):
                        continue
                    return entry
            except (discord.Forbidden, discord.HTTPException):
                return None
            if attempt < 2:
                await asyncio.sleep(0.35)
        return None

    async def _audit_details(
        self,
        guild: discord.Guild,
        action: discord.AuditLogAction,
        target_id: int | None = None,
        target_ids: set[int] | None = None,
        channel_id: int | None = None,
    ) -> tuple[discord.User | discord.Member, str | None] | None:
        entry = await self._recent_audit_entry(
            guild,
            (action,),
            target_id=target_id,
            target_ids=target_ids,
            channel_id=channel_id,
            max_age=30,
        )
        if entry is not None and entry.user is not None:
            return entry.user, entry.reason
        return None

    async def _recent_webhook_change(self, channel: discord.abc.GuildChannel) -> (
        tuple[
            discord.AuditLogAction,
            discord.Webhook,
            discord.User | discord.Member,
            str | None,
        ]
        | None
    ):
        """Find the create/delete audit entry that caused on_webhooks_update."""
        me = channel.guild.me
        if me is None or not me.guild_permissions.view_audit_log:
            return None

        actions = {
            discord.AuditLogAction.webhook_create,
            discord.AuditLogAction.webhook_delete,
            discord.AuditLogAction.webhook_update,
        }
        seen: dict[tuple[int, int, str], float] = getattr(
            self, "_logged_webhook_creations", {}
        )
        self._logged_webhook_creations = seen
        now = time.monotonic()
        for key, expires_at in list(seen.items()):
            if expires_at <= now:
                seen.pop(key, None)

        create_candidate: (
            tuple[
                discord.AuditLogAction,
                discord.Webhook,
                discord.User | discord.Member,
                str | None,
                tuple[int, int, str],
            ]
            | None
        ) = None
        for attempt in range(4):
            try:
                async for entry in channel.guild.audit_logs(limit=20):
                    if entry.action not in actions or entry.user is None:
                        continue
                    age = (discord.utils.utcnow() - entry.created_at).total_seconds()
                    if not -2 <= age <= 20:
                        continue

                    target = entry.target
                    webhook_id = getattr(target, "id", None)
                    if webhook_id is None:
                        continue
                    target_channel_id = self._audit_channel_id(entry)
                    if target_channel_id != channel.id:
                        continue

                    key = (channel.guild.id, int(webhook_id), str(entry.action))
                    if key in seen:
                        continue
                    candidate = (
                        entry.action,
                        cast(discord.Webhook, target),
                        entry.user,
                        entry.reason,
                        key,
                    )
                    if entry.action is discord.AuditLogAction.webhook_delete:
                        seen[key] = time.monotonic() + 60
                        return candidate[0], candidate[1], candidate[2], candidate[3]
                    if create_candidate is None:
                        create_candidate = candidate
            except (discord.Forbidden, discord.HTTPException):
                return None
            if attempt == 3 and create_candidate is not None:
                seen[create_candidate[4]] = time.monotonic() + 60
                return (
                    create_candidate[0],
                    create_candidate[1],
                    create_candidate[2],
                    create_candidate[3],
                )
            if attempt < 3:
                await asyncio.sleep(0.5)
        return None

    async def _add_audit_details(
        self,
        embed: discord.Embed,
        guild: discord.Guild,
        action: discord.AuditLogAction,
        target_id: int | None = None,
        target_ids: set[int] | None = None,
        channel_id: int | None = None,
        target: discord.User | discord.Member | None = None,
        actor_label: str = "Changed by",
    ) -> None:
        if target is not None and actor_label == "Moderator":
            embed.add_field(
                name="Target",
                value=f"{target.name}\n{target.mention}",
                inline=True,
            )
        details = await self._audit_details(
            guild, action, target_id, target_ids, channel_id
        )
        if details is None:
            return
        actor, reason = details
        embed.add_field(
            name=actor_label,
            value=(
                f"{actor.name}\n{actor.mention}"
                if actor_label == "Moderator"
                else f"{actor.mention} (`ID: {actor.id}`)"
            ),
            inline=True,
        )
        embed.add_field(
            name="Reason",
            value=_display(reason or "Not provided"),
            inline=False,
        )

    def _set_purge_pending(self, guild_id: int, channel_id: int) -> None:
        now = time.monotonic()
        pending: dict[tuple[int, int], float] = getattr(self, "_pending_purges", {})
        self._pending_purges = pending
        pending[(guild_id, channel_id)] = now + 30

    def _consume_purge_pending(self, guild_id: int, channel_id: int) -> bool:
        pending: dict[tuple[int, int], float] = getattr(self, "_pending_purges", {})
        expires_at = pending.pop((guild_id, channel_id), None)
        return expires_at is not None and expires_at > time.monotonic()

    @staticmethod
    def _bulk_delete_content(messages: Sequence[discord.Message]) -> str | None:
        entries = [
            f"`{message.id}`: {_display(message.content)}"
            for message in messages
            if message.content
        ]
        if not entries:
            return None
        content = "\n".join(entries)
        if len(content) > 1024:
            content = content[:1021].rsplit("\n", 1)[0] + "..."
        return content

    def _bulk_delete_embed(
        self,
        channel: discord.abc.Messageable,
        message_ids: Sequence[int],
        messages: Sequence[discord.Message] = (),
    ) -> discord.Embed:
        channel_name = getattr(channel, "mention", "this channel")
        embed = self._embed(
            "Messages purged",
            f"{len(message_ids):,} messages were bulk deleted in {channel_name}.",
            color=discord.Colour.red(),
        )
        embed.add_field(
            name="Message IDs",
            value=" ".join(f"`{message_id}`" for message_id in message_ids[:20])
            + (" ..." if len(message_ids) > 20 else ""),
            inline=False,
        )
        content = self._bulk_delete_content(messages)
        if content:
            embed.add_field(name="Content", value=content, inline=False)
        return embed

    async def _logger_list(self, ctx: GuildContext) -> None:
        rows = await self.bot.pool.fetch(
            "SELECT event, channel_id FROM guild_log_channels "
            "WHERE guild_id = $1 ORDER BY event",
            ctx.guild.id,
        )
        configured = {
            _canonical_logger_event(str(row["event"])): int(row["channel_id"])
            for row in rows
        }
        description = "\n".join(
            f"**{label}** — <#{configured[event]}>"
            for event, label in LOGGER_EVENTS.items()
            if event in configured
        )
        if not description:
            description = "No event logging channels are configured."

        embed = discord.Embed(
            title=f"Event logger for {ctx.guild.name}",
            description=description,
            color=self.bot.embedcolor,
        )
        embed.set_footer(
            text="Use `fish logger set <event> #channel` to configure one."
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def _configured_logger_channels(self, guild_id: int) -> dict[str, int]:
        rows = await self.bot.pool.fetch(
            "SELECT event, channel_id FROM guild_log_channels WHERE guild_id = $1",
            guild_id,
        )
        return {
            _canonical_logger_event(str(row["event"])): int(row["channel_id"])
            for row in rows
        }

    async def _set_logger_channel(
        self,
        ctx: GuildContext,
        event: str,
        channel: discord.TextChannel,
        *,
        announce: bool = True,
        actor_id: int | None = None,
    ) -> None:
        event = _canonical_logger_event(event)
        if event not in LOGGER_EVENTS:
            available = ", ".join(LOGGER_EVENTS)
            raise commands.BadArgument(f"Unknown event. Choose one of: {available}.")
        if channel.guild.id != ctx.guild.id:
            raise commands.BadArgument("The channel must be in this server.")

        me = ctx.guild.me
        if me is None:
            raise commands.BotMissingPermissions(["send_messages", "embed_links"])
        permissions = channel.permissions_for(me)
        if not permissions.send_messages or not permissions.embed_links:
            raise commands.BotMissingPermissions(["send_messages", "embed_links"])

        actor_id = actor_id or getattr(ctx.author, "id", None)
        async with self._webhook_lock(ctx.guild.id, event):
            existing = await self.bot.pool.fetchrow(
                "SELECT channel_id, webhook_url "
                "FROM guild_log_channels WHERE guild_id = $1 AND event = $2",
                ctx.guild.id,
                event,
            )
            old_webhook_url = existing["webhook_url"] if existing else None
            webhook_url = (
                str(existing["webhook_url"])
                if existing
                and int(existing["channel_id"]) == channel.id
                and existing["webhook_url"]
                else None
            )
            if webhook_url is None:
                webhook = await self._create_logger_webhook(
                    channel, event, created_by=actor_id
                )
                webhook_url = webhook.url

            if old_webhook_url and str(old_webhook_url) != str(webhook_url):
                await self._delete_logger_webhook(old_webhook_url, ctx.guild.id, event)

            await self.bot.pool.execute(
                "INSERT INTO guild_log_channels "
                "(guild_id, event, channel_id, webhook_url) "
                "VALUES ($1, $2, $3, $4) "
                "ON CONFLICT (guild_id, event) DO UPDATE SET "
                "channel_id = EXCLUDED.channel_id, webhook_url = EXCLUDED.webhook_url",
                ctx.guild.id,
                event,
                channel.id,
                webhook_url,
            )
        if announce:
            await ctx.send(
                f"{LOGGER_EVENTS[event]} will now be logged in {channel.mention}.",
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @staticmethod
    def _resolve_logger_channel(
        guild: discord.Guild, value: str
    ) -> discord.TextChannel:
        raw = value.strip()
        mention = re.fullmatch(r"<#(\d+)>", raw)
        channel_id = (
            int(mention.group(1)) if mention else int(raw) if raw.isdigit() else None
        )
        if channel_id is not None:
            channel = guild.get_channel(channel_id)
            if isinstance(channel, discord.TextChannel):
                return channel
            raise commands.BadArgument("That ID is not a text channel in this server.")
        matches = [
            channel
            for channel in guild.text_channels
            if channel.name.casefold() == raw.casefold()
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise commands.BadArgument(
                "More than one text channel has that name; use its ID."
            )
        raise commands.BadArgument("I could not find that text channel in this server.")

    async def _delete_logger_webhook(
        self, webhook_url: object | None, guild_id: int, event: str
    ) -> None:
        if not webhook_url:
            return
        try:
            await discord.Webhook.from_url(
                str(webhook_url), session=self.bot.session
            ).delete()
        except discord.HTTPException:
            self.bot.logger.debug(
                "Could not delete logger webhook for guild %s event %s",
                guild_id,
                event,
            )

    async def _clear_logger_channel(self, ctx: GuildContext, event: str | None) -> None:
        if event is None:
            rows = await self.bot.pool.fetch(
                "SELECT event, webhook_url FROM guild_log_channels WHERE guild_id = $1",
                ctx.guild.id,
            )
            if not rows:
                await ctx.send(
                    "No event logging channels are configured.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            confirmed = await ctx.prompt(
                f"This will disable all {len(rows)} event logger(s) and delete their webhooks. Continue?",
                confirm_label="Yes",
                cancel_label="No",
            )
            if confirmed is None:
                await ctx.send("Logger clearing cancelled.")
                return
            for row in rows:
                row_event = str(row["event"])
                async with self._webhook_lock(ctx.guild.id, row_event):
                    current = await self.bot.pool.fetchrow(
                        "SELECT webhook_url FROM guild_log_channels "
                        "WHERE guild_id = $1 AND event = $2",
                        ctx.guild.id,
                        row_event,
                    )
                    if current is None:
                        continue
                    await self._delete_logger_webhook(
                        current["webhook_url"], ctx.guild.id, row_event
                    )
                    await self.bot.pool.execute(
                        "DELETE FROM guild_log_channels "
                        "WHERE guild_id = $1 AND event = $2",
                        ctx.guild.id,
                        row_event,
                    )
            await ctx.send(
                f"Disabled all event logging and deleted {len(rows)} webhook(s).",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        event = _canonical_logger_event(event)
        if event not in LOGGER_EVENTS:
            available = ", ".join(LOGGER_EVENTS)
            raise commands.BadArgument(f"Unknown event. Choose one of: {available}.")

        async with self._webhook_lock(ctx.guild.id, event):
            row = await self.bot.pool.fetchrow(
                "SELECT webhook_url FROM guild_log_channels "
                "WHERE guild_id = $1 AND event = $2",
                ctx.guild.id,
                event,
            )
            if row is None:
                await ctx.send(
                    f"{LOGGER_EVENTS[event]} logging was not configured.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            await self._delete_logger_webhook(row["webhook_url"], ctx.guild.id, event)
            await self.bot.pool.execute(
                "DELETE FROM guild_log_channels WHERE guild_id = $1 AND event = $2",
                ctx.guild.id,
                event,
            )
        await ctx.send(
            f"Disabled {LOGGER_EVENTS[event].lower()} logging and deleted its webhook.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_group(name="logger", aliases=("log",))
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def logger(self, ctx: GuildContext) -> None:
        """Configure per-server event logging channels."""
        configured = await self._configured_logger_channels(ctx.guild.id)
        await ctx.send(
            view=LoggerPanelView(self, ctx, configured),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @logger.command(name="list")
    async def logger_list(self, ctx: GuildContext) -> None:
        """Show configured event logging channels."""
        await self._logger_list(ctx)

    @logger.command(name="set")
    async def logger_set(
        self,
        ctx: GuildContext,
        event: str,
        channel: discord.TextChannel,
    ) -> None:
        """Set an event logging channel."""
        await self._set_logger_channel(ctx, event, channel)

    @logger.command(name="avatar")
    async def logger_avatar(
        self, ctx: GuildContext, channel: discord.TextChannel
    ) -> None:
        """Log global and server avatar changes."""
        await self._set_logger_channel(ctx, "avatar", channel)

    @logger.command(name="member", aliases=("members",))
    async def logger_member(
        self, ctx: GuildContext, channel: discord.TextChannel
    ) -> None:
        """Log member joins, leaves, name, and tag changes."""
        await self._set_logger_channel(ctx, "member", channel)

    @logger.command(name="activity", aliases=("activities", "games"))
    async def logger_activity(
        self, ctx: GuildContext, channel: discord.TextChannel
    ) -> None:
        """Log member game and activity changes."""
        await self._set_logger_channel(ctx, "activity", channel)

    @logger.command(name="vc", aliases=("voice", "voicechannel"))
    async def logger_voice(
        self, ctx: GuildContext, channel: discord.TextChannel
    ) -> None:
        """Log voice channel joins, leaves, moves, mute, deafen, and disconnects."""
        await self._set_logger_channel(ctx, "voice", channel)

    @logger.command(name="channel", aliases=("channels",))
    async def logger_channel(
        self, ctx: GuildContext, channel: discord.TextChannel
    ) -> None:
        """Log channel, thread, and webhook creation, updates, and deletion."""
        await self._set_logger_channel(ctx, "channel", channel)

    @logger.command(name="role", aliases=("roles",))
    async def logger_role(
        self, ctx: GuildContext, channel: discord.TextChannel
    ) -> None:
        """Log role creation, updates, and deletion."""
        await self._set_logger_channel(ctx, "role", channel)

    @logger.command(name="server")
    async def logger_server(
        self, ctx: GuildContext, channel: discord.TextChannel
    ) -> None:
        """Log server settings, emoji, and sticker changes."""
        await self._set_logger_channel(ctx, "server", channel)

    @logger.command(name="moderation", aliases=("mod", "bans"))
    async def logger_moderation(
        self, ctx: GuildContext, channel: discord.TextChannel
    ) -> None:
        """Log bans, kicks, timeouts, and other moderation actions."""
        await self._set_logger_channel(ctx, "moderation", channel)

    @logger.command(name="message", aliases=("messages",))
    async def logger_message(
        self, ctx: GuildContext, channel: discord.TextChannel
    ) -> None:
        """Log message metadata edits, deletions, pins, and bulk purges."""
        await self._set_logger_channel(ctx, "message", channel)

    @logger.command(name="clear", aliases=("disable", "remove"))
    async def logger_clear(self, ctx: GuildContext, event: str | None = None) -> None:
        """Disable logging for an event."""
        await self._clear_logger_channel(ctx, event)

    async def _emit_logger(
        self,
        guild: discord.Guild,
        event: str,
        embed: discord.Embed,
        *,
        audit_action: discord.AuditLogAction | None = None,
        audit_target_id: int | None = None,
        audit_target_ids: set[int] | None = None,
        audit_channel_id: int | None = None,
        audit_target: discord.User | discord.Member | None = None,
        audit_actor_label: str = "Changed by",
    ) -> None:
        # The operational guild is reserved for private review/log channels.
        # Generic logger events must never leak into that server; video/post
        # review code addresses its channels explicitly and does not call this
        # method.
        if is_operational_guild(guild):
            return
        # The retiring process is read-only during the handoff.  Do not wait
        # for the replacement member to appear in this guild: gateway events
        # can arrive in either order and emitting here would duplicate logs or
        # recreate legacy webhooks while the new process is taking ownership.
        if self._is_legacy_instance():
            return
        # A server administrator can disable all logger writes from the
        # settings panel.  Check the in-memory setting before doing any
        # database or webhook work so a disabled guild remains quiet.
        if not self.bot.db_cache.guild_tracking_enabled(guild.id):
            return
        row = await self.bot.pool.fetchrow(
            "SELECT channel_id, webhook_url "
            "FROM guild_log_channels "
            "WHERE guild_id = $1 AND event = $2",
            guild.id,
            event,
        )
        if row is None:
            return
        channel = guild.get_channel(int(row["channel_id"]))
        if not isinstance(channel, discord.TextChannel):
            return
        if audit_action is not None:
            await self._add_audit_details(
                embed,
                guild,
                audit_action,
                audit_target_id,
                audit_target_ids,
                audit_channel_id,
                audit_target,
                audit_actor_label,
            )

        webhook_url = row["webhook_url"]
        try:
            if not webhook_url:
                async with self._webhook_lock(guild.id, event):
                    # Re-read under the lock so simultaneous events cannot create duplicates.
                    row = await self.bot.pool.fetchrow(
                        "SELECT channel_id, webhook_url "
                        "FROM guild_log_channels WHERE guild_id = $1 AND event = $2",
                        guild.id,
                        event,
                    )
                    if row is None:
                        return
                    locked_channel = guild.get_channel(int(row["channel_id"]))
                    if not isinstance(locked_channel, discord.TextChannel):
                        return
                    channel = locked_channel
                    webhook_url = row["webhook_url"]
                    if not webhook_url:
                        bot_id = self.bot.user.id if self.bot.user else None
                        webhook_url = (
                            await self._create_logger_webhook(
                                channel, event, created_by=bot_id
                            )
                        ).url
                        await self.bot.pool.execute(
                            "UPDATE guild_log_channels SET webhook_url = $1 "
                            "WHERE guild_id = $2 AND event = $3",
                            webhook_url,
                            guild.id,
                            event,
                        )
            webhook = discord.Webhook.from_url(
                str(webhook_url), session=self.bot.session
            )
            view = self._embed_view(embed)
            try:
                await webhook.send(
                    view=view,
                    username="Fishie Logger",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.NotFound:
                webhook_url = await self._replace_missing_logger_webhook(
                    guild, event, webhook_url
                )
                if webhook_url is None:
                    return
                await discord.Webhook.from_url(
                    webhook_url, session=self.bot.session
                ).send(
                    view=view,
                    username="Fishie Logger",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except (discord.HTTPException, commands.BotMissingPermissions):
            self.bot.logger.warning(
                "Could not send %s logger event in guild %s", event, guild.id
            )

    async def _replace_missing_logger_webhook(
        self, guild: discord.Guild, event: str, failed_url: object
    ) -> str | None:
        """Replace a deleted logger webhook without creating race duplicates."""
        async with self._webhook_lock(guild.id, event):
            row = await self.bot.pool.fetchrow(
                "SELECT channel_id, webhook_url FROM guild_log_channels "
                "WHERE guild_id = $1 AND event = $2",
                guild.id,
                event,
            )
            if row is None:
                return None
            current_url = row["webhook_url"]
            if current_url and str(current_url) != str(failed_url):
                return str(current_url)
            channel = guild.get_channel(int(row["channel_id"]))
            if not isinstance(channel, discord.TextChannel):
                return None
            bot_id = self.bot.user.id if self.bot.user else None
            webhook = await self._create_logger_webhook(
                channel, event, created_by=bot_id
            )
            await self.bot.pool.execute(
                "UPDATE guild_log_channels SET webhook_url = $1 "
                "WHERE guild_id = $2 AND event = $3",
                webhook.url,
                guild.id,
                event,
            )
            return webhook.url

    async def _create_logger_webhook(
        self,
        channel: discord.TextChannel,
        event: str,
        *,
        created_by: int | None = None,
    ) -> discord.Webhook:
        me = channel.guild.me
        if me is None:
            raise commands.BotMissingPermissions(["manage_webhooks"])
        permissions = channel.permissions_for(me)
        if not permissions.manage_webhooks:
            raise commands.BotMissingPermissions(["manage_webhooks"])
        try:
            avatar = LOGGER_AVATAR_PATH.read_bytes()
        except OSError:
            avatar = None
            self.bot.logger.warning(
                "Logger webhook avatar is unavailable at %s", LOGGER_AVATAR_PATH
            )
        try:
            creator = f"user {created_by}" if created_by is not None else "Fishie"
            webhook = await channel.create_webhook(
                name=f"Fishie Logger - {event}",
                avatar=avatar,
                reason=f"Create Fishie {event} logger webhook ({creator})",
            )
            self.bot.logger.info(
                "Created %s logger webhook in guild %s channel %s (requested by %s)",
                event,
                channel.guild.id,
                channel.id,
                creator,
            )
            return webhook
        except discord.Forbidden as exc:
            raise commands.BotMissingPermissions(["manage_webhooks"]) from exc

    @staticmethod
    def _embed(
        title: str, description: Optional[str], color: discord.Colour
    ) -> discord.Embed:
        return discord.Embed(
            title=title,
            description=description[:4096] if description else None,
            color=color,
            timestamp=discord.utils.utcnow(),
        )

    @staticmethod
    def _embed_view(embed: discord.Embed) -> discord.ui.LayoutView:
        """Convert a logger event into a Components V2 webhook payload.

        Logger webhooks previously sent legacy embeds. Keeping the event
        builders embed-shaped lets the existing formatting stay stable while
        this conversion makes every event a Components V2 message at the
        transport boundary.
        """
        view_type = type("LoggerEventView", (discord.ui.LayoutView,), {})
        view = view_type(timeout=None)
        children: list[discord.ui.Item] = []
        text_parts: list[str] = []
        if embed.title:
            text_parts.append(f"## {embed.title}")
        if embed.description:
            text_parts.append(embed.description)
        if embed.author and embed.author.name:
            text_parts.append(f"-# {embed.author.name}")
        for field in embed.fields:
            value = f"**{field.name}**\n{field.value}"
            text_parts.append(value)
        text = "\n\n".join(text_parts)
        while len(text) > 4000:
            cut = text.rfind("\n", 0, 4000)
            cut = cut if cut > 0 else 4000
            children.append(discord.ui.TextDisplay(text[:cut]))
            text = text[cut:].lstrip("\n")
        if text:
            children.append(discord.ui.TextDisplay(text))
        if embed.image.url:
            children.append(
                discord.ui.MediaGallery(discord.MediaGalleryItem(embed.image.url))
            )
        elif embed.thumbnail.url:
            children.append(
                discord.ui.MediaGallery(discord.MediaGalleryItem(embed.thumbnail.url))
            )
        if embed.footer.text:
            children.append(discord.ui.TextDisplay(f"-# {embed.footer.text}"))
        elif embed.timestamp is not None:
            children.append(
                discord.ui.TextDisplay(
                    f"-# {discord.utils.format_dt(embed.timestamp, 'R')}"
                )
            )
        if not children:
            children.append(discord.ui.TextDisplay("Logger event"))
        view.add_item(
            discord.ui.Container(
                *children,
                accent_color=embed.colour or discord.Colour.blurple(),
            )
        )
        return view

    @staticmethod
    def _add_item_id(
        embed: discord.Embed,
        item: (
            discord.User
            | discord.Member
            | discord.abc.GuildChannel
            | discord.Thread
            | discord.Role
            | discord.Guild
            | discord.Message
            | discord.Object
        ),
    ) -> None:
        embed.set_footer(text=f"ID: {item.id}")

    @commands.Cog.listener("on_user_update")
    async def logger_user_update(
        self, before: discord.User, after: discord.User
    ) -> None:
        avatar_changed = _asset_key(before.avatar) != _asset_key(after.avatar)
        profile_changed = (
            before.name != after.name or before.display_name != after.display_name
        )
        before_primary_guild = getattr(before, "primary_guild", None)
        after_primary_guild = getattr(after, "primary_guild", None)
        before_tag = getattr(before_primary_guild, "tag", None)
        after_tag = getattr(after_primary_guild, "tag", None)
        tag_changed = before_tag != after_tag
        if not avatar_changed and not profile_changed and not tag_changed:
            return

        for guild in self.bot.guilds:
            if guild.get_member(after.id) is None:
                continue
            if avatar_changed:
                embed = self._embed(
                    "Avatar changed",
                    f"{after.mention} changed their avatar.",
                    color=discord.Colour.blurple(),
                )
                embed.set_author(name=str(after), icon_url=after.display_avatar.url)
                embed.set_image(url=after.display_avatar.url)
                self._add_item_id(embed, after)
                await self._emit_logger(guild, "avatar", embed)
            if profile_changed:
                embed = self._embed(
                    "Member name changed",
                    f"{after.mention} updated their Discord profile.",
                    color=discord.Colour.orange(),
                )
                embed.add_field(
                    name="Before",
                    value=f"{_display(before.name)} / {_display(before.display_name)}",
                )
                embed.add_field(
                    name="After",
                    value=f"{_display(after.name)} / {_display(after.display_name)}",
                )
                self._add_item_id(embed, after)
                await self._emit_logger(guild, "member", embed)
            if tag_changed:
                embed = self._embed(
                    "Member tag changed",
                    f"{after.mention} changed their server tag.",
                    color=discord.Colour.orange(),
                )
                embed.add_field(name="Before", value=_display(before_tag))
                embed.add_field(name="After", value=_display(after_tag))
                self._add_item_id(embed, after)
                await self._emit_logger(guild, "member", embed)

    @commands.Cog.listener("on_member_update")
    async def logger_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        if _asset_key(before.guild_avatar) != _asset_key(after.guild_avatar):
            embed = self._embed(
                "Server avatar changed",
                f"{after.mention} changed their server avatar.",
                color=discord.Colour.blurple(),
            )
            embed.set_author(name=str(after), icon_url=after.display_avatar.url)
            embed.set_image(url=after.display_avatar.url)
            self._add_item_id(embed, after)
            await self._emit_logger(after.guild, "avatar", embed)

        if before.nick != after.nick:
            embed = self._embed(
                "Nickname changed",
                f"{after.mention} changed their nickname.",
                color=discord.Colour.orange(),
            )
            embed.add_field(name="Before", value=_display(before.nick))
            embed.add_field(name="After", value=_display(after.nick))
            self._add_item_id(embed, after)
            await self._emit_logger(
                after.guild,
                "member",
                embed,
                audit_action=discord.AuditLogAction.member_update,
                audit_target_id=after.id,
            )

        before_role_ids = {role.id for role in before.roles}
        after_role_ids = {role.id for role in after.roles}
        added_roles = [
            role
            for role in after.roles
            if role.id not in before_role_ids and not role.is_default()
        ]
        removed_roles = [
            role
            for role in before.roles
            if role.id not in after_role_ids and not role.is_default()
        ]
        if added_roles or removed_roles:
            embed = self._embed(
                "Member roles changed",
                f"{after.mention}'s roles were updated.",
                color=discord.Colour.orange(),
            )
            if added_roles:
                embed.add_field(
                    name="Roles added",
                    value="\n".join(
                        f"{role.mention} (`{role.id}`)" for role in added_roles
                    )[:1024],
                    inline=False,
                )
            if removed_roles:
                embed.add_field(
                    name="Roles removed",
                    value="\n".join(
                        f"{role.mention} (`{role.id}`)" for role in removed_roles
                    )[:1024],
                    inline=False,
                )
            self._add_item_id(embed, after)
            await self._emit_logger(
                after.guild,
                "member",
                embed,
                audit_action=discord.AuditLogAction.member_role_update,
                audit_target_id=after.id,
            )

        if before.timed_out_until != after.timed_out_until:
            timed_out = after.timed_out_until is not None
            embed = self._embed(
                "Member timed out" if timed_out else "Member timeout removed",
                (
                    f"{after.mention} was timed out."
                    if timed_out
                    else f"The timeout was removed from {after.mention}."
                ),
                color=discord.Colour.red() if timed_out else discord.Colour.green(),
            )
            if after.timed_out_until is not None:
                embed.add_field(
                    name="Until",
                    value=discord.utils.format_dt(after.timed_out_until, "R"),
                    inline=False,
                )
            self._add_item_id(embed, after)
            await self._emit_logger(
                after.guild,
                "moderation",
                embed,
                audit_action=discord.AuditLogAction.automod_timeout_member,
                audit_target_id=after.id,
                audit_target=after,
                audit_actor_label="Moderator",
            )

    @commands.Cog.listener("on_presence_update")
    async def logger_activity_update(
        self,
        before: discord.Member,
        after: discord.Member,
    ) -> None:
        if self.bot.db_cache.user_tracking_opted_out(after.id, "activity"):
            return
        before_activities = loggable_activities(before.activities)
        after_activities = loggable_activities(after.activities)
        before_identity = sorted(
            map(activity_identity, before_activities),
            key=_activity_identity_sort_key,
        )
        after_identity = sorted(
            map(activity_identity, after_activities),
            key=_activity_identity_sort_key,
        )
        if before_identity == after_identity:
            return

        embed = self._embed(
            "Member activity changed",
            f"{after.mention} updated their current activity.",
            color=discord.Colour.blurple(),
        )
        before_text = "\n\n".join(map(activity_summary, before_activities)) or "None"
        after_text = "\n\n".join(map(activity_summary, after_activities)) or "None"
        embed.add_field(
            name="Before",
            value=discord.utils.escape_mentions(before_text[:1024]),
            inline=False,
        )
        embed.add_field(
            name="After",
            value=discord.utils.escape_mentions(after_text[:1024]),
            inline=False,
        )
        if after_activities:
            image_url = activity_image_url(after_activities[0])
            if image_url:
                embed.set_thumbnail(url=image_url)
        embed.set_author(name=str(after), icon_url=after.display_avatar.url)
        self._add_item_id(embed, after)
        await self._emit_logger(after.guild, "activity", embed)

    @commands.Cog.listener("on_voice_state_update")
    async def logger_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        changes: list[str] = []
        channel_changed = before.channel != after.channel
        if channel_changed:
            before_channel = (
                before.channel.mention
                if before.channel is not None
                else "Not connected"
            )
            after_channel = (
                after.channel.mention if after.channel is not None else "Not connected"
            )
            if before.channel is None:
                changes.append(f"Joined {after_channel}")
            elif after.channel is None:
                changes.append(f"Left {before_channel}")
            else:
                changes.append(f"Moved from {before_channel} to {after_channel}")

        voice_flags = (
            ("mute", "Server mute"),
            ("deaf", "Server deafen"),
            ("self_mute", "Self mute"),
            ("self_deaf", "Self deafen"),
            ("self_stream", "Stream"),
            ("self_video", "Camera"),
            ("suppress", "Stage suppression"),
        )
        server_state_changed = False
        for attribute, label in voice_flags:
            previous = getattr(before, attribute, False)
            current = getattr(after, attribute, False)
            if previous == current:
                continue
            state = "enabled" if current else "disabled"
            changes.append(f"{label} {state}")
            if attribute in {"mute", "deaf", "suppress"}:
                server_state_changed = True

        if not changes:
            return

        embed = self._embed(
            "Voice channel updated",
            f"{member.mention}'s voice state was updated.",
            color=(
                discord.Colour.green()
                if before.channel is None and after.channel is not None
                else (
                    discord.Colour.red()
                    if after.channel is None and before.channel is not None
                    else discord.Colour.orange()
                )
            ),
        )
        audit_action: discord.AuditLogAction | None = None
        if channel_changed:
            audit_action = (
                discord.AuditLogAction.member_disconnect
                if after.channel is None
                else (
                    discord.AuditLogAction.member_move
                    if before.channel is not None and after.channel is not None
                    else None
                )
            )
        elif server_state_changed:
            audit_action = discord.AuditLogAction.member_update

        if audit_action is None:
            embed.add_field(
                name="Member",
                value=f"{_display(member.name)}\n{member.mention}",
                inline=True,
            )
        embed.add_field(
            name="Changes",
            value="\n".join(changes)[:1024],
            inline=False,
        )
        self._add_item_id(embed, member)
        await self._emit_logger(
            member.guild,
            "voice",
            embed,
            audit_action=audit_action,
            audit_target_id=member.id if audit_action is not None else None,
            audit_target=member if audit_action is not None else None,
            audit_actor_label="Moderator" if audit_action is not None else "Changed by",
        )

    @commands.Cog.listener("on_member_join")
    async def logger_member_join(self, member: discord.Member) -> None:
        if member.bot:
            embed = self._embed(
                "Bot added",
                f"{member.mention} was added to the server.",
                color=discord.Colour.green(),
            )
            embed.set_author(name=str(member), icon_url=member.display_avatar.url)
            embed.add_field(
                name="Bot",
                value=f"{_display(member.name)}\n{member.mention}",
                inline=True,
            )
            self._add_item_id(embed, member)
            await self._emit_logger(
                member.guild,
                "server",
                embed,
                audit_action=discord.AuditLogAction.bot_add,
                audit_target_id=member.id,
                audit_actor_label="Added by",
            )
            return

        embed = self._embed(
            "Member joined",
            f"{member.mention} joined the server.",
            color=discord.Colour.green(),
        )
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        self._add_item_id(embed, member)
        await self._emit_logger(member.guild, "member", embed)

    @commands.Cog.listener("on_member_remove")
    async def logger_member_remove(self, member: discord.Member) -> None:
        if await self._log_recent_prune(member.guild):
            return
        was_kicked = (
            await self._audit_details(
                member.guild, discord.AuditLogAction.kick, member.id
            )
        ) is not None
        title = "Member kicked" if was_kicked else "Member left"
        description = (
            None
            if was_kicked
            else f"{member.mention} left or was removed from the server."
        )
        embed = self._embed(
            title,
            description,
            color=discord.Colour.red(),
        )
        self._add_item_id(embed, member)
        if was_kicked:
            await self._emit_logger(
                member.guild,
                "moderation",
                embed,
                audit_action=discord.AuditLogAction.kick,
                audit_target_id=member.id,
                audit_target=member,
                audit_actor_label="Moderator",
            )
        else:
            await self._emit_logger(member.guild, "member", embed)

    @commands.Cog.listener("on_guild_channel_create")
    async def logger_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        embed = self._embed(
            "Channel created",
            f"{channel.mention} (`{channel.name}`) was created.",
            color=discord.Colour.green(),
        )
        self._add_item_id(embed, channel)
        await self._emit_logger(
            channel.guild,
            "channel",
            embed,
            audit_action=discord.AuditLogAction.channel_create,
            audit_target_id=channel.id,
        )

    @commands.Cog.listener("on_guild_channel_delete")
    async def logger_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        embed = self._embed(
            "Channel deleted",
            f"`{channel.name}` was deleted.",
            color=discord.Colour.red(),
        )
        self._add_item_id(embed, channel)
        await self._emit_logger(
            channel.guild,
            "channel",
            embed,
            audit_action=discord.AuditLogAction.channel_delete,
            audit_target_id=channel.id,
        )

    @staticmethod
    def _thread_summary(thread: discord.Thread) -> str:
        parent = getattr(thread, "parent", None)
        return (
            f"Parent: {getattr(parent, 'mention', 'Unknown')}\n"
            f"Type: {_display(getattr(thread, 'type', None))}\n"
            f"Archived: {'Yes' if getattr(thread, 'archived', False) else 'No'}\n"
            f"Locked: {'Yes' if getattr(thread, 'locked', False) else 'No'}\n"
            f"Auto archive: {_display(getattr(thread, 'auto_archive_duration', None))} minutes\n"
            f"Slowmode: {_display(getattr(thread, 'slowmode_delay', None))} seconds"
        )

    @commands.Cog.listener("on_thread_create")
    async def logger_thread_create(self, thread: discord.Thread) -> None:
        embed = self._embed(
            "Thread created",
            f"{thread.mention} was created.",
            color=discord.Colour.green(),
        )
        embed.add_field(
            name="Details", value=self._thread_summary(thread), inline=False
        )
        self._add_item_id(embed, thread)
        await self._emit_logger(
            thread.guild,
            "channel",
            embed,
            audit_action=discord.AuditLogAction.thread_create,
            audit_target_id=thread.id,
        )

    @commands.Cog.listener("on_thread_delete")
    async def logger_thread_delete(self, thread: discord.Thread) -> None:
        embed = self._embed(
            "Thread deleted",
            f"`{_display(thread.name)}` was deleted.",
            color=discord.Colour.red(),
        )
        parent = getattr(thread, "parent", None)
        embed.add_field(
            name="Parent",
            value=getattr(parent, "mention", "Unknown"),
            inline=False,
        )
        self._add_item_id(embed, thread)
        await self._emit_logger(
            thread.guild,
            "channel",
            embed,
            audit_action=discord.AuditLogAction.thread_delete,
            audit_target_id=thread.id,
        )

    @commands.Cog.listener("on_thread_update")
    async def logger_thread_update(
        self, before: discord.Thread, after: discord.Thread
    ) -> None:
        attributes = (
            ("name", "Name"),
            ("archived", "Archived"),
            ("locked", "Locked"),
            ("auto_archive_duration", "Auto archive (minutes)"),
            ("slowmode_delay", "Slowmode (seconds)"),
            ("invitable", "Invitable"),
            ("applied_tags", "Applied tags"),
        )
        changes: list[tuple[str, object, object]] = []
        for attribute, label in attributes:
            old = getattr(before, attribute, None)
            new = getattr(after, attribute, None)
            if old != new:
                changes.append((label, old, new))
        before_parent = getattr(getattr(before, "parent", None), "id", None)
        after_parent = getattr(getattr(after, "parent", None), "id", None)
        if before_parent != after_parent:
            changes.append(("Parent", before_parent, after_parent))
        if not changes:
            return
        embed = self._embed(
            "Thread updated",
            f"{after.mention} was updated.",
            color=discord.Colour.orange(),
        )
        embed.add_field(
            name="Changes",
            value="\n".join(
                f"**{label}:** {_display(old)} -> {_display(new)}"
                for label, old, new in changes
            )[:1024],
            inline=False,
        )
        self._add_item_id(embed, after)
        await self._emit_logger(
            after.guild,
            "channel",
            embed,
            audit_action=discord.AuditLogAction.thread_update,
            audit_target_id=after.id,
        )

    @commands.Cog.listener("on_guild_channel_update")
    async def logger_channel_update(
        self,
        before: discord.abc.GuildChannel,
        after: discord.abc.GuildChannel,
    ) -> None:
        before_category = before.category.id if before.category else None
        after_category = after.category.id if after.category else None
        metadata_attributes = (
            ("name", "Name"),
            ("topic", "Topic"),
            ("nsfw", "NSFW"),
            ("slowmode_delay", "Slowmode (seconds)"),
            ("bitrate", "Bitrate"),
            ("user_limit", "User limit"),
            ("rtc_region", "RTC region"),
            ("default_auto_archive_duration", "Default auto archive (minutes)"),
            ("default_thread_slowmode_delay", "Default thread slowmode (seconds)"),
            ("default_sort_order", "Default sort order"),
            ("default_forum_layout", "Default forum layout"),
            ("available_tags", "Available tags"),
            ("default_reaction_emoji", "Default reaction emoji"),
        )
        metadata_changes = [
            (
                label,
                getattr(before, attribute, None),
                getattr(after, attribute, None),
            )
            for attribute, label in metadata_attributes
            if getattr(before, attribute, None) != getattr(after, attribute, None)
        ]
        if before_category != after_category:
            metadata_changes.append(("Category", before.category, after.category))
        metadata_changed = bool(metadata_changes)
        before_position = getattr(before, "position", None)
        after_position = getattr(after, "position", None)
        position_changed = before_position != after_position
        permission_changes, overwrite_action = _channel_permission_changes(
            before,
            after,
        )
        if not metadata_changed and not position_changed and not permission_changes:
            return
        if position_changed and not metadata_changed and not permission_changes:
            self._queue_channel_position_update(before, after)
            return
        embed = self._embed(
            "Channel updated",
            f"{after.mention} was updated.",
            color=discord.Colour.orange(),
        )
        if metadata_changed:
            embed.add_field(
                name="Before",
                value="\n".join(
                    f"**{label}:** {_display(old)}"
                    for label, old, _ in metadata_changes
                )[:1024],
            )
            embed.add_field(
                name="After",
                value="\n".join(
                    f"**{label}:** {_display(new)}"
                    for label, _, new in metadata_changes
                )[:1024],
            )
        if position_changed:
            embed.add_field(
                name="Position",
                value=(
                    f"Before: {_display(before_position)}\n"
                    f"After: {_display(after_position)}"
                ),
                inline=False,
            )
        if permission_changes:
            permission_text = "\n\n".join(permission_changes)
            if len(permission_text) > 1024:
                permission_text = permission_text[:1021] + "..."
            embed.add_field(
                name="Permission changes",
                value=permission_text,
                inline=False,
            )
        self._add_item_id(embed, after)
        await self._emit_logger(
            after.guild,
            "channel",
            embed,
            audit_action=(
                overwrite_action
                if permission_changes
                else discord.AuditLogAction.channel_update
            ),
            audit_target_id=after.id,
        )

    @commands.Cog.listener("on_webhooks_update")
    async def logger_webhooks_update(self, channel: discord.abc.GuildChannel) -> None:
        """Log webhook creations, updates, and deletions in channel logs."""
        if channel.guild is None:
            return
        details = await self._recent_webhook_change(channel)
        if details is None:
            return
        audit_action, webhook, actor, reason = details
        deleted = audit_action is discord.AuditLogAction.webhook_delete
        updated = audit_action is discord.AuditLogAction.webhook_update
        action = "deleted" if deleted else "updated" if updated else "created"
        embed = self._embed(
            f"Webhook {action}",
            f"A webhook was {action} in {channel.mention}.",
            color=(
                discord.Colour.red()
                if deleted
                else discord.Colour.orange() if updated else discord.Colour.green()
            ),
        )
        embed.add_field(
            name="Webhook",
            value=f"{_display(getattr(webhook, 'name', None))} \n`{webhook.id}`",
            inline=True,
        )
        embed.add_field(
            name="Channel",
            value=f"{channel.mention} \n`{channel.id}`",
            inline=True,
        )
        embed.add_field(
            name="Deleted by" if deleted else "Updated by" if updated else "Created by",
            value=f"{actor.mention} \n`{actor.id}`",
            inline=True,
        )
        embed.add_field(
            name="Reason", value=_display(reason or "Not provided"), inline=False
        )
        self._add_item_id(embed, discord.Object(webhook.id))
        await self._emit_logger(channel.guild, "channel", embed)

    @commands.Cog.listener("on_guild_role_create")
    async def logger_role_create(self, role: discord.Role) -> None:
        embed = self._embed(
            "Role created", f"{role.mention} was created.", color=discord.Colour.green()
        )
        self._add_item_id(embed, role)
        await self._emit_logger(
            role.guild,
            "role",
            embed,
            audit_action=discord.AuditLogAction.role_create,
            audit_target_id=role.id,
        )

    @commands.Cog.listener("on_guild_role_delete")
    async def logger_role_delete(self, role: discord.Role) -> None:
        embed = self._embed(
            "Role deleted", f"`{role.name}` was deleted.", color=discord.Colour.red()
        )
        self._add_item_id(embed, role)
        await self._emit_logger(
            role.guild,
            "role",
            embed,
            audit_action=discord.AuditLogAction.role_delete,
            audit_target_id=role.id,
        )

    @commands.Cog.listener("on_guild_role_update")
    async def logger_role_update(
        self, before: discord.Role, after: discord.Role
    ) -> None:
        permission_changes = _role_permission_changes(
            before.permissions,
            after.permissions,
        )
        color_changed = before.colour != after.colour
        position_changed = before.position != after.position
        hoist_changed = before.hoist != after.hoist
        mentionable_changed = before.mentionable != after.mentionable
        before_icon = _asset_key(getattr(before, "icon", None))
        after_icon = _asset_key(getattr(after, "icon", None))
        icon_changed = before_icon != after_icon
        before_unicode_emoji = getattr(before, "unicode_emoji", None)
        after_unicode_emoji = getattr(after, "unicode_emoji", None)
        unicode_emoji_changed = before_unicode_emoji != after_unicode_emoji
        if (
            before.name == after.name
            and not permission_changes
            and not color_changed
            and not position_changed
            and not hoist_changed
            and not mentionable_changed
            and not icon_changed
            and not unicode_emoji_changed
        ):
            return
        if position_changed and not (
            before.name != after.name
            or permission_changes
            or color_changed
            or hoist_changed
            or mentionable_changed
            or icon_changed
            or unicode_emoji_changed
        ):
            # Reordering one role causes Discord to dispatch position-only
            # updates for every role shifted around it.  Batch those updates
            # and emit only the first changed role after the reorder settles.
            self._queue_role_position_update(before, after)
            return
        embed = self._embed(
            "Role updated",
            f"{after.mention} was updated.",
            color=discord.Colour.orange(),
        )
        if before.name != after.name:
            embed.add_field(name="Before", value=_display(before.name))
            embed.add_field(name="After", value=_display(after.name))
        if permission_changes:
            permission_text = "\n".join(permission_changes)
            if len(permission_text) > 1024:
                permission_text = permission_text[:1021] + "..."
            embed.add_field(
                name="Permission changes",
                value=permission_text,
                inline=False,
            )
        if color_changed:
            embed.add_field(
                name="Color",
                value=(
                    f"Before: {_display(before.colour)}\n"
                    f"After: {_display(after.colour)}"
                ),
                inline=False,
            )
        if position_changed:
            embed.add_field(
                name="Position",
                value=(
                    f"Before: {_display(before.position)}\n"
                    f"After: {_display(after.position)}"
                ),
                inline=False,
            )
        if hoist_changed:
            embed.add_field(
                name="Hoisted",
                value=f"Before: {'Yes' if before.hoist else 'No'}\n"
                f"After: {'Yes' if after.hoist else 'No'}",
                inline=False,
            )
        if mentionable_changed:
            embed.add_field(
                name="Mentionable",
                value=f"Before: {'Yes' if before.mentionable else 'No'}\n"
                f"After: {'Yes' if after.mentionable else 'No'}",
                inline=False,
            )
        if icon_changed:
            embed.add_field(
                name="Role icon",
                value=f"Before: {_display(before_icon)}\nAfter: {_display(after_icon)}",
                inline=False,
            )
            role_icon = getattr(after, "icon", None)
            if role_icon is not None:
                embed.set_thumbnail(url=role_icon.url)
        if unicode_emoji_changed:
            embed.add_field(
                name="Unicode emoji",
                value=(
                    f"Before: {_display(before_unicode_emoji)}\n"
                    f"After: {_display(after_unicode_emoji)}"
                ),
                inline=False,
            )
        self._add_item_id(embed, after)
        await self._emit_logger(
            after.guild,
            "role",
            embed,
            audit_action=discord.AuditLogAction.role_update,
            audit_target_id=after.id,
        )

    @commands.Cog.listener("on_guild_update")
    async def logger_guild_update(
        self, before: discord.Guild, after: discord.Guild
    ) -> None:
        asset_attributes = (
            ("icon", "Icon"),
            ("banner", "Banner"),
            ("splash", "Splash"),
            ("discovery_splash", "Discovery splash"),
        )
        asset_changes = [
            (
                label,
                _asset_key(getattr(before, attribute, None)),
                _asset_key(getattr(after, attribute, None)),
            )
            for attribute, label in asset_attributes
            if _asset_key(getattr(before, attribute, None))
            != _asset_key(getattr(after, attribute, None))
        ]
        scalar_attributes = (
            ("name", "Name"),
            ("description", "Description"),
            ("verification_level", "Verification level"),
            ("explicit_content_filter", "Explicit content filter"),
            ("default_notifications", "Default notifications"),
            ("afk_timeout", "AFK timeout"),
            ("system_channel_flags", "System channel flags"),
            ("preferred_locale", "Preferred locale"),
        )
        scalar_changes = [
            (label, getattr(before, attribute, None), getattr(after, attribute, None))
            for attribute, label in scalar_attributes
            if getattr(before, attribute, None) != getattr(after, attribute, None)
        ]
        channel_attributes = (
            ("afk_channel", "AFK channel"),
            ("rules_channel", "Rules channel"),
            ("system_channel", "System channel"),
            ("public_updates_channel", "Public updates channel"),
            ("safety_alerts_channel", "Safety alerts channel"),
        )
        channel_changes = [
            (label, getattr(before, attribute, None), getattr(after, attribute, None))
            for attribute, label in channel_attributes
            if getattr(before, attribute, None) != getattr(after, attribute, None)
        ]
        if not asset_changes and not scalar_changes and not channel_changes:
            return
        embed = self._embed(
            "Server updated",
            None,
            color=discord.Colour.orange(),
        )
        if scalar_changes:
            embed.add_field(
                name="Before",
                value="\n".join(
                    f"**{label}:** {_display(old)}" for label, old, _ in scalar_changes
                )[:1024],
            )
            embed.add_field(
                name="After",
                value="\n".join(
                    f"**{label}:** {_display(new)}" for label, _, new in scalar_changes
                )[:1024],
            )
        if channel_changes:
            embed.add_field(
                name="Channel changes",
                value="\n".join(
                    f"**{label}:** {_display(old)} -> {_display(new)}"
                    for label, old, new in channel_changes
                )[:1024],
                inline=False,
            )
        if asset_changes:
            embed.add_field(
                name="Asset changes",
                value="\n".join(
                    f"**{label}:** {_display(old)} -> {_display(new)}"
                    for label, old, new in asset_changes
                )[:1024],
                inline=False,
            )
        if after.icon:
            embed.set_thumbnail(url=after.icon.url)
        if after.banner:
            embed.set_image(url=after.banner.url)
        self._add_item_id(embed, after)
        await self._emit_logger(
            after,
            "server",
            embed,
            audit_action=discord.AuditLogAction.guild_update,
            audit_target_id=after.id,
        )

    @staticmethod
    def _emoji_summary(emoji: discord.Emoji) -> str:
        roles = ", ".join(role.name for role in emoji.roles) or "Everyone"
        return (
            f"Name: {_display(emoji.name)}\n"
            f"Roles: {_display(roles)}\n"
            f"Animated: {'Yes' if emoji.animated else 'No'}"
        )

    @commands.Cog.listener("on_guild_emojis_update")
    async def logger_guild_emojis_update(
        self,
        guild: discord.Guild,
        before: Sequence[discord.Emoji],
        after: Sequence[discord.Emoji],
    ) -> None:
        before_by_id = {emoji.id: emoji for emoji in before}
        after_by_id = {emoji.id: emoji for emoji in after}

        for emoji_id in after_by_id.keys() - before_by_id.keys():
            emoji = after_by_id[emoji_id]
            embed = self._embed(
                "Emoji created",
                f"{emoji} `{_display(emoji.name)}` was created.",
                color=discord.Colour.green(),
            )
            embed.set_thumbnail(url=emoji.url)
            self._add_item_id(embed, discord.Object(emoji.id))
            await self._emit_logger(
                guild,
                "server",
                embed,
                audit_action=discord.AuditLogAction.emoji_create,
                audit_target_id=emoji.id,
            )

        for emoji_id in before_by_id.keys() - after_by_id.keys():
            emoji = before_by_id[emoji_id]
            embed = self._embed(
                "Emoji deleted",
                f"`{_display(emoji.name)}` was deleted.",
                color=discord.Colour.red(),
            )
            embed.set_thumbnail(url=emoji.url)
            self._add_item_id(embed, discord.Object(emoji.id))
            await self._emit_logger(
                guild,
                "server",
                embed,
                audit_action=discord.AuditLogAction.emoji_delete,
                audit_target_id=emoji.id,
            )

        for emoji_id in before_by_id.keys() & after_by_id.keys():
            old_emoji = before_by_id[emoji_id]
            emoji = after_by_id[emoji_id]
            before_roles = tuple(role.id for role in old_emoji.roles)
            after_roles = tuple(role.id for role in emoji.roles)
            if (
                old_emoji.name == emoji.name
                and old_emoji.animated == emoji.animated
                and before_roles == after_roles
            ):
                continue
            embed = self._embed(
                "Emoji updated",
                f"{emoji} was updated.",
                color=discord.Colour.orange(),
            )
            embed.add_field(
                name="Before", value=self._emoji_summary(old_emoji), inline=True
            )
            embed.add_field(name="After", value=self._emoji_summary(emoji), inline=True)
            embed.set_thumbnail(url=emoji.url)
            self._add_item_id(embed, discord.Object(emoji.id))
            await self._emit_logger(
                guild,
                "server",
                embed,
                audit_action=discord.AuditLogAction.emoji_update,
                audit_target_id=emoji.id,
            )

    @staticmethod
    def _sticker_summary(sticker: discord.GuildSticker) -> str:
        return (
            f"Name: {_display(sticker.name)}\n"
            f"Description: {_display(sticker.description)}\n"
            f"Related emoji: {_display(sticker.emoji)}"
        )

    @commands.Cog.listener("on_guild_stickers_update")
    async def logger_guild_stickers_update(
        self,
        guild: discord.Guild,
        before: Sequence[discord.GuildSticker],
        after: Sequence[discord.GuildSticker],
    ) -> None:
        before_by_id = {sticker.id: sticker for sticker in before}
        after_by_id = {sticker.id: sticker for sticker in after}

        for sticker_id in after_by_id.keys() - before_by_id.keys():
            sticker = after_by_id[sticker_id]
            embed = self._embed(
                "Sticker created",
                f"`{_display(sticker.name)}` was created.",
                color=discord.Colour.green(),
            )
            embed.set_thumbnail(url=sticker.url)
            self._add_item_id(embed, discord.Object(sticker.id))
            await self._emit_logger(
                guild,
                "server",
                embed,
                audit_action=discord.AuditLogAction.sticker_create,
                audit_target_id=sticker.id,
            )

        for sticker_id in before_by_id.keys() - after_by_id.keys():
            sticker = before_by_id[sticker_id]
            embed = self._embed(
                "Sticker deleted",
                f"`{_display(sticker.name)}` was deleted.",
                color=discord.Colour.red(),
            )
            embed.set_thumbnail(url=sticker.url)
            self._add_item_id(embed, discord.Object(sticker.id))
            await self._emit_logger(
                guild,
                "server",
                embed,
                audit_action=discord.AuditLogAction.sticker_delete,
                audit_target_id=sticker.id,
            )

        for sticker_id in before_by_id.keys() & after_by_id.keys():
            old_sticker = before_by_id[sticker_id]
            sticker = after_by_id[sticker_id]
            if (
                old_sticker.name == sticker.name
                and old_sticker.description == sticker.description
                and old_sticker.emoji == sticker.emoji
            ):
                continue
            embed = self._embed(
                "Sticker updated",
                f"`{_display(sticker.name)}` was updated.",
                color=discord.Colour.orange(),
            )
            embed.add_field(
                name="Before", value=self._sticker_summary(old_sticker), inline=True
            )
            embed.add_field(
                name="After", value=self._sticker_summary(sticker), inline=True
            )
            embed.set_thumbnail(url=sticker.url)
            self._add_item_id(embed, discord.Object(sticker.id))
            await self._emit_logger(
                guild,
                "server",
                embed,
                audit_action=discord.AuditLogAction.sticker_update,
                audit_target_id=sticker.id,
            )

    @commands.Cog.listener("on_message_edit")
    async def logger_message_edit(
        self, before: discord.Message, after: discord.Message
    ) -> None:
        if before.guild is None:
            return
        content_changed = before.content != after.content
        attachments_changed = tuple(
            _attachment_signature(item) for item in before.attachments
        ) != tuple(_attachment_signature(item) for item in after.attachments)
        embeds_changed = tuple(
            _embed_signature(item) for item in before.embeds
        ) != tuple(_embed_signature(item) for item in after.embeds)
        stickers_changed = tuple(
            _message_sticker_signature(item) for item in before.stickers
        ) != tuple(_message_sticker_signature(item) for item in after.stickers)
        metadata_before = (
            getattr(before.flags, "value", None),
            before.pinned,
            before.tts,
            repr(getattr(before, "components", None)),
            repr(getattr(before, "poll", None)),
        )
        metadata_after = (
            getattr(after.flags, "value", None),
            after.pinned,
            after.tts,
            repr(getattr(after, "components", None)),
            repr(getattr(after, "poll", None)),
        )
        metadata_changed = metadata_before != metadata_after
        if not any(
            (
                content_changed,
                attachments_changed,
                embeds_changed,
                stickers_changed,
                metadata_changed,
            )
        ):
            return
        channel_name = getattr(after.channel, "mention", "this channel")
        embed = self._embed(
            "Message edited",
            f"A message in {channel_name} was edited.",
            color=discord.Colour.orange(),
        )
        embed.add_field(
            name="Author", value=f"{after.author.mention} (`ID: {after.author.id}`)"
        )
        if content_changed:
            embed.add_field(
                name="Before", value=_display(before.content) or "None", inline=False
            )
            embed.add_field(
                name="After", value=_display(after.content) or "None", inline=False
            )
        if attachments_changed:
            embed.add_field(
                name="Attachments before",
                value=_attachment_lines(before.attachments),
                inline=True,
            )
            embed.add_field(
                name="Attachments after",
                value=_attachment_lines(after.attachments),
                inline=True,
            )
        if embeds_changed:
            embed.add_field(
                name="Embeds",
                value=f"Before: {len(before.embeds)}\nAfter: {len(after.embeds)}",
                inline=True,
            )
        if stickers_changed:
            embed.add_field(
                name="Stickers",
                value=(
                    f"Before: {len(before.stickers)}\n" f"After: {len(after.stickers)}"
                ),
                inline=True,
            )
        if metadata_changed:
            metadata_changes = []
            for label, old, new in (
                ("Flags", metadata_before[0], metadata_after[0]),
                ("Pinned", metadata_before[1], metadata_after[1]),
                ("TTS", metadata_before[2], metadata_after[2]),
                ("Components", metadata_before[3], metadata_after[3]),
                ("Poll", metadata_before[4], metadata_after[4]),
            ):
                if old != new:
                    metadata_changes.append(
                        f"**{label}:** {_display(old)} -> {_display(new)}"
                    )
            if metadata_changes:
                embed.add_field(
                    name="Metadata changes",
                    value="\n".join(metadata_changes)[:1024],
                    inline=False,
                )
        self._add_item_id(embed, after)
        await self._emit_logger(before.guild, "message", embed)

    @commands.Cog.listener("on_raw_message_delete")
    async def logger_message_delete(
        self, payload: discord.RawMessageDeleteEvent
    ) -> None:
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        channel = guild.get_channel(payload.channel_id)
        channel_name = getattr(channel, "mention", f"channel `{payload.channel_id}`")
        cached = payload.cached_message
        if cached is None:
            description = f"A message in {channel_name} was deleted."
        else:
            description = f"A message in {channel_name} was deleted."
        embed = self._embed("Message deleted", description, color=discord.Colour.red())
        if cached is not None:
            embed.add_field(
                name="Author",
                value=f"{cached.author.mention} (`ID: {cached.author.id}`)",
            )
            if cached.content:
                embed.add_field(
                    name="Content", value=_display(cached.content), inline=False
                )
            if cached.attachments:
                embed.add_field(
                    name="Attachments",
                    value=_attachment_lines(cached.attachments),
                    inline=False,
                )
        self._add_item_id(embed, discord.Object(payload.message_id))
        await self._emit_logger(
            guild,
            "message",
            embed,
            audit_action=discord.AuditLogAction.message_delete,
            audit_target_id=cached.author.id if cached is not None else None,
            audit_channel_id=payload.channel_id,
        )

    async def _recent_pin_change(self, channel: discord.abc.GuildChannel) -> (
        tuple[
            discord.AuditLogAction,
            int | None,
            discord.User | discord.Member | None,
            str | None,
        ]
        | None
    ):
        entry = await self._recent_audit_entry(
            channel.guild,
            (discord.AuditLogAction.message_pin, discord.AuditLogAction.message_unpin),
            channel_id=channel.id,
            max_age=30,
        )
        if entry is None:
            return None
        message_id = getattr(getattr(entry, "extra", None), "message_id", None)
        return entry.action, message_id, entry.user, entry.reason

    @commands.Cog.listener("on_guild_channel_pins_update")
    async def logger_channel_pins_update(
        self, channel: discord.abc.GuildChannel, last_pin: object | None
    ) -> None:
        details = await self._recent_pin_change(channel)
        if details is None:
            title = "Channel pins updated"
            description = f"Pins changed in {getattr(channel, 'mention', 'a channel')}."
            color = discord.Colour.orange()
            message_id = None
            actor = None
            reason = None
        else:
            action, message_id, actor, reason = details
            pinned = action is discord.AuditLogAction.message_pin
            title = "Message pinned" if pinned else "Message unpinned"
            description = (
                f"A message was {'pinned to' if pinned else 'unpinned from'} "
                f"{getattr(channel, 'mention', 'a channel')}."
            )
            color = discord.Colour.green() if pinned else discord.Colour.red()
        embed = self._embed(title, description, color=color)
        if message_id is not None:
            embed.add_field(name="Message", value=f"`{message_id}`", inline=False)
            self._add_item_id(embed, discord.Object(message_id))
        else:
            self._add_item_id(embed, channel)
        if last_pin is not None:
            embed.add_field(name="Last pin", value=_display(last_pin), inline=False)
        if actor is not None:
            embed.add_field(
                name="Changed by",
                value=f"{actor.mention} (`ID: {actor.id}`)",
                inline=True,
            )
            embed.add_field(
                name="Reason", value=_display(reason or "Not provided"), inline=False
            )
        await self._emit_logger(channel.guild, "message", embed)

    @commands.Cog.listener("on_raw_bulk_message_delete")
    async def logger_bulk_message_delete(
        self, payload: discord.RawBulkMessageDeleteEvent
    ) -> None:
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        channel = guild.get_channel(payload.channel_id)
        ids = list(payload.message_ids)
        if self._consume_purge_pending(payload.guild_id, payload.channel_id):
            return
        if channel is None or not isinstance(channel, discord.abc.Messageable):
            return
        embed = self._bulk_delete_embed(channel, ids)
        await self._emit_logger(
            guild,
            "message",
            embed,
            audit_action=discord.AuditLogAction.message_bulk_delete,
            audit_channel_id=payload.channel_id,
        )

    @commands.Cog.listener("on_logger_purge_start")
    async def logger_purge_start(self, guild_id: int, channel_id: int) -> None:
        self._set_purge_pending(guild_id, channel_id)

    @commands.Cog.listener("on_logger_purge_cancel")
    async def logger_purge_cancel(self, guild_id: int, channel_id: int) -> None:
        pending: dict[tuple[int, int], float] = getattr(self, "_pending_purges", {})
        pending.pop((guild_id, channel_id), None)

    @commands.Cog.listener("on_logger_purge")
    async def logger_purge(
        self,
        guild: discord.Guild,
        channel: discord.abc.Messageable,
        messages: Sequence[discord.Message],
    ) -> None:
        channel_id = getattr(channel, "id", None)
        if channel_id is None:
            return
        message_ids = [message.id for message in messages]
        embed = self._bulk_delete_embed(channel, message_ids, messages)
        await self._emit_logger(
            guild,
            "message",
            embed,
            audit_action=discord.AuditLogAction.message_bulk_delete,
            audit_channel_id=channel_id,
        )

    async def _log_recent_prune(self, guild: discord.Guild) -> bool:
        entry = await self._recent_audit_entry(
            guild, (discord.AuditLogAction.member_prune,), max_age=30
        )
        if entry is None:
            return False
        seen: dict[int, float] = getattr(self, "_logged_prunes", {})
        self._logged_prunes = seen
        now = time.monotonic()
        for key, expires_at in list(seen.items()):
            if expires_at <= now:
                seen.pop(key, None)
        if entry.id in seen:
            return True
        seen[entry.id] = now + 60
        embed = self._embed(
            "Member prune",
            "A member prune was performed in this server.",
            color=discord.Colour.red(),
        )
        extra = getattr(entry, "extra", None)
        if extra is not None:
            embed.add_field(
                name="Members removed",
                value=_display(getattr(extra, "members_removed", None)),
                inline=True,
            )
            embed.add_field(
                name="Inactive days",
                value=_display(getattr(extra, "delete_member_days", None)),
                inline=True,
            )
        self._add_item_id(embed, guild)
        if entry.user is not None:
            embed.add_field(
                name="Moderator",
                value=f"{entry.user.name}\n{entry.user.mention}",
                inline=True,
            )
        embed.add_field(
            name="Reason", value=_display(entry.reason or "Not provided"), inline=False
        )
        await self._emit_logger(guild, "moderation", embed)
        return True

    @staticmethod
    def _audit_action_label(action: discord.AuditLogAction) -> str:
        return str(action).rsplit(".", 1)[-1].replace("_", " ").title()

    def _mark_audit_event_seen(self, entry_id: int, *, ttl: float = 60) -> bool:
        seen: dict[int, float] = getattr(self, "_logged_audit_events", {})
        self._logged_audit_events = seen
        now = time.monotonic()
        for key, expires_at in list(seen.items()):
            if expires_at <= now:
                seen.pop(key, None)
        if entry_id in seen:
            return True
        seen[entry_id] = now + ttl
        return False

    @commands.Cog.listener("on_audit_log_entry_create")
    async def logger_audit_log_entry_create(self, entry: discord.AuditLogEntry) -> None:
        """Log moderation actions delivered through Discord's audit-log gateway."""
        guild = getattr(entry, "guild", None)
        if not isinstance(guild, discord.Guild):
            return

        action = entry.action
        moderation_actions = {
            discord.AuditLogAction.member_prune,
            discord.AuditLogAction.automod_rule_create,
            discord.AuditLogAction.automod_rule_update,
            discord.AuditLogAction.automod_rule_delete,
            discord.AuditLogAction.automod_block_message,
            discord.AuditLogAction.automod_flag_message,
        }
        if action not in moderation_actions:
            return
        if self._mark_audit_event_seen(entry.id):
            return

        if action is discord.AuditLogAction.member_prune:
            prunes: dict[int, float] = getattr(self, "_logged_prunes", {})
            self._logged_prunes = prunes
            prunes[entry.id] = time.monotonic() + 60
            title = "Member prune"
            description = "A member prune was performed in this server."
        else:
            title = f"AutoMod {self._audit_action_label(action)}"
            description = "An AutoMod moderation action was recorded."

        embed = self._embed(title, description, color=discord.Colour.red())
        target = getattr(entry, "target", None)
        target_id = getattr(target, "id", None)
        if target_id is not None:
            target_name = getattr(target, "name", None)
            target_text = (
                f"{_display(target_name)} (`{target_id}`)"
                if target_name
                else f"`{target_id}`"
            )
            embed.add_field(name="Target", value=target_text, inline=True)

        extra = getattr(entry, "extra", None)
        if extra is not None:
            details = []
            for label, attribute in (
                ("Members removed", "members_removed"),
                ("Inactive days", "delete_member_days"),
                ("Rule", "rule_name"),
                ("Rule ID", "rule_id"),
                ("Channel", "channel"),
                ("Message", "message_id"),
            ):
                value = getattr(extra, attribute, None)
                if value is not None:
                    details.append(f"**{label}:** {_display(value)}")
            if details:
                embed.add_field(
                    name="Details", value="\n".join(details)[:1024], inline=False
                )
        if entry.user is not None:
            embed.add_field(
                name="Moderator",
                value=f"{entry.user.name}\n{entry.user.mention}",
                inline=True,
            )
        embed.add_field(
            name="Reason", value=_display(entry.reason or "Not provided"), inline=False
        )
        self._add_item_id(
            embed,
            discord.Object(target_id) if isinstance(target_id, int) else guild,
        )
        await self._emit_logger(guild, "moderation", embed)

    @commands.Cog.listener("on_logger_fishie_moderation")
    async def logger_fishie_moderation_event(
        self,
        guild: discord.Guild,
        title: str,
        description: str | None,
        target: discord.User | discord.Member | None,
        moderator: discord.User | discord.Member | None,
        reason: str | None,
    ) -> None:
        """Receive moderation events emitted by Fishie moderation commands."""
        await self.log_fishie_moderation(
            guild,
            title,
            description,
            target=target,
            moderator=moderator,
            reason=reason,
        )

    @commands.Cog.listener("on_member_ban")
    async def logger_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        embed = self._embed("Member banned", None, color=discord.Colour.red())
        self._add_item_id(embed, user)
        await self._emit_logger(
            guild,
            "moderation",
            embed,
            audit_action=discord.AuditLogAction.ban,
            audit_target_id=user.id,
            audit_target=user,
            audit_actor_label="Moderator",
        )

    @commands.Cog.listener("on_member_unban")
    async def logger_member_unban(
        self, guild: discord.Guild, user: discord.User
    ) -> None:
        embed = self._embed(
            "Member unbanned",
            None,
            color=discord.Colour.green(),
        )
        self._add_item_id(embed, user)
        await self._emit_logger(
            guild,
            "moderation",
            embed,
            audit_action=discord.AuditLogAction.unban,
            audit_target_id=user.id,
            audit_target=user,
            audit_actor_label="Moderator",
        )

    @commands.Cog.listener("on_automod_action")
    async def logger_automod_action(self, action: object) -> None:
        """Log AutoMod gateway actions when Discord dispatches them.

        discord.py does not expose a strongly typed payload for every gateway
        version, so this listener intentionally reads the documented fields
        defensively. Audit-log entries still provide the moderator and reason
        for rule changes separately.
        """
        guild = getattr(action, "guild", None)
        if not isinstance(guild, discord.Guild):
            return
        user_id = getattr(action, "user_id", None)
        channel_id = getattr(action, "channel_id", None)
        user = guild.get_member(user_id) if isinstance(user_id, int) else None
        channel = guild.get_channel(channel_id) if isinstance(channel_id, int) else None
        description = "An AutoMod action was triggered."
        if user is not None:
            description = f"AutoMod acted on {user.mention}."
        embed = self._embed(description, None, color=discord.Colour.red())
        embed.add_field(
            name="Rule",
            value=_display(
                getattr(action, "rule_name", None)
                or getattr(action, "automod_rule_name", None)
                or getattr(action, "rule_id", None)
                or "Unknown"
            ),
            inline=True,
        )
        if user is not None:
            embed.add_field(
                name="User", value=f"{user.name}\n{user.mention}", inline=True
            )
        if channel is not None:
            embed.add_field(name="Channel", value=channel.mention, inline=True)
        content = getattr(action, "content", None)
        if content:
            embed.add_field(name="Content", value=_display(content), inline=False)
        message_id = getattr(action, "message_id", None)
        self._add_item_id(
            embed,
            discord.Object(message_id) if isinstance(message_id, int) else guild,
        )
        await self._emit_logger(guild, "moderation", embed)

    async def log_fishie_moderation(
        self,
        guild: discord.Guild,
        title: str,
        description: str | None = None,
        *,
        target: discord.User | discord.Member | None = None,
        moderator: discord.User | discord.Member | None = None,
        reason: str | None = None,
    ) -> None:
        """Emit a moderation event generated by a Fishie command.

        Moderation commands can call this helper after completing an action so
        the logger can identify Fishie as the source even when Discord has no
        corresponding audit entry.
        """
        embed = self._embed(title, description, color=discord.Colour.red())
        if target is not None:
            embed.add_field(
                name="Target", value=f"{target.name}\n{target.mention}", inline=True
            )
        if moderator is not None:
            embed.add_field(
                name="Moderator",
                value=f"{moderator.name}\n{moderator.mention}",
                inline=True,
            )
        embed.add_field(name="Source", value="Fishie moderation command", inline=True)
        embed.add_field(
            name="Reason", value=_display(reason or "Not provided"), inline=False
        )
        self._add_item_id(embed, target or guild)
        await self._emit_logger(guild, "moderation", embed)

    def cog_unload(self) -> None:
        """Cancel pending position batches when the moderation cog reloads."""
        rebind_task = getattr(self, "_logger_rebind_task", None)
        if rebind_task is not None:
            rebind_task.cancel()
            self._logger_rebind_task = None
        for task in (
            *getattr(self, "_channel_move_tasks", {}).values(),
            *getattr(self, "_role_move_tasks", {}).values(),
        ):
            task.cancel()
        getattr(self, "_channel_move_tasks", {}).clear()
        getattr(self, "_channel_move_batches", {}).clear()
        getattr(self, "_role_move_tasks", {}).clear()
        getattr(self, "_role_move_batches", {}).clear()
