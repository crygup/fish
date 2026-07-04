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


class CommandStats(Cog):
    """View command usage statistics."""

    emoji = discord.PartialEmoji(name="\U0001f4ca")

    @commands.hybrid_group(name="stats", aliases=("commandstats",), fallback="user")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def stats(
        self,
        ctx: Context,
        *,
        user: discord.User = commands.param(
            default=commands.Author, description="User to check stats for (defaults to you)."
        ),
    ):
        """See your (or another user's) most-used commands."""
        await self._user_top(ctx, user)

    @stats.command(name="server")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def stats_server(
        self,
        ctx: Context,
        *,
        guild_id: Optional[str] = None,
    ):
        """See the most-used commands and top users in a server."""
        if guild_id:
            try:
                guild_id_int = int(guild_id)
            except ValueError:
                raise commands.BadArgument("Invalid guild ID.")
            guild = ctx.bot.get_guild(guild_id_int)
            if guild is None:
                raise commands.BadArgument("I'm not in a guild with that ID.")
        else:
            if not ctx.guild:
                raise commands.BadArgument(
                    "You must be in a server or provide a guild ID."
                )
            guild = ctx.guild

        await self._guild_stats(ctx, guild)

    @stats.command(name="command")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def stats_command(
        self,
        ctx: Context,
        command_name: str,
        *,
        user: discord.User = commands.param(
            default=commands.Author, description="User to check stats for (defaults to you)."
        ),
    ):
        """See how many times a command has been used by you or another user."""
        await self._command_count(ctx, user, command_name.strip().lower())

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    async def _command_count(
        self, ctx: Context, user: discord.User | discord.Member, command_name: str
    ) -> None:
        row = await ctx.bot.pool.fetchrow(
            "SELECT COUNT(*) AS total FROM command_logs "
            "WHERE user_id = $1 AND LOWER(command) = $2",
            user.id,
            command_name,
        )
        total = row["total"] if row else 0
        await ctx.send(
            f"**{user.display_name}** has used **{command_name}** "
            f"{total:,} time{'s' if total != 1 else ''}."
        )

    async def _user_top(
        self, ctx: Context, user: discord.User | discord.Member
    ) -> None:
        rows = await ctx.bot.pool.fetch(
            "SELECT command, COUNT(*) AS total FROM command_logs "
            "WHERE user_id = $1 "
            "GROUP BY command ORDER BY total DESC LIMIT 10",
            user.id,
        )

        if not rows:
            await ctx.send(
                f"{'You' if user.id == ctx.author.id else user.display_name} "
                f"{'have' if user.id == ctx.author.id else 'has'}n't used any commands yet!"
            )
            return

        lines = [f"**{r['total']:,}** {r['command']}" for r in rows]
        embed = discord.Embed(
            color=ctx.bot.embedcolor,
            description="\n".join(lines),
        )
        embed.set_author(
            name=f"Command stats for {user.display_name}",
            icon_url=user.display_avatar.url,
        )
        await ctx.send(embed=embed)

    async def _guild_stats(self, ctx: Context, guild: discord.Guild) -> None:
        cmd_rows = await ctx.bot.pool.fetch(
            "SELECT command, COUNT(*) AS total FROM command_logs "
            "WHERE guild_id = $1 "
            "GROUP BY command ORDER BY total DESC LIMIT 10",
            guild.id,
        )
        user_rows = await ctx.bot.pool.fetch(
            "SELECT user_id, COUNT(*) AS total FROM command_logs "
            "WHERE guild_id = $1 "
            "GROUP BY user_id ORDER BY total DESC LIMIT 10",
            guild.id,
        )

        if not cmd_rows and not user_rows:
            await ctx.send("No command data for that server yet!")
            return

        embed = discord.Embed(color=ctx.bot.embedcolor)
        embed.set_author(
            name=f"Command stats for {guild.name}",
            icon_url=guild.icon.url if guild.icon else None,
        )

        if cmd_rows:
            cmd_lines = [f"**{r['total']:,}** {r['command']}" for r in cmd_rows]
            embed.add_field(
                name="Top Commands",
                value="\n".join(cmd_lines),
                inline=True,
            )

        if user_rows:
            user_lines = []
            for r in user_rows:
                member = guild.get_member(r["user_id"])
                if member is None:
                    user = await get_or_fetch_user(ctx.bot, r["user_id"])
                    name = user.display_name if user else str(r["user_id"])
                else:
                    name = member.display_name
                user_lines.append(f"**{r['total']:,}** {name}")
            embed.add_field(
                name="Top Users",
                value="\n".join(user_lines),
                inline=True,
            )

        await ctx.send(embed=embed)


async def setup(bot: Fishie):
    await bot.add_cog(CommandStats())
