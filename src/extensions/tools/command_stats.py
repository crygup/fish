from __future__ import annotations

from collections.abc import Awaitable, Callable
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
            "SELECT split_part(LOWER(command), ' ', 1) AS command, "
            "COUNT(*) AS total FROM command_logs "
            "GROUP BY split_part(LOWER(command), ' ', 1) "
            "ORDER BY total DESC LIMIT 10"
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
        user: Optional[str] = None,
    ):
        """See how many times a command has been used (global by default, or per user)."""
        command_query, target_user = await self._parse_command_query(
            ctx, command_name, user
        )
        await self._command_count(ctx, target_user, command_query)

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
        rows = await ctx.bot.pool.fetch(
            "SELECT site, SUM(downloads) AS downloads FROM download_stats "
            "WHERE user_id = $1 GROUP BY site ORDER BY downloads DESC, site ASC",
            user.id,
        )
        if not rows:
            subject = "You" if user.id == ctx.author.id else user.name
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
            title=f"Download stats for {user.name}",
            description=f"**Total downloads:** {total:,}\n\n{sites}",
            color=ctx.bot.embedcolor,
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        await ctx.send(embed=embed)

    @cast(Any, stats.group)(
        name="emoji",
        invoke_without_command=True,
        extras={"usage": "[global] [user] [server] [emoji]"},
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        arguments=(
            "Optional global, user, server, and emoji filters in any order. "
            "Global overrides a server filter."
        )
    )
    async def stats_emoji(self, ctx: Context, *, arguments: str | None = None) -> None:
        """Show emoji statistics with dynamic filters."""
        # Emoji commands are mixed into the Discord cog, not registered as a
        # standalone ``Emojis`` cog.
        emoji_cog: Any = ctx.bot.get_cog("Discord")
        if emoji_cog is None:
            await ctx.send("Emoji statistics are not available right now.")
            return
        await emoji_cog.send_emoji_stats(ctx, arguments)

    @stats_emoji.command(name="global")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def stats_emoji_global(
        self, ctx: Context, target: Optional[str] = None
    ) -> None:
        """Show a user's existing emoji statistics across all servers."""
        emoji_cog: Any = ctx.bot.get_cog("Discord")
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

    @stats.command(name="connectfour", aliases=("connect4", "connect-four", "c4"))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def stats_connectfour(self, ctx: Context) -> None:
        """Show the global Connect Four leaderboards."""
        fun_cog: Any = ctx.bot.get_cog("Fun")
        controller: Any = getattr(fun_cog, "_connectfour_controller", None)
        if controller is None:
            await ctx.send("Connect Four statistics are not available right now.")
            return
        await controller.send_stats(ctx)

    @stats.command(name="race", aliases=("sea-race", "seaanimalrace"))
    @app_commands.describe(user="The user whose race stats you want to see.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def stats_race(
        self,
        ctx: Context,
        user: discord.User = commands.param(
            default=commands.Author,
            description="The user whose race stats you want to see.",
        ),
    ) -> None:
        """Show Sea Animal Race wins, losses, earnings, and faints."""
        fun_cog: Any = ctx.bot.get_cog("Fun")
        send_stats = cast(
            Callable[..., Awaitable[Any]] | None,
            getattr(fun_cog, "_send_race_stats", None),
        )
        if not callable(send_stats):
            await ctx.send(
                "Sea Animal Race statistics are not available right now.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await send_stats(ctx, user)

    @stats.command(name="luckyroll", aliases=("lucky-roll", "lr"))
    @app_commands.describe(user="The user whose Lucky Roll stats you want to see.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def stats_luckyroll(
        self,
        ctx: Context,
        user: discord.User = commands.param(
            default=commands.Author,
            description="The user whose Lucky Roll stats you want to see.",
        ),
    ) -> None:
        """Show Lucky Roll wins, losses, earnings, and losses."""
        fun_cog: Any = ctx.bot.get_cog("Fun")
        send_stats = cast(
            Callable[..., Awaitable[Any]] | None,
            getattr(fun_cog, "_send_luckyroll_stats", None),
        )
        if not callable(send_stats):
            await ctx.send(
                "Lucky Roll statistics are not available right now.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await send_stats(ctx, user)

    @stats.command(name="video", aliases=("videos",))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def stats_video(self, ctx: Context) -> None:
        """Show the users with the most approved Fishie video uploads."""

        fun_cog: Any = ctx.bot.get_cog("Fun")
        send_stats = cast(
            Callable[..., Awaitable[Any]] | None,
            getattr(fun_cog, "_send_video_stats", None),
        )
        if send_stats is None:
            await ctx.send(
                "Video statistics are not available right now.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        async with ctx.typing():
            await send_stats(ctx)

    @stats.command(name="click", aliases=("clicks", "clickstats"))
    @app_commands.describe(user="The user whose click total you want to see.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def stats_click(
        self,
        ctx: Context,
        user: discord.User = commands.param(
            default=commands.Author,
            description="The user whose click total you want to see.",
        ),
    ) -> None:
        """Show click totals and the global click leaderboards."""
        fun_cog: Any = ctx.bot.get_cog("Fun")
        send_stats = cast(
            Callable[..., Awaitable[Any]] | None,
            getattr(fun_cog, "_send_click_stats", None),
        )
        if send_stats is None:
            await ctx.send("Click statistics are not available right now.")
            return
        await send_stats(ctx, user)

    @stats.command(
        name="higher-or-lower",
        aliases=("hol", "higherorlower", "highorlow", "higherlower", "highlow"),
    )
    @app_commands.describe(user="The user whose highest streak you want to see.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def stats_higher_or_lower(
        self, ctx: Context, user: discord.User = commands.Author
    ) -> None:
        """Show Higher or Lower streak statistics."""
        fun_cog: Any = ctx.bot.get_cog("Fun")
        send_stats = cast(
            Callable[..., Awaitable[Any]] | None,
            getattr(fun_cog, "_send_streak_game_stats", None),
        )
        if send_stats is None:
            await ctx.send("Higher or Lower statistics are not available right now.")
            return
        await send_stats(ctx, "higher_or_lower", "Higher or Lower", user)

    @stats.command(
        name="heads-or-tails",
        aliases=("headsortails", "headortail", "coinflip", "cf"),
    )
    @app_commands.describe(user="The user whose highest streak you want to see.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def stats_heads_or_tails(
        self, ctx: Context, user: discord.User = commands.Author
    ) -> None:
        """Show Heads or Tails streak statistics."""
        fun_cog: Any = ctx.bot.get_cog("Fun")
        send_stats = cast(
            Callable[..., Awaitable[Any]] | None,
            getattr(fun_cog, "_send_streak_game_stats", None),
        )
        if send_stats is None:
            await ctx.send("Heads or Tails statistics are not available right now.")
            return
        await send_stats(ctx, "heads_or_tails", "Heads or Tails", user)

    @stats.command(
        name="rps",
        aliases=("rock-paper-scissors", "rockpaperscissors"),
    )
    @app_commands.describe(user="The user whose highest streak you want to see.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def stats_rock_paper_scissors(
        self, ctx: Context, user: discord.User = commands.Author
    ) -> None:
        """Show wagered Rock Paper Scissors streak statistics."""
        fun_cog: Any = ctx.bot.get_cog("Fun")
        send_stats = cast(
            Callable[..., Awaitable[Any]] | None,
            getattr(fun_cog, "_send_streak_game_stats", None),
        )
        if send_stats is None:
            await ctx.send(
                "Rock Paper Scissors statistics are not available right now."
            )
            return
        await send_stats(ctx, "rock_paper_scissors", "Rock Paper Scissors", user)

    @stats.command(name="wordbomb", aliases=("wb", "word-bomb"))
    @app_commands.describe(user="The user whose Word Bomb stats you want to see.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def stats_wordbomb(
        self, ctx: Context, user: discord.User = commands.Author
    ) -> None:
        """Show Word Bomb wins, losses, and the global wins leaderboard."""

        visible = ctx.bot.db_cache.game_history_visible_to(user.id, ctx.author.id)
        selected = None
        if visible:
            selected = await ctx.bot.pool.fetchrow(
                "SELECT wins, losses FROM wordbomb_stats WHERE user_id = $1",
                user.id,
            )
        rows = await ctx.bot.pool.fetch(
            "SELECT user_id, wins, losses FROM wordbomb_stats "
            "ORDER BY wins DESC, losses ASC, updated_at ASC, user_id ASC LIMIT 5"
        )
        rows = [
            row
            for row in rows
            if ctx.bot.db_cache.game_history_visible_to(
                int(row["user_id"]), ctx.author.id
            )
        ]

        name = discord.utils.escape_markdown(
            discord.utils.escape_mentions(getattr(user, "name", str(user.id)))
        )
        wins = int(selected["wins"]) if selected is not None else 0
        losses = int(selected["losses"]) if selected is not None else 0
        lines = [
            f"## Word Bomb stats for {name}",
            f"**Wins:** {wins:,} · **Losses:** {losses:,}",
            "### Most wins",
        ]
        if rows:
            for index, row in enumerate(rows, start=1):
                listed_user = await get_or_fetch_user(ctx.bot, int(row["user_id"]))
                listed_name = getattr(listed_user, "name", None) or str(row["user_id"])
                safe_listed_name = discord.utils.escape_markdown(
                    discord.utils.escape_mentions(listed_name)
                )
                lines.append(
                    f"**#{index} {safe_listed_name}** · "
                    f"{int(row['wins']):,} wins · {int(row['losses']):,} losses"
                )
        else:
            lines.append("No Word Bomb games have been recorded yet.")

        embed = discord.Embed(
            title="Word Bomb stats",
            description="\n".join(lines),
            color=ctx.bot.embedcolor,
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @stats.command(name="lastletter", aliases=("lastl", "last-letter"))
    @app_commands.describe(user="The user whose LastLetter stats you want to see.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def stats_lastletter(
        self, ctx: Context, user: discord.User = commands.Author
    ) -> None:
        """Show LastLetter wins, losses, and the global wins leaderboard."""

        fun_cog: Any = ctx.bot.get_cog("Fun")
        send_stats = cast(
            Callable[[Context, discord.User], Awaitable[Any]] | None,
            getattr(fun_cog, "_send_lastletter_stats", None),
        )
        if not callable(send_stats):
            await ctx.send(
                "LastLetter statistics are not available right now.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await send_stats(ctx, user)

    @stats.command(name="lightsout", aliases=("lights-out", "lights"))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def stats_lightsout(self, ctx: Context) -> None:
        """Show the fastest tracked Lights Out completions."""

        fun_cog: Any = ctx.bot.get_cog("Fun")
        send_stats = cast(
            Callable[[Context], Awaitable[Any]] | None,
            getattr(fun_cog, "_send_lightsout_stats", None),
        )
        if not callable(send_stats):
            await ctx.send(
                "Lights Out statistics are not available right now.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        async with ctx.typing():
            await send_stats(ctx)

    @stats.command(
        name="reactions",
        aliases=("reaction",),
        extras={"usage": "[global] [user] [server] [emoji]"},
    )
    @app_commands.describe(
        arguments=(
            "Optional global, user, server, and emoji filters in any order. "
            "Global overrides a server filter."
        )
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def stats_reactions(
        self, ctx: Context, *, arguments: str | None = None
    ) -> None:
        """Show reaction statistics with global, user, server, or emoji filters."""
        reaction_cog: Any = ctx.bot.get_cog("Fun")
        parse_arguments = getattr(reaction_cog, "_parse_reaction_arguments", None)
        send_stats = getattr(reaction_cog, "_send_reaction_stats", None)
        if not callable(parse_arguments) or not callable(send_stats):
            await ctx.send(
                "Reaction statistics are not available right now.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        async with ctx.typing():
            global_scope, user, guild, emoji_filter = await cast(Any, parse_arguments)(
                ctx, arguments
            )
            await cast(Any, send_stats)(
                ctx,
                global_scope=global_scope,
                user=user,
                guild=guild,
                emoji_filter=emoji_filter,
            )

    @stats.command(name="joins", aliases=("join",))
    @app_commands.describe(
        target="A server, user, or `global`; omitted uses the current server."
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def stats_joins(self, ctx: Context, *, target: str | None = None) -> None:
        """Show join leaderboards for a server, user, or globally."""
        async with ctx.typing():
            scope, value = await self._resolve_join_target(ctx, target)
            if scope == "global":
                await self._global_join_stats(ctx)
            elif scope == "server":
                assert isinstance(value, discord.Guild)
                await self._server_join_stats(ctx, value)
            else:
                assert isinstance(value, (discord.User, discord.Member))
                await self._user_join_stats(ctx, value)

    async def _resolve_join_target(
        self, ctx: Context, target: str | None
    ) -> tuple[str, discord.Guild | discord.User | discord.Member | None]:
        raw = target.strip() if target else ""
        lowered = raw.casefold()
        if not raw or lowered in {"server", "guild", "here"}:
            if ctx.guild is None:
                raise commands.BadArgument(
                    "Provide a server, a user, or use `global` in DMs."
                )
            return "server", ctx.guild
        if lowered in {"global", "all"}:
            return "global", None
        if lowered in {"user", "me", "self"}:
            return "user", ctx.author

        guild: discord.Guild | None = None
        if raw.isdigit():
            guild = ctx.bot.get_guild(int(raw))
        if guild is None:
            try:
                guild = await commands.GuildConverter().convert(ctx, raw)
            except commands.CommandError:
                guild = None
        if guild is not None:
            return "server", guild

        try:
            user = await commands.UserConverter().convert(ctx, raw)
        except commands.CommandError as error:
            raise commands.BadArgument(
                f"I could not find a server or user for `{raw}`."
            ) from error
        return "user", user

    @staticmethod
    def _safe_join_name(value: object) -> str:
        return discord.utils.escape_mentions(
            discord.utils.escape_markdown(str(value or "Unknown"))
        )

    async def _join_user_name(
        self,
        ctx: Context,
        user_id: int,
        guild: discord.Guild | None = None,
        user: discord.User | discord.Member | None = None,
    ) -> str:
        if user is None and guild is not None:
            user = guild.get_member(user_id)
        if user is None:
            user = await get_or_fetch_user(ctx.bot, user_id)
        return self._safe_join_name(getattr(user, "name", None) or user_id)

    def _join_visible(
        self,
        ctx: Context,
        user_id: int,
        user: discord.User | discord.Member | None = None,
    ) -> bool:
        if user is not None and user.bot:
            ctx.bot.db_cache.remember_user(user.id, is_bot=True)
            return True
        return user_id == ctx.author.id or (
            ctx.bot.db_cache.user_history_is_public(user_id)
            and not ctx.bot.db_cache.user_tracking_opted_out(user_id, "joins")
        )

    async def _server_join_stats(self, ctx: Context, guild: discord.Guild) -> None:
        rows = await ctx.bot.pool.fetch(
            "SELECT member_id, COUNT(*) AS total FROM member_join_logs "
            "WHERE guild_id = $1 GROUP BY member_id ORDER BY total DESC",
            guild.id,
        )
        lines: list[str] = []
        for row in rows:
            member_id = int(row["member_id"])
            user = guild.get_member(member_id) or await get_or_fetch_user(
                ctx.bot, member_id
            )
            if not self._join_visible(ctx, member_id, user):
                continue
            name = await self._join_user_name(ctx, member_id, guild, user)
            lines.append(f"**{name}** · {int(row['total']):,}")
            if len(lines) == 5:
                break
        if not lines:
            await ctx.send(
                f"No public join data for **{self._safe_join_name(guild.name)}** yet.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        embed = discord.Embed(
            title=f"Join stats · {self._safe_join_name(guild.name)}",
            description="\n".join(lines),
            color=ctx.bot.embedcolor,
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def _user_join_stats(
        self, ctx: Context, user: discord.User | discord.Member
    ) -> None:
        if not self._join_visible(ctx, user.id, user):
            await ctx.send(
                "That user's join history is private.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        rows = await ctx.bot.pool.fetch(
            "SELECT guild_id, COUNT(*) AS total FROM member_join_logs "
            "WHERE member_id = $1 GROUP BY guild_id ORDER BY total DESC LIMIT 5",
            user.id,
        )
        lines: list[str] = []
        for row in rows:
            guild_id = int(row["guild_id"])
            guild = ctx.bot.get_guild(guild_id)
            name = self._safe_join_name(guild.name if guild else f"Server {guild_id}")
            lines.append(f"**{name}** · {int(row['total']):,}")
        if not lines:
            await ctx.send(
                f"**{self._safe_join_name(user.name)}** has no join records yet.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        embed = discord.Embed(
            title=f"Join stats · {self._safe_join_name(user.name)}",
            description="\n".join(lines),
            color=ctx.bot.embedcolor,
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def _global_join_stats(self, ctx: Context) -> None:
        rows = await ctx.bot.pool.fetch(
            "SELECT member_id, COUNT(*) AS total FROM member_join_logs "
            "GROUP BY member_id ORDER BY total DESC"
        )
        lines: list[str] = []
        for row in rows:
            member_id = int(row["member_id"])
            user = await get_or_fetch_user(ctx.bot, member_id)
            if not self._join_visible(ctx, member_id, user):
                continue
            name = await self._join_user_name(ctx, member_id, user=user)
            lines.append(f"**{name}** · {int(row['total']):,}")
            if len(lines) == 5:
                break
        if not lines:
            await ctx.send(
                "No public join data yet.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        embed = discord.Embed(
            title="Join stats · Global",
            description="\n".join(lines),
            color=ctx.bot.embedcolor,
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def _command_count(
        self,
        ctx: Context,
        user: discord.User | discord.Member | None,
        command_name: str,
    ) -> None:
        command = ctx.bot.get_command(command_name)
        if command is not None:
            command_name = command.qualified_name.casefold()
        is_group = isinstance(command, commands.Group)
        command_filter = (
            "(LOWER(command) = $1 OR LOWER(command) LIKE $1 || ' %')"
            if is_group
            else "LOWER(command) = $1"
        )

        if user:
            rows = await ctx.bot.pool.fetch(
                "SELECT LOWER(command) AS command, COUNT(*) AS total "
                "FROM command_logs WHERE user_id = $2 AND "
                + command_filter
                + " GROUP BY LOWER(command) ORDER BY total DESC",
                command_name,
                user.id,
            )
            total = sum(int(row["total"]) for row in rows)
            if not rows:
                await ctx.send(f"**{user.name}** has used **{command_name}** 0 times.")
                return
            if not is_group:
                await ctx.send(
                    f"**{user.name}** has used **{command_name}** "
                    f"{total:,} time{'s' if total != 1 else ''}."
                )
                return

            embed = discord.Embed(
                title=f"{command_name} stats for {user.name}",
                description=f"**Total:** {total:,}",
                color=ctx.bot.embedcolor,
            )
            subcommands = [row for row in rows if str(row["command"]) != command_name]
            if subcommands:
                embed.add_field(
                    name="Subcommands",
                    value="\n".join(
                        f"**{int(row['total']):,}** {row['command']}"
                        for row in subcommands[:5]
                    ),
                    inline=False,
                )
            await ctx.send(embed=embed)

        else:
            global_rows = await ctx.bot.pool.fetch(
                "SELECT user_id, COUNT(*) AS total FROM command_logs WHERE "
                + command_filter
                + " GROUP BY user_id ORDER BY total DESC LIMIT 5",
                command_name,
            )
            if not global_rows:
                await ctx.send(f"No one has used **{command_name}** yet!")
                return
            embed = discord.Embed(color=ctx.bot.embedcolor)
            embed.set_author(name=f"Top users of {command_name}")
            total = sum(int(row["total"]) for row in global_rows)
            embed.description = f"**Total:** {total:,}"
            g_lines = []
            for r in global_rows:
                u = await get_or_fetch_user(ctx.bot, r["user_id"])
                g_lines.append(f"**{r['total']:,}** {u.name if u else r['user_id']}")
            embed.add_field(name="Global", value="\n".join(g_lines), inline=True)

            if is_group:
                subcommands = await ctx.bot.pool.fetch(
                    "SELECT LOWER(command) AS command, COUNT(*) AS total "
                    "FROM command_logs WHERE "
                    + command_filter
                    + " GROUP BY LOWER(command) ORDER BY total DESC",
                    command_name,
                )
                subcommands = [
                    row for row in subcommands if str(row["command"]) != command_name
                ]
                if subcommands:
                    embed.add_field(
                        name="Subcommands",
                        value="\n".join(
                            f"**{int(row['total']):,}** {row['command']}"
                            for row in subcommands[:5]
                        ),
                        inline=False,
                    )
            if ctx.guild:
                guild_rows = await ctx.bot.pool.fetch(
                    "SELECT user_id, COUNT(*) AS total FROM command_logs WHERE "
                    + command_filter
                    + " AND guild_id = $2 "
                    + "GROUP BY user_id ORDER BY total DESC LIMIT 5",
                    command_name,
                    ctx.guild.id,
                )
                if guild_rows:
                    s_lines = []
                    for r in guild_rows:
                        m = ctx.guild.get_member(r["user_id"])
                        name = m.name if m else str(r["user_id"])
                        s_lines.append(f"**{r['total']:,}** {name}")
                    embed.add_field(
                        name=ctx.guild.name, value="\n".join(s_lines), inline=True
                    )
            await ctx.send(embed=embed)

    async def _parse_command_query(
        self, ctx: Context, command_name: str, tail: str | None
    ) -> tuple[str, discord.User | discord.Member | None]:
        """Accept unquoted nested command paths while retaining an optional user."""

        command_query = (
            " ".join(part for part in (command_name, tail or "") if part)
            .strip()
            .casefold()
        )
        if not tail:
            return command_query, None

        # If the complete remainder is a registered command path, it is a
        # nested command rather than a user argument (``stats command jsk py``).
        if ctx.bot.get_command(command_query) is not None:
            return command_query, None

        parts = tail.split()
        if not parts:
            return command_name.strip().casefold(), None
        try:
            target_user = await commands.UserConverter().convert(ctx, parts[-1])
        except commands.CommandError:
            return command_query, None
        command_query = " ".join((command_name, *parts[:-1])).strip().casefold()
        return command_query, target_user

    async def _user_top(
        self, ctx: Context, user: discord.User | discord.Member
    ) -> None:
        rows = await ctx.bot.pool.fetch(
            "SELECT split_part(LOWER(command), ' ', 1) AS command, "
            "COUNT(*) AS total FROM command_logs "
            "WHERE user_id = $1 "
            "GROUP BY split_part(LOWER(command), ' ', 1) "
            "ORDER BY total DESC LIMIT 10",
            user.id,
        )

        if not rows:
            await ctx.send(
                f"{'You' if user.id == ctx.author.id else user.name} "
                f"{'have' if user.id == ctx.author.id else 'has'}n't used any commands yet!"
            )
            return

        lines = [f"**{r['total']:,}** {r['command']}" for r in rows]
        embed = discord.Embed(
            color=ctx.bot.embedcolor,
            description="\n".join(lines),
        )
        embed.set_author(
            name=f"Command stats for {user.name}",
            icon_url=user.display_avatar.url,
        )
        await ctx.send(embed=embed)

    async def _guild_stats(self, ctx: Context, guild: discord.Guild) -> None:
        cmd_rows = await ctx.bot.pool.fetch(
            "SELECT split_part(LOWER(command), ' ', 1) AS command, "
            "COUNT(*) AS total FROM command_logs "
            "WHERE guild_id = $1 "
            "GROUP BY split_part(LOWER(command), ' ', 1) "
            "ORDER BY total DESC LIMIT 10",
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
                    name = user.name if user else str(r["user_id"])
                else:
                    name = member.name
                user_lines.append(f"**{r['total']:,}** {name}")
            embed.add_field(
                name="Top Users",
                value="\n".join(user_lines),
                inline=True,
            )

        await ctx.send(embed=embed)


async def setup(bot: Fishie):
    await bot.add_cog(CommandStats())
