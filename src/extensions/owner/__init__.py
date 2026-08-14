from __future__ import annotations

import asyncio
import datetime
import difflib
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, List, Literal, Optional, Union, cast

import discord
from discord.abc import Messageable
from discord.ext import commands

from core import Cog
from utils import (
    AllMsgbleChannels,
    fish_owner,
    fish_x,
    greenTick,
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

    def __init__(self, bot: Fishie):
        super().__init__()
        self.bot = bot
        self._last_reload: float = time.time()

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

        invite_url = discord.utils.oauth_url(
            self.bot.config["ids"]["bot_id"], permissions=self.bot.bot_permissions
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

    @commands.command(name="shutdown", aliases=("restart",))
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
