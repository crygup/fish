from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands
from .view import ActionView
from ._base import CogBase

if TYPE_CHECKING:
    from cogs.context import Context


class Mass(CogBase):
    @commands.group(
        name="massban", extras={"BPerms": ["Ban Members"], "UPerms": ["Ban Members"]}
    )
    @commands.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def massban(
        self,
        ctx: Context,
    ):
        await ctx.send(view=ActionView(ctx, "ban"))

    @commands.group(
        name="masskick", extras={"BPerms": ["Kick Members"], "UPerms": ["Kick Members"]}
    )
    @commands.has_permissions(kick_members=True)
    @commands.bot_has_permissions(kick_members=True)
    async def masskick(
        self,
        ctx: Context,
    ):
        await ctx.send(view=ActionView(ctx, "kick"))

    @commands.group(
        name="massnick",
        extras={"BPerms": ["Manage Nicknames"], "UPerms": ["Manage Nicknames"]},
    )
    @commands.has_permissions(manage_nicknames=True)
    @commands.bot_has_permissions(manage_nicknames=True)
    async def massnick(
        self,
        ctx: Context,
    ):
        await ctx.send(view=ActionView(ctx, "nick"))
