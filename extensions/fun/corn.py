from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from core import Cog
from utils import get_or_fetch_user

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


class Corn(Cog):
    """Corn reaction leaderboards."""
    emoji = discord.PartialEmoji(name="\U0001f33d")

    @commands.hybrid_group(name="corn", fallback="guild")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def corn(
        self,
        ctx: Context,
        *,
        target: Optional[str] = None,
    ):
        """See the corn leaderboard for this server, another server, or a user."""
        if target is None:
            if not ctx.guild:
                raise commands.BadArgument("Provide a guild ID, user, or use `corn global`.")
            return await self._server_leaderboard(ctx, ctx.guild)

        if target.isdigit():
            guild = ctx.bot.get_guild(int(target))
            if guild is not None:
                return await self._server_leaderboard(ctx, guild)

        try:
            user = await commands.UserConverter().convert(ctx, target)
        except commands.UserNotFound:
            raise commands.BadArgument("Could not find a user or server with that input.")
        await self._user_stats(ctx, user)

    @corn.command(name="global")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def corn_global(self, ctx: Context):
        """Global corn leaderboard — totals across all servers."""
        givers = await ctx.bot.pool.fetch(
            "SELECT giver_id, COUNT(*) AS total FROM corn_reacts "
            "GROUP BY giver_id ORDER BY total DESC LIMIT 5")
        receivers = await ctx.bot.pool.fetch(
            "SELECT receiver_id, COUNT(*) AS total FROM corn_reacts "
            "GROUP BY receiver_id ORDER BY total DESC LIMIT 5")

        if not givers and not receivers:
            await ctx.send("No corn reactions yet!"); return

        embed = discord.Embed(color=ctx.bot.embedcolor)
        embed.set_author(name="Corn Leaderboard  •  Global")

        if givers:
            lines = []
            for r in givers:
                user = await get_or_fetch_user(ctx.bot, r["giver_id"])
                name = user.display_name if user else str(r["giver_id"])
                lines.append(f"**{r['total']:,}** {name}")
            embed.add_field(name="Top Givers", value="\n".join(lines), inline=True)
        if receivers:
            lines = []
            for r in receivers:
                user = await get_or_fetch_user(ctx.bot, r["receiver_id"])
                name = user.display_name if user else str(r["receiver_id"])
                lines.append(f"**{r['total']:,}** {name}")
            embed.add_field(name="Top Receivers", value="\n".join(lines), inline=True)
        await ctx.send(embed=embed)


    async def _server_leaderboard(self, ctx: Context, guild: discord.Guild) -> None:
        givers = await ctx.bot.pool.fetch(
            "SELECT giver_id, COUNT(*) AS total FROM corn_reacts "
            "WHERE guild_id = $1 GROUP BY giver_id ORDER BY total DESC LIMIT 5",
            guild.id)
        receivers = await ctx.bot.pool.fetch(
            "SELECT receiver_id, COUNT(*) AS total FROM corn_reacts "
            "WHERE guild_id = $1 GROUP BY receiver_id ORDER BY total DESC LIMIT 5",
            guild.id)
        if not givers and not receivers:
            await ctx.send(f"No corn reactions in **{guild.name}** yet!"); return
        embed = discord.Embed(color=ctx.bot.embedcolor)
        embed.set_author(name=f"Corn Leaderboard  •  {guild.name}",
                         icon_url=guild.icon.url if guild.icon else None)
        if givers:
            lines = []
            for r in givers:
                user = guild.get_member(r["giver_id"]) or await get_or_fetch_user(ctx.bot, r["giver_id"])
                name = user.display_name if user else str(r["giver_id"])
                lines.append(f"**{r['total']:,}** {name}")
            embed.add_field(name="Top Givers", value="\n".join(lines), inline=True)
        if receivers:
            lines = []
            for r in receivers:
                user = guild.get_member(r["receiver_id"]) or await get_or_fetch_user(ctx.bot, r["receiver_id"])
                name = user.display_name if user else str(r["receiver_id"])
                lines.append(f"**{r['total']:,}** {name}")
            embed.add_field(name="Top Receivers", value="\n".join(lines), inline=True)
        await ctx.send(embed=embed)

    async def _user_stats(self, ctx: Context, user: discord.User) -> None:
        given_total = await ctx.bot.pool.fetchval(
            "SELECT COUNT(*) FROM corn_reacts WHERE giver_id = $1", user.id)
        received_total = await ctx.bot.pool.fetchval(
            "SELECT COUNT(*) FROM corn_reacts WHERE receiver_id = $1", user.id)

        if not given_total and not received_total:
            await ctx.send(f"**{user.display_name}** hasn't given or received any corns yet!"); return

        embed = discord.Embed(color=ctx.bot.embedcolor)
        embed.set_author(name=f"Corn Stats  •  {user.display_name}",
                         icon_url=user.display_avatar.url)

        given_rows = await ctx.bot.pool.fetch(
            "SELECT receiver_id, COUNT(*) AS total FROM corn_reacts "
            "WHERE giver_id = $1 GROUP BY receiver_id ORDER BY total DESC LIMIT 5",
            user.id)
        if given_rows:
            lines = []
            for r in given_rows:
                target = await get_or_fetch_user(ctx.bot, r["receiver_id"])
                name = target.display_name if target else str(r["receiver_id"])
                lines.append(f"**{r['total']:,}** {name}")
            embed.add_field(name=f"Given ({given_total:,})", value="\n".join(lines), inline=True)
        else:
            embed.add_field(name="Given (0)", value="*None*", inline=True)

        received_rows = await ctx.bot.pool.fetch(
            "SELECT giver_id, COUNT(*) AS total FROM corn_reacts "
            "WHERE receiver_id = $1 GROUP BY giver_id ORDER BY total DESC LIMIT 5",
            user.id)
        if received_rows:
            lines = []
            for r in received_rows:
                target = await get_or_fetch_user(ctx.bot, r["giver_id"])
                name = target.display_name if target else str(r["giver_id"])
                lines.append(f"**{r['total']:,}** {name}")
            embed.add_field(name=f"Received ({received_total:,})", value="\n".join(lines), inline=True)
        else:
            embed.add_field(name="Received (0)", value="*None*", inline=True)

        await ctx.send(embed=embed)


async def setup(bot: Fishie):
    await bot.add_cog(Corn())
