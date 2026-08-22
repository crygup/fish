from __future__ import annotations

import asyncio
import datetime
from typing import TYPE_CHECKING, Any, Callable, Optional, TypeVar, cast

import discord
from discord import app_commands
from discord.ext import commands
from typing_extensions import Annotated

from core import Cog
from extensions.context import ConfirmationView
from utils import time as time_utils

from .custom_roles import CustomRoles
from .honeypot import Honeypot
from .logger import Logger
from .mass import Mass
from .snipe import Snipe

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import GuildContext

F = TypeVar("F", bound=Callable[..., object])

MAX_MUTE_MINUTES = 20160  # 2 weeks
DEHOIST_CHARS = frozenset("!.@?#$*()_-'")


def mod_target(perm: str):
    """Decorator that checks the invoker's role hierarchy and bot permissions."""

    async def predicate(ctx: GuildContext) -> bool:
        if not ctx.guild:
            return False
        if not getattr(ctx.author.guild_permissions, perm):
            raise commands.MissingPermissions([perm])
        if not getattr(ctx.guild.me.guild_permissions, perm):  # type: ignore[union-attr]
            raise commands.BotMissingPermissions([perm])
        return True

    return commands.check(predicate)


def _check_target(ctx: GuildContext, target: discord.Member) -> None:
    cmd_name = ctx.command.name if ctx.command else "do that to"
    if target.id == ctx.author.id:
        raise commands.BadArgument(f"You cannot {cmd_name} yourself.")
    if target.top_role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
        raise commands.BadArgument(
            f"You cannot {cmd_name} someone with a higher or equal role."
        )
    if target.top_role >= ctx.guild.me.top_role:  # type: ignore[union-attr]
        raise commands.BadArgument(
            "I cannot target someone with a higher or equal role."
        )


def _parse_duration(until: datetime.datetime) -> int:
    """Return minutes from now until the given datetime, or raise."""
    delta = until - discord.utils.utcnow()
    minutes = delta.total_seconds() / 60
    if minutes <= 0:
        raise commands.BadArgument("Duration must be in the future.")
    if minutes > MAX_MUTE_MINUTES:
        raise commands.BadArgument("Duration cannot exceed 2 weeks.")
    return int(minutes)


