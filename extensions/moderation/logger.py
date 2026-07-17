from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Sequence, cast

import discord
from discord.ext import commands

from core import Cog

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import GuildContext


LOGGER_EVENTS: dict[str, str] = {
    "avatar": "Avatar changes",
    "member": "Member joins, leaves, and name changes",
    "channel": "Channel changes",
    "role": "Role changes",
    "server": "Server changes",
    "moderation": "Bans, kicks, unbans, and timeouts",
    "message": "Message edits, deletions, and purges",
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


class _LoggerView(discord.ui.LayoutView):
    def __init__(self, ctx: GuildContext, *, timeout: float = 300) -> None:
        super().__init__(timeout=timeout)
        self.ctx = ctx
        self.author_id = ctx.author.id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message(
            "Only the person who opened the logger controls can use these buttons.",
            ephemeral=True,
        )
        return False


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
        choose_text = discord.ui.Button(
            label="Enter channel name/ID", style=discord.ButtonStyle.secondary
        )
        choose_text.callback = self._open_modal
        current = discord.ui.Button(
            label="Set to current channel", style=discord.ButtonStyle.success
        )
        current.callback = self._set_current
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(f"## Configure {LOGGER_EVENTS[event]}"),
                discord.ui.TextDisplay(
                    "Use the channel picker below, enter a channel name/ID, or set this command's channel."
                ),
                discord.ui.ActionRow(LoggerChannelSelect(self)),
                discord.ui.ActionRow(choose_text, current),
            )
        )

    async def _open_modal(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            LoggerChannelModal(self.cog, self.ctx, self.event)
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
        await interaction.response.send_message(
            f"{LOGGER_EVENTS[self.event]} will now be logged in {channel.mention}.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class LoggerConfigureButton(discord.ui.Button):
    def __init__(self, cog: Logger, ctx: GuildContext, event: str) -> None:
        self.cog = cog
        self.ctx = ctx
        self.event = event
        super().__init__(label="Choose channel", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            view=LoggerChannelPickerView(self.cog, self.ctx, self.event)
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
        await interaction.response.send_message(
            f"{LOGGER_EVENTS[self.event]} will now be logged in {channel.mention}.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
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

    _pending_purges: dict[tuple[int, int], float]
    _webhook_locks: dict[tuple[int, str], asyncio.Lock]
    _logged_webhook_creations: dict[tuple[int, int, str], float]

    def _webhook_lock(self, guild_id: int, event: str) -> asyncio.Lock:
        locks: dict[tuple[int, str], asyncio.Lock] = getattr(self, "_webhook_locks", {})
        self._webhook_locks = locks
        return locks.setdefault((guild_id, event), asyncio.Lock())

    async def _audit_details(
        self,
        guild: discord.Guild,
        action: discord.AuditLogAction,
        target_id: int | None = None,
    ) -> tuple[discord.User | discord.Member, str | None] | None:
        me = guild.me
        if me is None or not me.guild_permissions.view_audit_log:
            return None

        for attempt in range(2):
            try:
                async for entry in guild.audit_logs(limit=8, action=action):
                    entry_target_id = getattr(entry.target, "id", None)
                    if target_id is not None and entry_target_id != target_id:
                        continue
                    age = (discord.utils.utcnow() - entry.created_at).total_seconds()
                    if -2 <= age <= 15:
                        if entry.user is not None:
                            return entry.user, entry.reason
            except (discord.Forbidden, discord.HTTPException):
                return None
            if attempt == 0:
                await asyncio.sleep(0.25)
        return None

    async def _recent_webhook_change(self, channel: discord.TextChannel) -> (
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
                    target_channel_id = getattr(target, "channel_id", None)
                    target_channel = getattr(target, "channel", None)
                    if target_channel_id is None and target_channel is not None:
                        target_channel_id = getattr(target_channel, "id", None)
                    if target_channel_id is None:
                        for state in (entry.before, entry.after):
                            changed_channel = getattr(state, "channel", None)
                            if changed_channel is not None:
                                target_channel_id = getattr(changed_channel, "id", None)
                                if target_channel_id is not None:
                                    break
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
        target: discord.User | discord.Member | None = None,
        actor_label: str = "Changed by",
    ) -> None:
        if target is not None and actor_label == "Moderator":
            embed.add_field(
                name="Target",
                value=f"{target.name}\n{target.mention}",
                inline=True,
            )
        details = await self._audit_details(guild, action, target_id)
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
        configured = {str(row["event"]): int(row["channel_id"]) for row in rows}
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

    async def _set_logger_channel(
        self,
        ctx: GuildContext,
        event: str,
        channel: discord.TextChannel,
        *,
        announce: bool = True,
        actor_id: int | None = None,
    ) -> None:
        event = event.casefold()
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

        event = event.casefold()
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
        rows = await self.bot.pool.fetch(
            "SELECT event, channel_id FROM guild_log_channels WHERE guild_id = $1",
            ctx.guild.id,
        )
        configured = {str(row["event"]): int(row["channel_id"]) for row in rows}
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
        """Log member joins, leaves, and name changes."""
        await self._set_logger_channel(ctx, "member", channel)

    @logger.command(name="channel", aliases=("channels",))
    async def logger_channel(
        self, ctx: GuildContext, channel: discord.TextChannel
    ) -> None:
        """Log channel creation, updates, and deletion."""
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
        """Log server name and icon changes."""
        await self._set_logger_channel(ctx, "server", channel)

    @logger.command(name="moderation", aliases=("mod", "bans"))
    async def logger_moderation(
        self, ctx: GuildContext, channel: discord.TextChannel
    ) -> None:
        """Log member bans and unbans."""
        await self._set_logger_channel(ctx, "moderation", channel)

    @logger.command(name="message", aliases=("messages",))
    async def logger_message(
        self, ctx: GuildContext, channel: discord.TextChannel
    ) -> None:
        """Log message edits, deletions, and bulk purges."""
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
        audit_target: discord.User | discord.Member | None = None,
        audit_actor_label: str = "Changed by",
    ) -> None:
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
            try:
                await webhook.send(
                    embed=embed,
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
                    embed=embed,
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
    def _add_item_id(
        embed: discord.Embed,
        item: (
            discord.User
            | discord.Member
            | discord.abc.GuildChannel
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
        if not avatar_changed and not profile_changed:
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

    @commands.Cog.listener("on_member_join")
    async def logger_member_join(self, member: discord.Member) -> None:
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

    @commands.Cog.listener("on_guild_channel_update")
    async def logger_channel_update(
        self,
        before: discord.abc.GuildChannel,
        after: discord.abc.GuildChannel,
    ) -> None:
        before_category = before.category.id if before.category else None
        after_category = after.category.id if after.category else None
        if before.name == after.name and before_category == after_category:
            return
        embed = self._embed(
            "Channel updated",
            f"{after.mention} was updated.",
            color=discord.Colour.orange(),
        )
        embed.add_field(
            name="Before",
            value=f"{_display(before.name)} / {_display(before.category)}",
        )
        embed.add_field(
            name="After", value=f"{_display(after.name)} / {_display(after.category)}"
        )
        self._add_item_id(embed, after)
        await self._emit_logger(
            after.guild,
            "channel",
            embed,
            audit_action=discord.AuditLogAction.channel_update,
            audit_target_id=after.id,
        )

    @commands.Cog.listener("on_webhooks_update")
    async def logger_webhooks_update(self, channel: discord.abc.GuildChannel) -> None:
        """Log webhook creations and deletions in the channel logger."""
        if not isinstance(channel, discord.TextChannel):
            return
        details = await self._recent_webhook_change(channel)
        if details is None:
            return
        audit_action, webhook, actor, reason = details
        deleted = audit_action is discord.AuditLogAction.webhook_delete
        action = "deleted" if deleted else "created"
        embed = self._embed(
            f"Webhook {action}",
            f"A webhook was {action} in {channel.mention}.",
            color=discord.Colour.red() if deleted else discord.Colour.green(),
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
            name="Deleted by" if deleted else "Created by",
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
        if before.name == after.name and before.permissions == after.permissions:
            return
        embed = self._embed(
            "Role updated",
            f"{after.mention} was updated.",
            color=discord.Colour.orange(),
        )
        embed.add_field(name="Before", value=_display(before.name))
        embed.add_field(name="After", value=_display(after.name))
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
        if before.name == after.name and _asset_key(before.icon) == _asset_key(
            after.icon
        ):
            return
        embed = self._embed(
            "Server updated",
            None,
            color=discord.Colour.orange(),
        )
        embed.add_field(name="Before", value=_display(before.name))
        embed.add_field(name="After", value=_display(after.name))
        if after.icon:
            embed.set_thumbnail(url=after.icon.url)
        self._add_item_id(embed, after)
        await self._emit_logger(
            after,
            "server",
            embed,
            audit_action=discord.AuditLogAction.guild_update,
            audit_target_id=after.id,
        )

    @commands.Cog.listener("on_message_edit")
    async def logger_message_edit(
        self, before: discord.Message, after: discord.Message
    ) -> None:
        if before.guild is None or before.content == after.content:
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
        embed.add_field(
            name="Before", value=_display(before.content) or "None", inline=False
        )
        embed.add_field(
            name="After", value=_display(after.content) or "None", inline=False
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
                value=f"{cached.author.mention} (`ID: {cached.author.id})`",
            )
            if cached.content:
                embed.add_field(
                    name="Content", value=_display(cached.content), inline=False
                )
        self._add_item_id(embed, discord.Object(payload.message_id))
        await self._emit_logger(
            guild,
            "message",
            embed,
            audit_action=discord.AuditLogAction.message_delete,
            audit_target_id=payload.channel_id,
        )

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
            audit_target_id=payload.channel_id,
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
            audit_target_id=channel_id,
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
