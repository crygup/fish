from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Callable, Optional, TypeVar

import discord
from discord.ext import commands
from typing_extensions import Annotated
from discord import app_commands

from core import Cog
from utils import time as time_utils
from .honeypot import Honeypot
from .logger import Logger

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import GuildContext

F = TypeVar("F", bound=Callable[..., object])

MAX_MUTE_MINUTES = 20160  # 2 weeks


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


class Moderation(Logger, Honeypot):
    """Server moderation commands."""

    emoji = discord.PartialEmoji(name="\U0001f528")

    def __init__(self, bot: Fishie):
        self.bot = bot

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

        setattr(ctx, "_skip_command_disable_check", True)
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
