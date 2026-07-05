from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Callable, Optional, TypeVar

import discord
from discord.ext import commands
from typing_extensions import Annotated

from core import Cog
from utils import time as time_utils
from .honeypot import Honeypot

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


class Moderation(Honeypot, Cog):
    """Server moderation commands."""

    emoji = discord.PartialEmoji(name="\U0001f528")

    def __init__(self, bot: Fishie):
        self.bot = bot

    @commands.hybrid_command(name="ban")
    @mod_target("ban_members")
    async def ban(
        self,
        ctx: GuildContext,
        member: discord.Member,
        *,
        reason: Optional[str] = None,
    ):
        """Ban someone."""
        _check_target(ctx, member)
        await ctx.guild.ban(
            member,
            reason=f"{str(ctx.author)} (ID: {ctx.author.id}): {reason}",
            delete_message_seconds=604800,
        )
        await ctx.send(f"Banned **{member}**.")

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

    @commands.hybrid_command(name="kick")
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
