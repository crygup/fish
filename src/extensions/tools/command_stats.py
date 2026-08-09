from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional, cast

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
            default=commands.Author,
            description="User to check stats for (defaults to you).",
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

    @stats.command(name="global")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def stats_global(self, ctx: Context):
        """See the most-used commands and top users globally."""
        cmd_rows = await ctx.bot.pool.fetch(
            "SELECT command, COUNT(*) AS total FROM command_logs "
            "GROUP BY command ORDER BY total DESC LIMIT 10"
        )
        user_rows = await ctx.bot.pool.fetch(
            "SELECT user_id, COUNT(*) AS total FROM command_logs "
            "GROUP BY user_id ORDER BY total DESC LIMIT 10"
        )
        if not cmd_rows and not user_rows:
            await ctx.send("No command data yet!")
            return
        embed = discord.Embed(color=ctx.bot.embedcolor)
        embed.set_author(name="Command Stats")
        if cmd_rows:
            embed.add_field(
                name="Top Commands",
                value="\n".join(f"**{r['total']:,}** {r['command']}" for r in cmd_rows),
                inline=True,
            )
        if user_rows:
            lines = []
            for r in user_rows:
                user = await get_or_fetch_user(ctx.bot, r["user_id"])
                lines.append(
                    f"**{r['total']:,}** {user.name if user else r['user_id']}"
                )
            embed.add_field(name="Top Users", value="\n".join(lines), inline=True)
        await ctx.send(embed=embed)

    @stats.command(name="command")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def stats_command(
        self,
        ctx: Context,
        command_name: str,
        *,
        user: Optional[discord.User] = None,
    ):
        """See how many times a command has been used (global by default, or per user)."""
        await self._command_count(ctx, user, command_name.strip().lower())

    @stats.command(name="download", aliases=("downloads", "dl"))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def stats_download(
        self,
        ctx: Context,
        *,
        user: discord.User = commands.param(
            default=commands.Author,
            description="User to check download stats for (defaults to you).",
        ),
    ) -> None:
        """See total downloads and the sites a user downloads from most."""
        if user.id != ctx.author.id and not ctx.bot.db_cache.user_history_is_public(
            user.id
        ):
            await ctx.send("That user's download statistics are private.")
            return

        rows = await ctx.bot.pool.fetch(
            "SELECT site, SUM(downloads) AS downloads FROM download_stats "
            "WHERE user_id = $1 GROUP BY site ORDER BY downloads DESC, site ASC",
            user.id,
        )
        if not rows:
            subject = "You" if user.id == ctx.author.id else user.display_name
            await ctx.send(f"{subject} have not downloaded any media yet.")
            return

        total = sum(int(row["downloads"]) for row in rows)
        site_labels = {
            "instagram": "Instagram",
            "tiktok": "TikTok",
            "twitter": "Twitter",
            "youtube": "YouTube",
            "twitch": "Twitch",
            "reddit": "Reddit",
            "threads": "Threads",
            "facebook": "Facebook",
            "pixiv": "Pixiv",
            "tumblr": "Tumblr",
            "pinterest": "Pinterest",
            "soundcloud": "SoundCloud",
            "klipy": "Klipy",
            "tenor": "Tenor",
        }
        sites = "\n".join(
            f"**{site_labels.get(str(row['site']), str(row['site']).title())}** "
            f"({int(row['downloads']):,})"
            for row in rows
        )
        embed = discord.Embed(
            title=f"Download stats for {user.display_name}",
            description=f"**Total downloads:** {total:,}\n\n{sites}",
            color=ctx.bot.embedcolor,
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        await ctx.send(embed=embed)

    @cast(Any, stats.group)(name="emoji", invoke_without_command=True)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def stats_emoji(self, ctx: Context, target: Optional[str] = None) -> None:
        """Show the existing server or user emoji statistics."""
        emoji_cog: Any = ctx.bot.get_cog("Emojis")
        if emoji_cog is None:
            await ctx.send("Emoji statistics are not available right now.")
            return
        await emoji_cog.send_emoji_stats(ctx, target)

    @stats_emoji.command(name="global")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def stats_emoji_global(
        self, ctx: Context, target: Optional[str] = None
    ) -> None:
        """Show a user's existing emoji statistics across all servers."""
        emoji_cog: Any = ctx.bot.get_cog("Emojis")
        if emoji_cog is None:
            await ctx.send("Emoji statistics are not available right now.")
            return
        await emoji_cog.send_global_emoji_stats(ctx, target)

    @stats.command(name="tictactoe", aliases=("ttt",))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def stats_tictactoe(self, ctx: Context) -> None:
        """Show the global Tic-Tac-Toe leaderboards."""
        fun_cog: Any = ctx.bot.get_cog("Fun")
        controller: Any = getattr(fun_cog, "_tictactoe_controller", None)
        if controller is None:
            await ctx.send("Tic-Tac-Toe statistics are not available right now.")
            return
        await controller.send_stats(ctx)

    async def _command_count(
        self,
        ctx: Context,
        user: discord.User | discord.Member | None,
        command_name: str,
    ) -> None:
        if user:
            row = await ctx.bot.pool.fetchrow(
                "SELECT COUNT(*) AS total FROM command_logs "
                "WHERE user_id = $1 AND LOWER(command) = $2",
                user.id,
                command_name,
            )
            total = row["total"] if row else 0
            await ctx.send(
                f"**{user.display_name}** has used **{command_name}** {total:,} time{'s' if total != 1 else ''}."
            )
        else:
            global_rows = await ctx.bot.pool.fetch(
                "SELECT user_id, COUNT(*) AS total FROM command_logs "
                "WHERE LOWER(command) = $1 "
                "GROUP BY user_id ORDER BY total DESC LIMIT 10",
                command_name,
            )
            if not global_rows:
                await ctx.send(f"No one has used **{command_name}** yet!")
                return
            embed = discord.Embed(color=ctx.bot.embedcolor)
            embed.set_author(name=f"Top users of {command_name}")
            g_lines = []
            for r in global_rows:
                u = await get_or_fetch_user(ctx.bot, r["user_id"])
                g_lines.append(
                    f"**{r['total']:,}** {u.display_name if u else r['user_id']}"
                )
            embed.add_field(name="Global", value="\n".join(g_lines), inline=True)
            if ctx.guild:
                guild_rows = await ctx.bot.pool.fetch(
                    "SELECT user_id, COUNT(*) AS total FROM command_logs "
                    "WHERE LOWER(command) = $1 AND guild_id = $2 "
                    "GROUP BY user_id ORDER BY total DESC LIMIT 10",
                    command_name,
                    ctx.guild.id,
                )
                if guild_rows:
                    s_lines = []
                    for r in guild_rows:
                        m = ctx.guild.get_member(r["user_id"])
                        name = m.display_name if m else str(r["user_id"])
                        s_lines.append(f"**{r['total']:,}** {name}")
                    embed.add_field(
                        name=ctx.guild.name, value="\n".join(s_lines), inline=True
                    )
            await ctx.send(embed=embed)

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
