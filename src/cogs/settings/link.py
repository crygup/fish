from __future__ import annotations

import re
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from utils import UnknownAccount, SteamIDConverter

from ._base import CogBase

if TYPE_CHECKING:
    from cogs.context import Context


class LinkCog(CogBase):
    @commands.command(name="accounts")
    async def accounts(self, ctx: Context, *, user: discord.User = commands.Author):
        """Shows your linked accounts"""
        accounts = await ctx.bot.pool.fetchrow(
            "SELECT * FROM accounts WHERE user_id = $1", user.id
        )

        if not accounts:
            await ctx.send(f"{str(user)} has no linked accounts.")
            return

        embed = discord.Embed()
        embed.set_author(
            name=f"{user.display_name} - Connected accounts",
            icon_url=user.display_avatar.url,
        )

        embed.add_field(name="Last.fm", value=accounts["lastfm"] or "Not set")
        embed.add_field(name="osu!", value=accounts["osu"] or "Not set")
        embed.add_field(name="Steam", value=accounts["steam"] or "Not set")
        embed.add_field(name="Roblox", value=accounts["roblox"] or "Not set")

        await ctx.send(embed=embed, check_ref=True)

    @commands.group(name="link", aliases=("set",), invoke_without_command=True)
    async def set(self, ctx: Context):
        """Sets your profile for a site"""
        await ctx.send_help(ctx.command)

    @set.command(name="lastfm", aliases=["fm"])
    async def set_lastfm(self, ctx: Context, username: str):
        """Sets your last.fm account"""
        if not re.fullmatch(r"[a-zA-Z0-9_-]{2,15}", username):
            raise UnknownAccount("Invalid username.")

        await self.link_method(ctx, ctx.author.id, "lastfm", username)

    @set.command(name="osu")
    async def set_osu(self, ctx: Context, *, username: str):
        """Sets your osu! account"""
        if not re.fullmatch(r"[a-zA-Z0-9_\s-]{2,16}", username):
            raise UnknownAccount("Invalid username.")

        await self.link_method(ctx, ctx.author.id, "osu", username)

    @set.command(name="steam")
    async def set_steam(self, ctx: Context, username: str):
        """Sets your steam account"""

        await self.link_method(
            ctx, ctx.author.id, "steam", str(SteamIDConverter(username))
        )

    @set.command(name="roblox")
    async def set_roblox(self, ctx: Context, *, username: str):
        """Sets your roblox account"""

        await self.link_method(ctx, ctx.author.id, "roblox", username)

    @set.command(name="genshin")
    async def set_genshin(self, ctx: Context, *, user_id: str):
        """Sets your genshin account"""
        if not re.match(r"[0-9]{4,15}", user_id):
            raise UnknownAccount("Invalid UID.")

        await self.link_method(ctx, ctx.author.id, "genshin", user_id)

    @commands.group(name="unlink", invoke_without_command=True)
    async def unlink(self, ctx: Context):
        """Unlinks your account"""
        await ctx.send_help(ctx.command)

    @unlink.command(name="lastfm")
    async def unlink_lastfm(self, ctx: Context):
        """Unlinks your last.fm account"""
        await self.unlink_method(ctx, ctx.author.id, "lastfm")

    @unlink.command(name="osu")
    async def unlink_osu(self, ctx: Context):
        """Unlinks your osu account"""
        await self.unlink_method(ctx, ctx.author.id, "osu")

    @unlink.command(name="steam")
    async def unlink_steam(self, ctx: Context):
        """Unlinks your steam account"""
        await self.unlink_method(ctx, ctx.author.id, "steam")

    @unlink.command(name="roblox")
    async def unlink_roblox(self, ctx: Context):
        """Unlinks your roblox account"""
        await self.unlink_method(ctx, ctx.author.id, "roblox")

    @unlink.command(name="genshin")
    async def unlink_genshin(self, ctx: Context):
        """Unlinks your genshin account"""
        await self.unlink_method(ctx, ctx.author.id, "genshin")
