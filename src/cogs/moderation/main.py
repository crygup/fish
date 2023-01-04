from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, List, Optional, Union

import discord
from discord.ext import commands

from utils import CHECK, ActionReason, MemberID, action_test, plural, BannedMember

from ._base import CogBase

if TYPE_CHECKING:
    from cogs.context import Context


class Main(CogBase):
    @commands.group(name="ban")
    @commands.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def ban(
        self,
        ctx: Context,
        user: Union[discord.Member, discord.User],
        *,
        reason: Optional[str] = "No reason provided",
    ):
        action_test(ctx, ctx.author, user, "ban")

        await ctx.guild.ban(user, reason=reason)
        await ctx.send(f"{CHECK} Successfully banned {user}.")

    @commands.group(name="kick")
    @commands.has_permissions(kick_members=True)
    @commands.bot_has_permissions(kick_members=True)
    async def kick(
        self,
        ctx: Context,
        member: discord.Member,
        *,
        reason: Optional[str] = "No reason provided",
    ):
        action_test(ctx, ctx.author, member, "kick")

        await ctx.guild.kick(member, reason=reason)
        await ctx.send(f"{CHECK} Successfully kicked {member}.")

    @commands.command(name="multiban")
    @commands.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def multiban(
        self,
        ctx: Context,
        members: Annotated[List[discord.abc.Snowflake], commands.Greedy[MemberID]],
        *,
        reason: Annotated[Optional[str], ActionReason] = None,
    ):
        """Bans multiple members from the server.

        This only works with a user ID.
        """

        if reason is None:
            reason = f"Action done by {ctx.author} (ID: {ctx.author.id})"

        total_members = len(members)
        if total_members == 0:
            return await ctx.send("Missing members to ban.")

        confirm = await ctx.prompt(
            f"This will ban **{plural(total_members):member}**. Are you sure?"
        )
        if not confirm:
            return await ctx.send("Aborting.")

        failed = 0
        for member in members:
            try:
                await ctx.guild.ban(member, reason=reason)
            except discord.HTTPException:
                failed += 1

        await ctx.send(f"Banned {total_members - failed}/{total_members} members.")

    @commands.command(name="multikick")
    @commands.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def multikick(
        self,
        ctx: Context,
        members: Annotated[List[discord.abc.Snowflake], commands.Greedy[MemberID]],
        *,
        reason: Annotated[Optional[str], ActionReason] = None,
    ):
        """Kicks multiple members from the server.

        This only works with a user ID.
        """

        if reason is None:
            reason = f"Action done by {ctx.author} (ID: {ctx.author.id})"

        total_members = len(members)
        if total_members == 0:
            return await ctx.send("Missing members to kick.")

        confirm = await ctx.prompt(
            f"This will kick **{plural(total_members):member}**. Are you sure?"
        )
        if not confirm:
            return await ctx.send("Aborting.")

        failed = 0
        for member in members:
            try:
                await ctx.guild.kick(member, reason=reason)
            except discord.HTTPException:
                failed += 1

        await ctx.send(f"Kicked {total_members - failed}/{total_members} members.")

    @commands.command()
    @commands.guild_only()
    @commands.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def unban(
        self,
        ctx: Context,
        member: Annotated[discord.BanEntry, BannedMember],
        *,
        reason: Annotated[Optional[str], ActionReason] = None,
    ):
        """Unbans a member from the server.

        You can pass either the ID of the banned member or the Name#Discrim
        """

        if reason is None:
            reason = f"Action done by {ctx.author} (ID: {ctx.author.id})"

        await ctx.guild.unban(member.user, reason=reason)
        if member.reason:
            return await ctx.send(
                f"Unbanned {member.user} (ID: {member.user.id}), previously banned for {member.reason}."
            )

        await ctx.send(f"Unbanned {member.user} (ID: {member.user.id}).")
