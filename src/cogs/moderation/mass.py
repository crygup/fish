from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from utils import BaseCog

from .view import ActionView

if TYPE_CHECKING:
    from cogs.context import Context


class Mass(BaseCog):
    @commands.group(name="mass", invoke_without_command=True)
    async def mass(self, ctx: Context):
        await ctx.send_help(ctx.command)

    @mass.command(
        name="mass",
        extras={"BPerms": ["Ban Members"], "UPerms": ["Ban Members"]},
    )
    @commands.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def mass_ban(
        self,
        ctx: Context,
    ):
        await ctx.send(view=ActionView(ctx, "ban"))

    @mass.group(
        name="kick",
        extras={"BPerms": ["Kick Members"], "UPerms": ["Kick Members"]},
    )
    @commands.has_permissions(kick_members=True)
    @commands.bot_has_permissions(kick_members=True)
    async def mass_kick(
        self,
        ctx: Context,
    ):
        await ctx.send(view=ActionView(ctx, "kick"))

    @mass.group(
        name="nick",
        extras={"BPerms": ["Manage Nicknames"], "UPerms": ["Manage Nicknames"]},
    )
    @commands.has_permissions(manage_nicknames=True)
    @commands.bot_has_permissions(manage_nicknames=True)
    async def mass_nick(
        self,
        ctx: Context,
    ):
        await ctx.send(view=ActionView(ctx, "nick"))