class Moderation(Mass, Logger, Honeypot, Snipe, CustomRoles):
    """Server moderation commands"""

    emoji = discord.PartialEmoji(name="\U0001f528")

    def __init__(self, bot: Fishie):
        self.bot = bot
        self._dehoist_guilds: set[int] = set()
        self._snipe_enabled_guilds: set[int] = set()
        self._editsnipe_enabled_guilds: set[int] = set()
        self._snipe_messages = {}
        self._last_snipes = {}
        self._last_editsnipes = {}
        self._snipe_last_prune = 0.0

    async def cog_load(self) -> None:
        rows = await self.bot.pool.fetch(
            "SELECT guild_id FROM guild_settings WHERE dehoist = TRUE"
        )
        self._dehoist_guilds = {int(row["guild_id"]) for row in rows}
        await self._load_snipe_settings()

    @staticmethod
    def _can_dehoist(member: discord.Member, me: discord.Member) -> bool:
        if (
            member.bot
            or member.id in {member.guild.owner_id, me.id}
            or member.top_role >= me.top_role
        ):
            return False
        if me.guild_permissions.administrator:
            return True
        if member.guild_permissions.administrator:
            return False
        return not (member.guild_permissions.value & ~me.guild_permissions.value)

    async def _dehoist_member(
        self, member: discord.Member, *, name: str | None = None
    ) -> bool:
        me = member.guild.me
        if me is None or not me.guild_permissions.manage_nicknames:
            return False
        if not self._can_dehoist(member, me):
            return False

        name = name or member.display_name
        if name.startswith("| ") or not name or name[0] not in DEHOIST_CHARS:
            return False

        try:
            await member.edit(
                nick=f"| {name}"[:32],
                reason="Dehoist member name",
            )
        except (discord.Forbidden, discord.HTTPException):
            return False
        return True

    @commands.Cog.listener("on_member_update")
    async def dehoist_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        if after.guild.id not in self._dehoist_guilds:
            return
        username_changed = before.name != after.name and bool(after.nick)
        await self._dehoist_member(
            after,
            name=after.name if username_changed else None,
        )

    async def _scan_dehoist(
        self, guild: discord.Guild, message: discord.Message
    ) -> tuple[int, int]:
        await message.edit(content="Dehoist scan is fetching the full member list...")
        try:
            members = [member async for member in guild.fetch_members(limit=None)]
        except discord.HTTPException:
            members = list(guild.members)
        total = len(members)
        processed = 0
        changed = 0
        lock = asyncio.Lock()
        semaphore = asyncio.Semaphore(3)
        last_update = 0.0

        async def update_progress(force: bool = False) -> None:
            nonlocal last_update
            now = asyncio.get_running_loop().time()
            if not force and processed < total and now - last_update < 2:
                return
            last_update = now
            await message.edit(
                content=(
                    f"Dehoist scan in progress: **{processed:,}/{total:,}** "
                    f"members checked • **{changed:,}** updated."
                )
            )

        async def process(member: discord.Member) -> None:
            nonlocal processed, changed
            async with semaphore:
                updated = await self._dehoist_member(member)
            async with lock:
                processed += 1
                if updated:
                    changed += 1
                await update_progress()

        await asyncio.gather(*(process(member) for member in members))
        await update_progress(force=True)
        return total, changed

    @commands.hybrid_command(name="dehoist")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def dehoist(self, ctx: GuildContext) -> None:
        """Toggle automatic dehoisting for members in this server."""
        guild = ctx.guild
        if guild.me is None or not guild.me.guild_permissions.manage_nicknames:
            raise commands.BotMissingPermissions(["manage_nicknames"])

        if guild.id in self._dehoist_guilds:
            await self.bot.pool.execute(
                "UPDATE guild_settings SET dehoist = FALSE WHERE guild_id = $1",
                guild.id,
            )
            self._dehoist_guilds.remove(guild.id)
            await ctx.send("Dehoist is now disabled for this server.")
            return

        await self.bot.pool.execute(
            """
            INSERT INTO guild_settings (guild_id, dehoist)
            VALUES ($1, TRUE)
            ON CONFLICT (guild_id) DO UPDATE SET dehoist = TRUE
            """,
            guild.id,
        )
        self._dehoist_guilds.add(guild.id)
        view = ConfirmationView(
            timeout=60,
            author_id=ctx.author.id,
            ctx=ctx,
            delete_after=False,
            confirm_label="Scan all members",
            cancel_label="Only future updates",
        )
        view.message = await ctx.send(
            "Dehoist is enabled. Do you want to scan existing members now?",
            view=view,
        )
        await view.wait()
        if view.value is not True:
            await ctx.send(
                "Dehoist is enabled for future name updates. No member scan was run."
            )
            return

        total, changed = await self._scan_dehoist(guild, view.message)
        await view.message.edit(
            content=(
                f"Dehoist is enabled. Checked **{total:,}** members and updated "
                f"**{changed:,}** name{'s' if changed != 1 else ''}."
            ),
            view=None,
        )

    async def _resolve_command_for_config(
        self, ctx: GuildContext, command_name: str
    ) -> commands.Command:
        command = self.bot.get_command(command_name.casefold())
        if command is None:
            raise commands.BadArgument(
                f"I couldn't find a command named `{command_name}`."
            )
        if self.bot._command_disable_excluded(command):
            raise commands.BadArgument(
                f"The `{command.qualified_name}` command cannot be disabled."
            )

        setattr(cast(Any, ctx), "_skip_command_disable_check", True)
        try:
            can_use = await command.can_run(ctx)
        except commands.CommandError:
            can_use = False
        finally:
            delattr(ctx, "_skip_command_disable_check")
        if not can_use:
            raise commands.BadArgument(
                f"You cannot use `{command.qualified_name}`, so you cannot configure it."
            )
        return command

    @commands.hybrid_command(name="disable")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def disable(
        self,
        ctx: GuildContext,
        command_name: str,
        channel: Optional[discord.TextChannel] = None,
    ):
        """Disable a command in this channel or the whole server."""
        command = await self._resolve_command_for_config(ctx, command_name)
        if channel is not None and channel.guild.id != ctx.guild.id:
            raise commands.BadArgument("The channel must belong to this server.")

        channel_id = channel.id if channel is not None else 0
        await self.bot.pool.execute(
            """
            INSERT INTO command_disables (guild_id, command, channel_id)
            VALUES ($1, $2, $3)
            ON CONFLICT (guild_id, command, channel_id) DO NOTHING
            """,
            ctx.guild.id,
            command.qualified_name.casefold(),
            channel_id,
        )
        self.bot.db_cache.add_disabled_command(
            ctx.guild.id, command.qualified_name.casefold(), channel_id
        )
        scope = f"in {channel.mention}" if channel is not None else "in this server"
        await ctx.send(f"Disabled `{command.qualified_name}` {scope}.")

    @commands.command(name="enable")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def enable(
        self,
        ctx: GuildContext,
        command_name: str,
        channel: Optional[discord.TextChannel] = None,
    ):
        """Enable a command in this channel or the whole server."""
        command = await self._resolve_command_for_config(ctx, command_name)
        if channel is not None and channel.guild.id != ctx.guild.id:
            raise commands.BadArgument("The channel must belong to this server.")

        channel_id = channel.id if channel is not None else 0
        await self.bot.pool.execute(
            "DELETE FROM command_disables WHERE guild_id = $1 AND command = $2 AND channel_id = $3",
            ctx.guild.id,
            command.qualified_name.casefold(),
            channel_id,
        )
        self.bot.db_cache.remove_disabled_command(
            ctx.guild.id, command.qualified_name.casefold(), channel_id
        )
        scope = f"in {channel.mention}" if channel is not None else "in this server"
        await ctx.send(f"Enabled `{command.qualified_name}` {scope}.")

    @staticmethod
    def _lockable_channel(channel: discord.abc.GuildChannel) -> bool:
        return isinstance(channel, (discord.TextChannel, discord.ForumChannel))

    async def _resolve_lock_channels(
        self, ctx: GuildContext, target: str | None
    ) -> list[discord.abc.GuildChannel]:
        guild = ctx.guild
        if target is None or not target.strip():
            channel = ctx.channel
            if not isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
                raise commands.BadArgument(
                    "Lock and unlock can only be used in text or forum channels."
                )
            return [channel]

        value = target.strip()
        if value.casefold() == "all":
            return [
                channel for channel in guild.channels if self._lockable_channel(channel)
            ]

        try:
            channel = await commands.TextChannelConverter().convert(ctx, value)
        except commands.BadArgument:
            try:
                channel = await commands.ForumChannelConverter().convert(ctx, value)
            except commands.BadArgument as error:
                raise commands.BadArgument(
                    "Choose a text/forum channel, `all`, or leave it blank for this channel."
                ) from error
        if channel.guild.id != guild.id:
            raise commands.BadArgument("The channel must belong to this server.")
        return [channel]

    async def _lock_channel(
        self, ctx: GuildContext, channel: discord.abc.GuildChannel
    ) -> bool:
        existing = await self.bot.pool.fetchrow(
            "SELECT 1 FROM channel_locks WHERE guild_id = $1 AND channel_id = $2",
            channel.guild.id,
            channel.id,
        )
        if existing:
            return False

        role = channel.guild.default_role
        previous = channel.overwrites_for(role)
        allow, deny = previous.pair()
        had_overwrite = role in channel.overwrites
        await self.bot.pool.execute(
            """
            INSERT INTO channel_locks
                (guild_id, channel_id, had_overwrite, allow_bits, deny_bits, locked_by)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            channel.guild.id,
            channel.id,
            had_overwrite,
            allow.value,
            deny.value,
            ctx.author.id,
        )
        try:
            previous.send_messages = False
            await channel.set_permissions(
                role,
                overwrite=previous,
                reason=f"Channel locked by {ctx.author} (ID: {ctx.author.id})",
            )
        except Exception:
            await self.bot.pool.execute(
                "DELETE FROM channel_locks WHERE guild_id = $1 AND channel_id = $2",
                channel.guild.id,
                channel.id,
            )
            raise
        return True

    async def _unlock_channel(self, channel: discord.abc.GuildChannel) -> bool:
        row = await self.bot.pool.fetchrow(
            """
            SELECT had_overwrite, allow_bits, deny_bits
            FROM channel_locks
            WHERE guild_id = $1 AND channel_id = $2
            """,
            channel.guild.id,
            channel.id,
        )
        if row is None:
            return False

        role = channel.guild.default_role
        overwrite = None
        if row["had_overwrite"]:
            overwrite = discord.PermissionOverwrite.from_pair(
                discord.Permissions(int(row["allow_bits"])),
                discord.Permissions(int(row["deny_bits"])),
            )
        await channel.set_permissions(
            role,
            overwrite=overwrite,
            reason="Channel unlocked and previous permissions restored",
        )
        await self.bot.pool.execute(
            "DELETE FROM channel_locks WHERE guild_id = $1 AND channel_id = $2",
            channel.guild.id,
            channel.id,
        )
        return True

    @commands.hybrid_command(name="lock")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_channels=True)
    @commands.bot_has_guild_permissions(manage_channels=True)
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    @app_commands.describe(
        target="A channel mention/name/ID, or `all`. Leave blank for this channel."
    )
    async def lock(self, ctx: GuildContext, *, target: str | None = None) -> None:
        """Lock a text or forum channel and remember its previous permissions."""
        channels = await self._resolve_lock_channels(ctx, target)
        changed = 0
        for channel in channels:
            if await self._lock_channel(ctx, channel):
                changed += 1
        if target and target.strip().casefold() == "all":
            await ctx.send(
                f"Locked **{changed}** channel{'s' if changed != 1 else ''}."
            )
        elif changed:
            await ctx.send(f"Locked {channels[0].mention}.")
        else:
            await ctx.send(f"{channels[0].mention} is already locked.")

    @commands.hybrid_command(name="unlock")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_channels=True)
    @commands.bot_has_guild_permissions(manage_channels=True)
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    @app_commands.describe(
        target="A channel mention/name/ID, or `all`. Leave blank for this channel."
    )
    async def unlock(self, ctx: GuildContext, *, target: str | None = None) -> None:
        """Restore the permissions saved by lock for a text or forum channel."""
        channels = await self._resolve_lock_channels(ctx, target)
        changed = 0
        for channel in channels:
            if await self._unlock_channel(channel):
                changed += 1
        if target and target.strip().casefold() == "all":
            await ctx.send(
                f"Unlocked **{changed}** channel{'s' if changed != 1 else ''} and restored their permissions."
            )
        elif changed:
            await ctx.send(
                f"Unlocked {channels[0].mention} and restored its permissions."
            )
        else:
            await ctx.send(f"{channels[0].mention} does not have a saved lock.")

    @commands.command(name="ban")
    @mod_target("ban_members")
    async def ban(
        self,
        ctx: GuildContext,
        user: discord.User,
        *,
        reason: Optional[str] = None,
    ):
        """Ban a server member or a user who is not currently in the server."""
        member = ctx.guild.get_member(user.id)
        if member is not None:
            _check_target(ctx, member)
        await ctx.guild.ban(
            user,
            reason=f"{str(ctx.author)} (ID: {ctx.author.id}): {reason}",
            delete_message_seconds=604800,
        )
        await ctx.send(f"Banned **{user}**.")

    @commands.hybrid_command(name="softban")
    @mod_target("ban_members")
    async def softban(
        self,
        ctx: GuildContext,
        member: discord.Member,
        *,
        reason: Optional[str] = None,
    ):
        """Ban then immediately unban a member to delete their messages."""

        _check_target(ctx, member)
        reason = (
            f"{str(ctx.author)} (ID: {ctx.author.id}): {reason}"
            if reason
            else f"{str(ctx.author)} (ID: {ctx.author.id})"
        )

        await ctx.guild.ban(member, reason=reason, delete_message_seconds=604800)
        await ctx.guild.unban(member, reason=reason)
        await ctx.send(f"Softbanned **{member}**.")

    @commands.hybrid_command(name="mute", aliases=("timeout",))
    @mod_target("moderate_members")
    async def mute(
        self,
        ctx: GuildContext,
        member: discord.Member,
        *,
        when: Annotated[
            time_utils.FriendlyTimeResult,
            time_utils.UserFriendlyTime(commands.clean_content, default="1h"),
        ],
    ):
        """Timeout a member. Examples: 10m, 1h, 2d, 1h30m. Max 2 weeks."""
        _check_target(ctx, member)
        if member.is_timed_out():
            raise commands.BadArgument("That member is already muted.")

        minutes = _parse_duration(when.dt)
        until = discord.utils.utcnow() + datetime.timedelta(minutes=minutes)
        reason = (
            f"{str(ctx.author)} (ID: {ctx.author.id}): {when.arg}"
            if bool(when.arg)
            else f"{str(ctx.author)} (ID: {ctx.author.id})"
        )
        await member.timeout(until, reason=reason)
        await ctx.send(
            f"Muted **{member}** for {time_utils.human_timedelta(until, suffix=False)}."
            + (f" Reason: {when.arg}" if when.arg else "")
        )

    @commands.hybrid_command(name="unmute")
    @mod_target("moderate_members")
    async def unmute(self, ctx: GuildContext, member: discord.Member):
        """Remove someone's timeout."""
        if not member.is_timed_out():
            raise commands.BadArgument("That member is not muted.")
        await member.timeout(None, reason=f"{str(ctx.author)} (ID: {ctx.author.id})")
        await ctx.send(f"Unmuted **{member}**.")

    @commands.command(name="kick")
    @mod_target("kick_members")
    async def kick(
        self,
        ctx: GuildContext,
        member: discord.Member,
        *,
        reason: Optional[str] = None,
    ):
        """Kick someone."""
        _check_target(ctx, member)
        reason = (
            f"{str(ctx.author)} (ID: {ctx.author.id}): {reason}"
            if reason
            else f"{str(ctx.author)} (ID: {ctx.author.id})"
        )
        await ctx.guild.kick(member, reason=reason)
        await ctx.send(f"Kicked **{member}**.")

    @commands.hybrid_command(name="unban")
    @mod_target("ban_members")
    async def unban(
        self,
        ctx: GuildContext,
        user: discord.User,
        *,
        reason: Optional[str] = None,
    ):
        """Unban someone."""
        reason = (
            f"{str(ctx.author)} (ID: {ctx.author.id}): {reason}"
            if reason
            else f"{str(ctx.author)} (ID: {ctx.author.id})"
        )
        await ctx.guild.unban(user, reason=reason)
        await ctx.send(f"Unbanned **{user}**.")


async def setup(bot: Fishie):
    await bot.add_cog(Moderation(bot))
