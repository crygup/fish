from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast

import discord
from discord import app_commands
from discord.ext import commands

from core import Cog, is_operational_guild
from core.handoff import is_legacy_instance
from utils import AuthorLayoutView

if TYPE_CHECKING:
    from extensions.context import GuildContext


ProtectionMode = str

TRIGGER_LABELS: dict[str, str] = {
    "mass_bans": "Mass bans",
    "mass_kicks": "Mass kicks",
    "channel_changes": "Channel deletions/renames",
    "role_changes": "Role deletions/renames",
    "member_timeouts": "Member timeouts",
    "security_level": "Server security changes",
    "vanity_url": "Vanity URL changes",
    "administrator_role": "Administrator roles",
    "invites": "Discord invites",
}

TRIGGER_LIMITS: dict[str, tuple[int, int]] = {
    "mass_bans": (3, 10),
    "mass_kicks": (3, 10),
    "channel_changes": (3, 15),
    "role_changes": (3, 15),
    "member_timeouts": (3, 15),
    "security_level": (1, 1),
    "vanity_url": (1, 1),
    "administrator_role": (1, 1),
    "invites": (1, 1),
}

TRIGGER_ALIASES: dict[str, str] = {
    "ban": "mass_bans",
    "bans": "mass_bans",
    "mass_ban": "mass_bans",
    "mass_bans": "mass_bans",
    "kick": "mass_kicks",
    "kicks": "mass_kicks",
    "mass_kick": "mass_kicks",
    "mass_kicks": "mass_kicks",
    "channel": "channel_changes",
    "channels": "channel_changes",
    "channel_change": "channel_changes",
    "channel_changes": "channel_changes",
    "role": "role_changes",
    "roles": "role_changes",
    "role_change": "role_changes",
    "role_changes": "role_changes",
    "timeout": "member_timeouts",
    "timeouts": "member_timeouts",
    "member_timeout": "member_timeouts",
    "member_timeouts": "member_timeouts",
    "security": "security_level",
    "security_level": "security_level",
    "verification": "security_level",
    "verification_level": "security_level",
    "vanity": "vanity_url",
    "vanity_url": "vanity_url",
    "admin_role": "administrator_role",
    "admin_roles": "administrator_role",
    "administrator_role": "administrator_role",
    "administrator_roles": "administrator_role",
    "invite": "invites",
    "invites": "invites",
    "all": "all",
}

MODE_ALIASES = {
    "warn": "warn",
    "warning": "warn",
    "lock": "lock",
    "timeout": "lock",
    "both": "both",
}

INVITE_RE = re.compile(
    r"(?i)(?:https?://)?(?:www\.)?(?:discord(?:app)?\.com/invite|discord\.gg)/"
    r"[A-Za-z0-9-]+"
)
PROTECTION_LOCK_DURATION = timedelta(days=27, hours=23)


def _trigger_label(trigger: str) -> str:
    return TRIGGER_LABELS.get(trigger, trigger.replace("_", " ").title())


@dataclass(frozen=True, slots=True)
class ProtectionConfig:
    guild_id: int
    channel_id: int
    protection_role_id: int | None
    enabled: bool
    response_mode: ProtectionMode
    allowed_vanity_code: str | None
    enabled_triggers: frozenset[str]


def _normalize_trigger(value: str) -> str:
    normalized = value.strip().casefold().replace("-", "_").replace(" ", "_")
    trigger = TRIGGER_ALIASES.get(normalized)
    if trigger is None:
        choices = ", ".join(TRIGGER_LABELS)
        raise commands.BadArgument(
            f"Unknown protection trigger. Choose from: {choices}."
        )
    return trigger


def _normalize_mode(value: str) -> str:
    mode = MODE_ALIASES.get(value.strip().casefold())
    if mode is None:
        raise commands.BadArgument("Choose `warn`, `lock`, or `both`.")
    return mode


async def _protection_command_check(ctx: GuildContext) -> bool:
    cog = ctx.cog
    if not isinstance(cog, Protection):
        raise commands.CheckFailure("Protection is unavailable right now.")
    if not isinstance(ctx.author, discord.Member):
        raise commands.NoPrivateMessage()
    config = cog._protection_configs.get(ctx.guild.id)
    if cog._can_manage_protection(ctx.author, config, allow_role=True):
        return True
    raise commands.CheckFailure(
        "You must be the server owner, have the protection role, or have "
        "Administrator with a role above Fishie to manage protection."
    )


async def _protection_trust_root_check(ctx: GuildContext) -> bool:
    cog = ctx.cog
    if not isinstance(cog, Protection):
        raise commands.CheckFailure("Protection is unavailable right now.")
    if not isinstance(ctx.author, discord.Member):
        raise commands.NoPrivateMessage()
    config = cog._protection_configs.get(ctx.guild.id)
    if cog._can_manage_protection(ctx.author, config, allow_role=False):
        return True
    raise commands.CheckFailure(
        "Only the server owner or an Administrator above Fishie can change the "
        "protection role."
    )


def protection_manager_only(callback: Any) -> Any:
    callback = commands.check(_protection_command_check)(callback)
    return commands.guild_only()(callback)


def protection_trust_root_only(callback: Any) -> Any:
    callback = commands.check(_protection_trust_root_check)(callback)
    return commands.guild_only()(callback)


class _ProtectionView(AuthorLayoutView):
    def __init__(self, cog: Protection, ctx: GuildContext, *, timeout: float = 300):
        super().__init__(ctx, timeout=timeout)
        self.cog = cog
        self.ctx = ctx

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await super().interaction_check(interaction):
            return False
        member = interaction.user
        config = self.cog._protection_configs.get(self.ctx.guild.id)
        if isinstance(member, discord.Member) and self.cog._can_manage_protection(
            member, config, allow_role=True
        ):
            return True
        await interaction.response.send_message(
            "You no longer have permission to manage protection.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False


class ProtectionChannelModal(discord.ui.Modal, title="Protection logs channel"):
    channel_input = discord.ui.TextInput(
        label="Channel name, ID, or mention",
        placeholder="#protection-logs, protection-logs, or a channel ID",
        required=True,
        max_length=100,
    )

    def __init__(self, picker: ProtectionChannelPicker) -> None:
        super().__init__()
        self.picker = picker

    async def on_submit(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        config = self.picker.cog._protection_configs.get(self.picker.ctx.guild.id)
        if not isinstance(
            member, discord.Member
        ) or not self.picker.cog._can_manage_protection(
            member, config, allow_role=True
        ):
            await interaction.response.send_message(
                "You no longer have permission to manage protection.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            channel = await self.picker.cog._resolve_protection_channel(
                self.picker.ctx, str(self.channel_input.value)
            )
            await self.picker.cog._set_protection_channel(
                self.picker.ctx, channel, interaction.user.id
            )
        except (commands.CommandError, discord.HTTPException) as exc:
            await interaction.response.send_message(
                str(exc),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        config = self.picker.cog._require_protection_config(self.picker.ctx.guild.id)
        await interaction.response.edit_message(
            view=ProtectionSettingsView(self.picker.cog, self.picker.ctx, config),
            allowed_mentions=discord.AllowedMentions.none(),
        )


class ProtectionChannelPicker(_ProtectionView):
    def __init__(
        self,
        cog: Protection,
        ctx: GuildContext,
        *,
        configured: bool,
    ) -> None:
        super().__init__(cog, ctx)
        self.configured = configured
        enter = discord.ui.Button(
            label="Enter channel", style=discord.ButtonStyle.secondary
        )
        enter.callback = self._open_modal
        choose = discord.ui.Button(
            label="Choose channels", style=discord.ButtonStyle.secondary
        )
        choose.callback = self._open_pages
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
                discord.ui.TextDisplay("## Configure protection logs"),
                discord.ui.TextDisplay(
                    "Enter a text channel, choose from the channel list, use this "
                    "channel, or create `#protection-logs`."
                ),
                discord.ui.ActionRow(enter, choose),
                discord.ui.ActionRow(current, create, back, quit_button),
                accent_color=ctx.bot.embedcolor,
            )
        )

    async def _open_modal(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ProtectionChannelModal(self))

    async def _open_pages(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            view=ProtectionChannelPages(self),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _finish(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        await self.cog._set_protection_channel(self.ctx, channel, interaction.user.id)
        config = self.cog._require_protection_config(self.ctx.guild.id)
        await interaction.edit_original_response(
            view=ProtectionSettingsView(self.cog, self.ctx, config),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _set_current(self, interaction: discord.Interaction) -> None:
        channel = self.ctx.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "Run the command in a text channel to use this option.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.defer()
        try:
            await self._finish(interaction, channel)
        except (commands.CommandError, discord.HTTPException) as exc:
            await interaction.followup.send(
                str(exc),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _create_channel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            channel = await self.ctx.guild.create_text_channel(
                "protection-logs",
                reason=f"Protection logs created by {interaction.user}",
            )
            await self._finish(interaction, channel)
        except (discord.HTTPException, commands.CommandError) as exc:
            await interaction.followup.send(
                str(exc) or "I could not create the protection logs channel.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _back(self, interaction: discord.Interaction) -> None:
        if not self.configured:
            self.stop()
            await interaction.response.edit_message(view=None)
            return
        config = self.cog._require_protection_config(self.ctx.guild.id)
        await interaction.response.edit_message(
            view=ProtectionSettingsView(self.cog, self.ctx, config),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _quit(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(view=None)


class ProtectionChannelPages(_ProtectionView):
    def __init__(self, picker: ProtectionChannelPicker) -> None:
        super().__init__(picker.cog, picker.ctx)
        self.picker = picker
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
                    "## Choose protection logs channel\n"
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
            await self.cog._set_protection_channel(
                self.ctx, channel, interaction.user.id
            )
        except (commands.CommandError, discord.HTTPException) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        config = self.cog._require_protection_config(self.ctx.guild.id)
        await interaction.response.edit_message(
            view=ProtectionSettingsView(self.cog, self.ctx, config),
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


class ProtectionSettingsView(_ProtectionView):
    def __init__(
        self, cog: Protection, ctx: GuildContext, config: ProtectionConfig
    ) -> None:
        super().__init__(cog, ctx)
        self.config = config
        self._render()

    def _render(self) -> None:
        self.clear_items()
        status = "Enabled" if self.config.enabled else "Disabled"
        role = (
            f"<@&{self.config.protection_role_id}>"
            if self.config.protection_role_id
            else "Not set"
        )
        trigger_buttons: list[discord.ui.Button[Any]] = []
        for trigger, label in TRIGGER_LABELS.items():
            enabled = trigger in self.config.enabled_triggers
            button = discord.ui.Button(
                label=f"{'Disable' if enabled else 'Enable'} {label}"[:80],
                style=(
                    discord.ButtonStyle.danger
                    if enabled
                    else discord.ButtonStyle.secondary
                ),
            )

            async def toggle(
                interaction: discord.Interaction, selected: str = trigger
            ) -> None:
                current = self.cog._require_protection_config(self.ctx.guild.id)
                await self.cog._set_protection_trigger(
                    self.ctx.guild.id,
                    selected,
                    selected not in current.enabled_triggers,
                    interaction.user.id,
                )
                config = self.cog._require_protection_config(self.ctx.guild.id)
                await interaction.response.edit_message(
                    view=ProtectionSettingsView(self.cog, self.ctx, config),
                    allowed_mentions=discord.AllowedMentions.none(),
                )

            button.callback = toggle
            trigger_buttons.append(button)

        trigger_rows = [
            discord.ui.ActionRow(*trigger_buttons[offset : offset + 5])
            for offset in range(0, len(trigger_buttons), 5)
        ]
        mode_buttons: list[discord.ui.Button[Any]] = []
        for mode, label in (
            ("warn", "Warn only"),
            ("lock", "Lock only"),
            ("both", "Warn and lock"),
        ):
            button = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.secondary,
                disabled=self.config.response_mode == mode,
            )

            async def set_mode(
                interaction: discord.Interaction, selected: str = mode
            ) -> None:
                await self.cog._set_protection_mode(
                    self.ctx.guild.id, selected, interaction.user.id
                )
                config = self.cog._require_protection_config(self.ctx.guild.id)
                await interaction.response.edit_message(
                    view=ProtectionSettingsView(self.cog, self.ctx, config),
                    allowed_mentions=discord.AllowedMentions.none(),
                )

            button.callback = set_mode
            mode_buttons.append(button)

        change_channel = discord.ui.Button(
            label="Change logs channel", style=discord.ButtonStyle.secondary
        )
        change_channel.callback = self._change_channel
        quit_button = discord.ui.Button(
            label="Quit", style=discord.ButtonStyle.secondary
        )
        quit_button.callback = self._quit
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(f"## Protection · {self.ctx.guild.name}"),
                discord.ui.TextDisplay(
                    f"**Status:** {status}\n"
                    f"**Logs:** <#{self.config.channel_id}>\n"
                    f"**Protection role:** {role}\n"
                    f"**Response:** {self.config.response_mode.title()}"
                ),
                discord.ui.Separator(),
                discord.ui.TextDisplay("### Triggers"),
                *trigger_rows,
                discord.ui.Separator(),
                discord.ui.TextDisplay("### Response mode"),
                discord.ui.ActionRow(*mode_buttons),
                discord.ui.TextDisplay(
                    "-# Lock strips every role Fishie can manage and applies a "
                    "27-day, 23-hour timeout."
                ),
                discord.ui.ActionRow(change_channel, quit_button),
                discord.ui.TextDisplay(
                    "-# Disable a trigger before performing that protected action. "
                    "The server owner and protection role are exempt."
                ),
                accent_color=self.ctx.bot.embedcolor,
            )
        )

    async def _change_channel(self, interaction: discord.Interaction) -> None:
        picker = ProtectionChannelPicker(self.cog, self.ctx, configured=True)
        await interaction.response.send_message(
            view=picker,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _quit(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(view=None)


class Protection(Cog):
    """Anti-nuke and anti-raid protection for moderation."""

    def _initialize_protection(self) -> None:
        self._protection_configs: dict[int, ProtectionConfig] = {}
        self._protection_windows: dict[tuple[int, int, str], deque[float]] = {}
        self._protection_cooldowns: dict[tuple[int, int, str], float] = {}
        self._protection_seen_entries: dict[int, float] = {}

    async def _load_protection_settings(self) -> None:
        self._protection_configs.clear()
        settings = await self.bot.pool.fetch(
            "SELECT guild_id, channel_id, protection_role_id, enabled, "
            "response_mode, allowed_vanity_code FROM guild_protection"
        )
        triggers = await self.bot.pool.fetch(
            "SELECT guild_id, trigger, enabled FROM guild_protection_triggers"
        )
        enabled_by_guild: dict[int, set[str]] = {
            int(row["guild_id"]): set(TRIGGER_LABELS) for row in settings
        }
        for row in triggers:
            guild_id = int(row["guild_id"])
            trigger = str(row["trigger"])
            if trigger not in TRIGGER_LABELS:
                continue
            enabled_by_guild.setdefault(guild_id, set(TRIGGER_LABELS))
            if row["enabled"]:
                enabled_by_guild[guild_id].add(trigger)
            else:
                enabled_by_guild[guild_id].discard(trigger)
        for row in settings:
            guild_id = int(row["guild_id"])
            self._protection_configs[guild_id] = ProtectionConfig(
                guild_id=guild_id,
                channel_id=int(row["channel_id"]),
                protection_role_id=(
                    int(row["protection_role_id"])
                    if row["protection_role_id"] is not None
                    else None
                ),
                enabled=bool(row["enabled"]),
                response_mode=str(row["response_mode"]),
                allowed_vanity_code=row["allowed_vanity_code"],
                enabled_triggers=frozenset(enabled_by_guild[guild_id]),
            )

    async def _refresh_protection_config(
        self, guild_id: int
    ) -> ProtectionConfig | None:
        row = await self.bot.pool.fetchrow(
            "SELECT guild_id, channel_id, protection_role_id, enabled, "
            "response_mode, allowed_vanity_code FROM guild_protection "
            "WHERE guild_id = $1",
            guild_id,
        )
        if row is None:
            self._protection_configs.pop(guild_id, None)
            return None
        trigger_rows = await self.bot.pool.fetch(
            "SELECT trigger, enabled FROM guild_protection_triggers "
            "WHERE guild_id = $1",
            guild_id,
        )
        enabled = set(TRIGGER_LABELS)
        for trigger_row in trigger_rows:
            trigger = str(trigger_row["trigger"])
            if trigger not in TRIGGER_LABELS:
                continue
            if trigger_row["enabled"]:
                enabled.add(trigger)
            else:
                enabled.discard(trigger)
        config = ProtectionConfig(
            guild_id=guild_id,
            channel_id=int(row["channel_id"]),
            protection_role_id=(
                int(row["protection_role_id"])
                if row["protection_role_id"] is not None
                else None
            ),
            enabled=bool(row["enabled"]),
            response_mode=str(row["response_mode"]),
            allowed_vanity_code=row["allowed_vanity_code"],
            enabled_triggers=frozenset(enabled),
        )
        self._protection_configs[guild_id] = config
        return config

    def _require_protection_config(self, guild_id: int) -> ProtectionConfig:
        config = self._protection_configs.get(guild_id)
        if config is None:
            raise commands.BadArgument(
                "Protection is not configured. Run `protection` to set it up."
            )
        return config

    @staticmethod
    def _can_manage_protection(
        member: discord.Member,
        config: ProtectionConfig | None,
        *,
        allow_role: bool,
    ) -> bool:
        guild = member.guild
        if member.id == guild.owner_id:
            return True
        if (
            allow_role
            and config is not None
            and config.protection_role_id is not None
            and member.get_role(config.protection_role_id) is not None
        ):
            return True
        me = guild.me
        return bool(
            me is not None
            and member.guild_permissions.administrator
            and member.top_role > me.top_role
        )

    @staticmethod
    def _actor_is_exempt(
        guild: discord.Guild,
        actor_id: int,
        config: ProtectionConfig,
        member: discord.Member | None = None,
    ) -> bool:
        if actor_id in {guild.owner_id, guild.me.id if guild.me else 0}:
            return True
        member = member or guild.get_member(actor_id)
        return bool(
            member is not None
            and config.protection_role_id is not None
            and member.get_role(config.protection_role_id) is not None
        )

    def _validate_protection_channel(
        self, guild: discord.Guild, channel: discord.TextChannel
    ) -> None:
        if channel.guild.id != guild.id:
            raise commands.BadArgument("The channel must belong to this server.")
        me = guild.me
        if me is None:
            raise commands.BadArgument("Fishie is not available in this server.")
        channel_permissions = channel.permissions_for(me)
        missing: list[str] = []
        for permission, enabled in (
            ("view_channel", channel_permissions.view_channel),
            ("send_messages", channel_permissions.send_messages),
            ("view_audit_log", me.guild_permissions.view_audit_log),
            ("manage_messages", me.guild_permissions.manage_messages),
            ("manage_roles", me.guild_permissions.manage_roles),
            ("moderate_members", me.guild_permissions.moderate_members),
            ("manage_guild", me.guild_permissions.manage_guild),
        ):
            if not enabled:
                missing.append(permission)
        if missing:
            raise commands.BotMissingPermissions(missing)

    async def _resolve_protection_channel(
        self, ctx: GuildContext, value: str
    ) -> discord.TextChannel:
        channel = await commands.TextChannelConverter().convert(ctx, value)
        if channel.guild.id != ctx.guild.id:
            raise commands.BadArgument("The channel must belong to this server.")
        return channel

    async def _set_protection_channel(
        self,
        ctx: GuildContext,
        channel: discord.TextChannel,
        actor_id: int,
    ) -> None:
        self._validate_protection_channel(ctx.guild, channel)
        await self.bot.pool.execute(
            """
            INSERT INTO guild_protection(
                guild_id, channel_id, enabled, response_mode,
                allowed_vanity_code, configured_by, updated_by
            ) VALUES ($1, $2, TRUE, 'both', $3, $4, $4)
            ON CONFLICT (guild_id) DO UPDATE
            SET channel_id = EXCLUDED.channel_id,
                updated_by = EXCLUDED.updated_by,
                updated_at = now()
            """,
            ctx.guild.id,
            channel.id,
            ctx.guild.vanity_url_code,
            actor_id,
        )
        await self.bot.pool.executemany(
            """
            INSERT INTO guild_protection_triggers(
                guild_id, trigger, enabled, threshold, window_seconds
            ) VALUES ($1, $2, TRUE, $3, $4)
            ON CONFLICT (guild_id, trigger) DO NOTHING
            """,
            [
                (ctx.guild.id, trigger, threshold, window)
                for trigger, (threshold, window) in TRIGGER_LIMITS.items()
            ],
        )
        await self._refresh_protection_config(ctx.guild.id)

    async def _set_protection_trigger(
        self,
        guild_id: int,
        trigger: str,
        enabled: bool,
        actor_id: int,
    ) -> None:
        threshold, window = TRIGGER_LIMITS[trigger]
        await self.bot.pool.execute(
            """
            INSERT INTO guild_protection_triggers(
                guild_id, trigger, enabled, threshold, window_seconds
            ) VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (guild_id, trigger) DO UPDATE
            SET enabled = EXCLUDED.enabled, updated_at = now()
            """,
            guild_id,
            trigger,
            enabled,
            threshold,
            window,
        )
        await self.bot.pool.execute(
            "UPDATE guild_protection SET updated_by = $2, updated_at = now() "
            "WHERE guild_id = $1",
            guild_id,
            actor_id,
        )
        await self._refresh_protection_config(guild_id)

    async def _set_protection_mode(
        self, guild_id: int, mode: str, actor_id: int
    ) -> None:
        await self.bot.pool.execute(
            "UPDATE guild_protection SET response_mode = $2, updated_by = $3, "
            "updated_at = now() WHERE guild_id = $1",
            guild_id,
            mode,
            actor_id,
        )
        config = self._require_protection_config(guild_id)
        self._protection_configs[guild_id] = replace(config, response_mode=mode)

    async def _set_protection_enabled(
        self, guild_id: int, enabled: bool, actor_id: int
    ) -> None:
        await self.bot.pool.execute(
            "UPDATE guild_protection SET enabled = $2, updated_by = $3, "
            "updated_at = now() WHERE guild_id = $1",
            guild_id,
            enabled,
            actor_id,
        )
        config = self._require_protection_config(guild_id)
        self._protection_configs[guild_id] = replace(config, enabled=enabled)

    async def _set_allowed_vanity(self, guild_id: int, code: str | None) -> None:
        await self.bot.pool.execute(
            "UPDATE guild_protection SET allowed_vanity_code = $2, "
            "updated_at = now() WHERE guild_id = $1",
            guild_id,
            code,
        )
        config = self._protection_configs.get(guild_id)
        if config is not None:
            self._protection_configs[guild_id] = replace(
                config, allowed_vanity_code=code
            )

    async def _send_protection(self, ctx: GuildContext) -> None:
        config = self._protection_configs.get(ctx.guild.id)
        if config is None:
            view: AuthorLayoutView = ProtectionChannelPicker(
                self, ctx, configured=False
            )
        else:
            view = ProtectionSettingsView(self, ctx, config)
        view.message = await ctx.send(
            view=view,
            ephemeral=ctx.interaction is not None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_group(
        name="protection",
        aliases=(
            "antinuke",
            "serverproection",
            "serverprotection",
            "protect",
            "antiraid",
        ),
        fallback="info",
    )
    @commands.guild_only()
    @commands.check(_protection_command_check)
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def protection(self, ctx: GuildContext) -> None:
        """Set up and manage anti-nuke and anti-raid protection."""
        if ctx.invoked_subcommand is not None:
            return
        await self._send_protection(ctx)

    @protection.command(name="enable")
    @protection_manager_only
    async def protection_enable(
        self, ctx: GuildContext, *, trigger: str | None = None
    ) -> None:
        """Enable protection or one protection trigger."""
        self._require_protection_config(ctx.guild.id)
        if trigger is None:
            await self._set_protection_enabled(ctx.guild.id, True, ctx.author.id)
            await ctx.send("Protection is enabled.")
            return
        normalized = _normalize_trigger(trigger)
        targets = TRIGGER_LABELS if normalized == "all" else (normalized,)
        for target in targets:
            await self._set_protection_trigger(
                ctx.guild.id, target, True, ctx.author.id
            )
        await ctx.send(
            "Enabled all protection triggers."
            if normalized == "all"
            else f"Enabled **{TRIGGER_LABELS[normalized]}** protection."
        )

    @protection.command(name="disable")
    @protection_manager_only
    async def protection_disable(
        self, ctx: GuildContext, *, trigger: str | None = None
    ) -> None:
        """Disable protection or one protection trigger."""
        self._require_protection_config(ctx.guild.id)
        if trigger is None:
            await self._set_protection_enabled(ctx.guild.id, False, ctx.author.id)
            await ctx.send("Protection is disabled.")
            return
        normalized = _normalize_trigger(trigger)
        targets = TRIGGER_LABELS if normalized == "all" else (normalized,)
        for target in targets:
            await self._set_protection_trigger(
                ctx.guild.id, target, False, ctx.author.id
            )
        if normalized in {"all", "vanity_url"}:
            await self._set_allowed_vanity(ctx.guild.id, ctx.guild.vanity_url_code)
        await ctx.send(
            "Disabled all protection triggers."
            if normalized == "all"
            else f"Disabled **{TRIGGER_LABELS[normalized]}** protection."
        )

    @protection.command(name="action", aliases=("mode", "response"))
    @protection_manager_only
    async def protection_action(
        self, ctx: GuildContext, mode: str | None = None
    ) -> None:
        """View or change whether protection warns, locks, or does both."""
        config = self._require_protection_config(ctx.guild.id)
        if mode is None:
            await ctx.send(f"Protection response: **{config.response_mode.title()}**")
            return
        normalized = _normalize_mode(mode)
        await self._set_protection_mode(ctx.guild.id, normalized, ctx.author.id)
        await ctx.send(f"Protection response set to **{normalized.title()}**.")

    @protection.command(name="channel")
    @protection_manager_only
    async def protection_channel(
        self,
        ctx: GuildContext,
        channel: discord.TextChannel | None = None,
    ) -> None:
        """View or change the protection logs channel."""
        self._require_protection_config(ctx.guild.id)
        if channel is None:
            view = ProtectionChannelPicker(self, ctx, configured=True)
            view.message = await ctx.send(
                view=view,
                ephemeral=ctx.interaction is not None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await self._set_protection_channel(ctx, channel, ctx.author.id)
        await ctx.send(f"Protection logs will be sent to {channel.mention}.")

    @staticmethod
    def _validate_manual_lock_target(ctx: GuildContext, member: discord.Member) -> None:
        actor = ctx.author
        guild = ctx.guild
        me = guild.me
        if not isinstance(actor, discord.Member) or me is None:
            raise commands.BadArgument("That member cannot be managed right now.")
        if member.id == guild.owner_id:
            raise commands.BadArgument("The server owner cannot be locked.")
        if member.id == me.id:
            raise commands.BadArgument("Fishie cannot lock itself.")
        if member.top_role >= me.top_role:
            raise commands.BadArgument(
                "That member's highest role is above or equal to Fishie."
            )
        if actor.id != guild.owner_id and member.top_role >= actor.top_role:
            raise commands.BadArgument(
                "You cannot manage a member whose highest role is above or equal "
                "to yours."
            )

    @protection.command(name="lock")
    @protection_manager_only
    async def protection_lock(self, ctx: GuildContext, member: discord.Member) -> None:
        """Strip a member's roles and lock them until protection unlocks them."""
        config = self._require_protection_config(ctx.guild.id)
        self._validate_manual_lock_target(ctx, member)
        contained, details = await self._lock_protection_member(
            ctx.guild, member, "manual_lock"
        )
        await self._record_protection_incident(
            guild_id=ctx.guild.id,
            actor_id=ctx.author.id,
            target_id=member.id,
            trigger="manual_lock",
            audit_entry_id=None,
            mode="lock",
            contained=contained,
            details=details,
        )
        await self._send_protection_log(
            ctx.guild,
            config,
            actor_id=ctx.author.id,
            target_id=member.id,
            trigger="manual_lock",
            count=1,
            contained=contained,
            details=details,
            response="Locked" if contained else "Lock incomplete",
        )
        await ctx.send(
            f"{'Locked' if contained else 'Partially locked'} **{member}**. {details}"
        )

    @protection.command(name="unlock")
    @protection_manager_only
    async def protection_unlock(
        self, ctx: GuildContext, member: discord.Member
    ) -> None:
        """Restore roles saved by protection and remove its timeout."""
        config = self._require_protection_config(ctx.guild.id)
        self._validate_manual_lock_target(ctx, member)
        complete, details = await self._unlock_protection_member(
            ctx.guild, member, ctx.author
        )
        await self._record_protection_incident(
            guild_id=ctx.guild.id,
            actor_id=ctx.author.id,
            target_id=member.id,
            trigger="manual_unlock",
            audit_entry_id=None,
            mode="lock",
            contained=complete,
            details=details,
        )
        await self._send_protection_log(
            ctx.guild,
            config,
            actor_id=ctx.author.id,
            target_id=member.id,
            trigger="manual_unlock",
            count=1,
            contained=complete,
            details=details,
            response="Unlocked" if complete else "Unlock incomplete",
        )
        await ctx.send(
            f"{'Unlocked' if complete else 'Partially unlocked'} **{member}**. "
            f"{details}"
        )

    @cast(Any, protection.group)(name="role", invoke_without_command=True)
    @protection_manager_only
    async def protection_role(self, ctx: GuildContext) -> None:
        """View or manage the role trusted by protection."""
        if ctx.invoked_subcommand is not None:
            return
        config = self._require_protection_config(ctx.guild.id)
        if config.protection_role_id is None:
            await ctx.send("No protection role is set.")
        else:
            await ctx.send(f"Protection role: <@&{config.protection_role_id}>")

    @protection_role.command(name="set")
    @protection_trust_root_only
    async def protection_role_set(self, ctx: GuildContext, role: discord.Role) -> None:
        """Set the role allowed to manage and bypass protection."""
        self._require_protection_config(ctx.guild.id)
        if role.is_default() or role.managed:
            raise commands.BadArgument("Choose a normal server role.")
        await self.bot.pool.execute(
            "UPDATE guild_protection SET protection_role_id = $2, updated_by = $3, "
            "updated_at = now() WHERE guild_id = $1",
            ctx.guild.id,
            role.id,
            ctx.author.id,
        )
        await self._refresh_protection_config(ctx.guild.id)
        await ctx.send(f"{role.mention} is now the protection role.")

    @protection_role.command(name="remove", aliases=("clear",))
    @protection_trust_root_only
    async def protection_role_remove(self, ctx: GuildContext) -> None:
        """Remove the trusted protection role."""
        self._require_protection_config(ctx.guild.id)
        await self.bot.pool.execute(
            "UPDATE guild_protection SET protection_role_id = NULL, updated_by = $2, "
            "updated_at = now() WHERE guild_id = $1",
            ctx.guild.id,
            ctx.author.id,
        )
        await self._refresh_protection_config(ctx.guild.id)
        await ctx.send("Removed the protection role.")

    @staticmethod
    def _change_value(entry: discord.AuditLogEntry, name: str) -> tuple[Any, Any]:
        return getattr(entry.before, name, None), getattr(entry.after, name, None)

    @classmethod
    def _classify_protection_entry(
        cls, entry: discord.AuditLogEntry
    ) -> tuple[str, ...]:
        action = entry.action
        triggers: list[str] = []
        if action is discord.AuditLogAction.ban:
            triggers.append("mass_bans")
        elif action is discord.AuditLogAction.kick:
            triggers.append("mass_kicks")
        elif action is discord.AuditLogAction.channel_delete:
            triggers.append("channel_changes")
        elif action is discord.AuditLogAction.channel_update:
            before, after = cls._change_value(entry, "name")
            if before is not None and after is not None and before != after:
                triggers.append("channel_changes")
        elif action is discord.AuditLogAction.role_delete:
            triggers.append("role_changes")
        elif action is discord.AuditLogAction.role_update:
            before_name, after_name = cls._change_value(entry, "name")
            if (
                before_name is not None
                and after_name is not None
                and before_name != after_name
            ):
                triggers.append("role_changes")
            before_permissions, after_permissions = cls._change_value(
                entry, "permissions"
            )
            before_admin = bool(getattr(before_permissions, "administrator", False))
            after_admin = bool(getattr(after_permissions, "administrator", False))
            if after_admin and not before_admin:
                triggers.append("administrator_role")
        elif action is discord.AuditLogAction.role_create:
            permissions = getattr(entry.target, "permissions", None) or getattr(
                entry.after, "permissions", None
            )
            if getattr(permissions, "administrator", False):
                triggers.append("administrator_role")
        elif action is discord.AuditLogAction.member_update:
            before_timeout = getattr(entry.before, "timed_out_until", None)
            after_timeout = getattr(entry.after, "timed_out_until", None)
            if before_timeout is None:
                before_timeout = getattr(
                    entry.before, "communication_disabled_until", None
                )
            if after_timeout is None:
                after_timeout = getattr(
                    entry.after, "communication_disabled_until", None
                )
            if after_timeout is not None and before_timeout != after_timeout:
                triggers.append("member_timeouts")
        elif action is discord.AuditLogAction.guild_update:
            for attribute in (
                "verification_level",
                "explicit_content_filter",
                "mfa_level",
            ):
                before, after = cls._change_value(entry, attribute)
                if before != after and (before is not None or after is not None):
                    triggers.append("security_level")
                    break
            before_vanity, after_vanity = cls._change_value(entry, "vanity_url_code")
            if before_vanity != after_vanity and (
                before_vanity is not None or after_vanity is not None
            ):
                triggers.append("vanity_url")
        return tuple(dict.fromkeys(triggers))

    def _mark_protection_entry_seen(self, entry_id: int) -> bool:
        now = time.monotonic()
        for seen_id, expires_at in list(self._protection_seen_entries.items()):
            if expires_at <= now:
                self._protection_seen_entries.pop(seen_id, None)
        if entry_id in self._protection_seen_entries:
            return True
        self._protection_seen_entries[entry_id] = now + 120
        return False

    def _threshold_reached(
        self, guild_id: int, actor_id: int, trigger: str
    ) -> tuple[bool, int]:
        threshold, window_seconds = TRIGGER_LIMITS[trigger]
        if threshold <= 1:
            return True, 1
        now = time.monotonic()
        key = (guild_id, actor_id, trigger)
        events = self._protection_windows.setdefault(key, deque())
        while events and events[0] <= now - window_seconds:
            events.popleft()
        events.append(now)
        count = len(events)
        if count < threshold:
            return False, count
        events.clear()
        if self._protection_cooldowns.get(key, 0) > now:
            return False, count
        self._protection_cooldowns[key] = now + 60
        return True, count

    @staticmethod
    def _removable_protection_roles(
        member: discord.Member, me: discord.Member
    ) -> list[discord.Role]:
        return [
            role
            for role in member.roles
            if not role.is_default() and not role.managed and role < me.top_role
        ]

    async def _lock_protection_member(
        self, guild: discord.Guild, member: discord.Member, trigger: str
    ) -> tuple[bool, str]:
        me = guild.me
        if me is None:
            return False, "Fishie is not available in the server."
        if member.id == guild.owner_id:
            return False, "The server owner cannot be locked."
        if member.id == me.id:
            return False, "Fishie cannot lock itself."
        if member.top_role >= me.top_role:
            return False, "The actor's highest role is above or equal to Fishie."

        roles = self._removable_protection_roles(member, me)
        existing = await self.bot.pool.fetchrow(
            "SELECT role_ids FROM guild_protection_locks "
            "WHERE guild_id = $1 AND user_id = $2",
            guild.id,
            member.id,
        )
        saved_ids = {
            int(role_id) for role_id in (existing["role_ids"] if existing else ())
        }
        saved_ids.update(role.id for role in roles)
        await self.bot.pool.execute(
            """
            INSERT INTO guild_protection_locks(
                guild_id, user_id, role_ids, trigger
            ) VALUES ($1, $2, $3, $4)
            ON CONFLICT (guild_id, user_id) DO UPDATE
            SET role_ids = EXCLUDED.role_ids,
                trigger = EXCLUDED.trigger,
                updated_at = now()
            """,
            guild.id,
            member.id,
            sorted(saved_ids),
            trigger,
        )

        reason = f"Fishie protection triggered: {_trigger_label(trigger)}"
        roles_ok = True
        role_details: str
        if roles and not me.guild_permissions.manage_roles:
            roles_ok = False
            role_details = "Fishie is missing Manage Roles, so roles were not stripped."
        elif roles:
            try:
                await member.remove_roles(*roles, reason=reason, atomic=False)
                role_details = f"Stripped {len(roles):,} role(s)."
            except (discord.Forbidden, discord.HTTPException) as exc:
                roles_ok = False
                role_details = f"Discord rejected the role removal: {exc}"
        else:
            role_details = "The member had no removable roles."

        timeout_ok = True
        if member.bot:
            timeout_details = "Bot accounts cannot be timed out."
        elif not me.guild_permissions.moderate_members:
            timeout_ok = False
            timeout_details = "Fishie is missing Timeout Members."
        else:
            until = discord.utils.utcnow() + PROTECTION_LOCK_DURATION
            try:
                await member.timeout(until, reason=reason)
                timeout_details = (
                    f"Timed out until {discord.utils.format_dt(until, 'F')}."
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                timeout_ok = False
                timeout_details = f"Discord rejected the timeout: {exc}"

        return roles_ok and timeout_ok, f"{role_details} {timeout_details}"

    async def _contain_protection_actor(
        self, guild: discord.Guild, actor_id: int, trigger: str
    ) -> tuple[bool, str]:
        member = guild.get_member(actor_id)
        if member is None:
            try:
                member = await guild.fetch_member(actor_id)
            except discord.HTTPException:
                return False, "The actor is no longer in the server."
        return await self._lock_protection_member(guild, member, trigger)

    async def _unlock_protection_member(
        self,
        guild: discord.Guild,
        member: discord.Member,
        manager: discord.Member,
    ) -> tuple[bool, str]:
        row = await self.bot.pool.fetchrow(
            "SELECT role_ids FROM guild_protection_locks "
            "WHERE guild_id = $1 AND user_id = $2",
            guild.id,
            member.id,
        )
        saved_ids = [int(role_id) for role_id in (row["role_ids"] if row else ())]
        saved_roles = [guild.get_role(role_id) for role_id in saved_ids]
        existing_roles = [role for role in saved_roles if role is not None]

        if manager.id != guild.owner_id and any(
            role >= manager.top_role for role in existing_roles
        ):
            raise commands.BadArgument(
                "You cannot restore a saved role above or equal to your highest role."
            )

        me = guild.me
        if me is None:
            return False, "Fishie is not available in the server."
        restorable = [
            role
            for role in existing_roles
            if not role.is_default() and not role.managed and role < me.top_role
        ]
        deferred_ids = {
            role.id
            for role in existing_roles
            if not role.is_default() and not role.managed and role >= me.top_role
        }

        roles_ok = not deferred_ids
        if restorable:
            try:
                await member.add_roles(
                    *restorable,
                    reason=f"Protection unlocked by {manager} ({manager.id})",
                    atomic=False,
                )
                role_details = f"Restored {len(restorable):,} role(s)."
            except (discord.Forbidden, discord.HTTPException) as exc:
                roles_ok = False
                deferred_ids.update(role.id for role in restorable)
                role_details = f"Discord rejected the role restoration: {exc}"
        elif deferred_ids:
            role_details = (
                f"Could not restore {len(deferred_ids):,} role(s) above Fishie."
            )
        elif row is None:
            role_details = "There were no saved protection roles."
        else:
            role_details = "There were no roles to restore."

        timeout_ok = True
        if member.bot:
            timeout_details = "Bot accounts do not have timeouts."
        elif not me.guild_permissions.moderate_members:
            timeout_ok = False
            timeout_details = "Fishie is missing Timeout Members."
        else:
            try:
                await member.timeout(
                    None,
                    reason=f"Protection unlocked by {manager} ({manager.id})",
                )
                timeout_details = "Removed the protection timeout."
            except (discord.Forbidden, discord.HTTPException) as exc:
                timeout_ok = False
                timeout_details = f"Discord rejected the timeout removal: {exc}"

        if roles_ok and timeout_ok:
            await self.bot.pool.execute(
                "DELETE FROM guild_protection_locks "
                "WHERE guild_id = $1 AND user_id = $2",
                guild.id,
                member.id,
            )
        else:
            await self.bot.pool.execute(
                "UPDATE guild_protection_locks SET role_ids = $3, updated_at = now() "
                "WHERE guild_id = $1 AND user_id = $2",
                guild.id,
                member.id,
                sorted(deferred_ids),
            )
        return roles_ok and timeout_ok, f"{role_details} {timeout_details}"

    async def _record_protection_incident(
        self,
        *,
        guild_id: int,
        actor_id: int,
        target_id: int | None,
        trigger: str,
        audit_entry_id: int | None,
        mode: str,
        contained: bool,
        details: str,
    ) -> None:
        await self.bot.pool.execute(
            """
            INSERT INTO guild_protection_incidents(
                guild_id, actor_id, target_id, trigger, audit_entry_id,
                response_mode, contained, details
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (guild_id, audit_entry_id, trigger)
                WHERE audit_entry_id IS NOT NULL DO NOTHING
            """,
            guild_id,
            actor_id,
            target_id,
            trigger,
            audit_entry_id,
            mode,
            contained,
            details[:2000],
        )

    async def _send_protection_log(
        self,
        guild: discord.Guild,
        config: ProtectionConfig,
        *,
        actor_id: int,
        target_id: int | None,
        trigger: str,
        count: int,
        contained: bool,
        details: str,
        response: str | None = None,
    ) -> None:
        channel = guild.get_channel(config.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        actor = guild.get_member(actor_id) or self.bot.get_user(actor_id)
        actor_name = discord.utils.escape_mentions(
            discord.utils.escape_markdown(getattr(actor, "name", str(actor_id)))
        )
        target = f"`{target_id}`" if target_id is not None else "Not available"
        response = response or (
            "Locked"
            if contained
            else "Warning only" if config.response_mode == "warn" else "Lock failed"
        )
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("## Protection triggered"),
                discord.ui.TextDisplay(
                    f"**Trigger:** {_trigger_label(trigger)}\n"
                    f"**Actor:** {actor_name} (`{actor_id}`)\n"
                    f"**Target:** {target}\n"
                    f"**Events in window:** {count:,}\n"
                    f"**Response:** {response}"
                ),
                discord.ui.Separator(),
                discord.ui.TextDisplay(discord.utils.escape_mentions(details)[:3900]),
                discord.ui.TextDisplay(
                    f"-# {discord.utils.format_dt(discord.utils.utcnow(), 'F')}"
                ),
                accent_color=discord.Colour.red(),
            )
        )
        try:
            await channel.send(
                view=view, allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException:
            self.bot.logger.warning(
                "Could not send protection log in guild %s", guild.id
            )

    async def _restore_vanity(
        self, guild: discord.Guild, config: ProtectionConfig
    ) -> str:
        expected = config.allowed_vanity_code
        if guild.vanity_url_code == expected:
            return "The configured vanity URL was already restored."
        try:
            await guild.edit(
                vanity_code=expected or "",
                reason="Fishie protection restored the configured vanity URL",
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            return f"Vanity rollback failed: {exc}"
        return "The previous vanity URL was restored."

    async def _handle_protection_trigger(
        self,
        guild: discord.Guild,
        config: ProtectionConfig,
        *,
        actor_id: int,
        target_id: int | None,
        trigger: str,
        audit_entry_id: int | None,
        extra_details: str | None = None,
    ) -> None:
        reached, count = self._threshold_reached(guild.id, actor_id, trigger)
        if not reached:
            return
        contained = False
        details = extra_details or f"Detected {TRIGGER_LABELS[trigger].lower()}."
        if config.response_mode in {"lock", "both"}:
            contained, containment_details = await self._contain_protection_actor(
                guild, actor_id, trigger
            )
            details = f"{details}\n{containment_details}"
        await self._record_protection_incident(
            guild_id=guild.id,
            actor_id=actor_id,
            target_id=target_id,
            trigger=trigger,
            audit_entry_id=audit_entry_id,
            mode=config.response_mode,
            contained=contained,
            details=details,
        )
        if config.response_mode in {"warn", "both"} or not contained:
            await self._send_protection_log(
                guild,
                config,
                actor_id=actor_id,
                target_id=target_id,
                trigger=trigger,
                count=count,
                contained=contained,
                details=details,
            )

    @commands.Cog.listener("on_audit_log_entry_create")
    async def protection_audit_log_entry_create(
        self, entry: discord.AuditLogEntry
    ) -> None:
        if is_legacy_instance(self.bot):
            return
        guild = getattr(entry, "guild", None)
        if not isinstance(guild, discord.Guild):
            return
        if is_operational_guild(guild):
            return
        config = self._protection_configs.get(guild.id)
        if config is None:
            return
        triggers = self._classify_protection_entry(entry)
        if not triggers or self._mark_protection_entry_seen(entry.id):
            return
        actor_id = getattr(entry.user, "id", None)
        if actor_id is None:
            return
        actor_id = int(actor_id)
        vanity_change = "vanity_url" in triggers
        actor_member = entry.user if isinstance(entry.user, discord.Member) else None
        exempt = self._actor_is_exempt(guild, actor_id, config, actor_member)
        if vanity_change and (
            exempt or not config.enabled or "vanity_url" not in config.enabled_triggers
        ):
            after_code = getattr(entry.after, "vanity_url_code", guild.vanity_url_code)
            await self._set_allowed_vanity(guild.id, after_code)
        if exempt or not config.enabled:
            return
        target_id = getattr(entry.target, "id", None)
        for trigger in triggers:
            if trigger not in config.enabled_triggers:
                continue
            extra = None
            if trigger == "vanity_url":
                extra = await self._restore_vanity(guild, config)
            await self._handle_protection_trigger(
                guild,
                config,
                actor_id=actor_id,
                target_id=int(target_id) if target_id is not None else None,
                trigger=trigger,
                audit_entry_id=entry.id,
                extra_details=extra,
            )

    @commands.Cog.listener("on_message")
    async def protection_invite_message(self, message: discord.Message) -> None:
        if is_legacy_instance(self.bot):
            return
        guild = message.guild
        if (
            guild is None
            or is_operational_guild(guild)
            or message.author.id == getattr(self.bot.user, "id", 0)
        ):
            return
        config = self._protection_configs.get(guild.id)
        if (
            config is None
            or not config.enabled
            or "invites" not in config.enabled_triggers
            or not INVITE_RE.search(message.content or "")
            or self._actor_is_exempt(guild, message.author.id, config)
        ):
            return
        try:
            await message.delete()
            deletion = "The Discord invite was deleted."
        except (discord.Forbidden, discord.HTTPException) as exc:
            deletion = f"Fishie could not delete the Discord invite: {exc}"
        await self._handle_protection_trigger(
            guild,
            config,
            actor_id=message.author.id,
            target_id=message.id,
            trigger="invites",
            audit_entry_id=None,
            extra_details=deletion,
        )
