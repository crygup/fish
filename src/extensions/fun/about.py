from __future__ import annotations

import asyncio
import textwrap
from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import TYPE_CHECKING, NamedTuple

import discord
import psutil
from discord.ext import commands

from core import Cog
from utils import get_or_fetch_user, human_timedelta, natural_size

if TYPE_CHECKING:
    from extensions.context import Context


class TableCounts(NamedTuple):
    total: int
    last_24_hours: int


class About(Cog):
    process: psutil.Process
    invite_url: str

    @commands.command(name="about")
    async def about(self, ctx: Context):
        """Tells you information about the bot itself."""

        if ctx.bot.user is None:
            return

        start = discord.utils.utcnow() - timedelta(hours=24)
        counts = await self.bot.pool.fetchrow(
            "SELECT COUNT(*) AS total, "
            "COUNT(*) FILTER (WHERE created_at >= $1) AS last_24_hours "
            "FROM command_logs",
            start,
        )
        command_counts = (
            TableCounts(total=0, last_24_hours=0)
            if counts is None
            else TableCounts(
                total=int(counts["total"]),
                last_24_hours=int(counts["last_24_hours"]),
            )
        )
        liz = await get_or_fetch_user(
            bot=self.bot, user_id=self.bot.config["ids"]["owner_id"]
        )
        uptime = human_timedelta(
            ctx.bot.start_time, accuracy=None, brief=True, suffix=False
        )
        owner_name = discord.utils.escape_markdown(liz.name)
        details = (
            f"Uptime: {uptime}\n"
            f"Servers: {len(ctx.bot.guilds):,}\n"
            f"Commands ran: {command_counts.total:,} "
            f"({command_counts.last_24_hours:,} past 24h)\n"
            "Invite: [Server](https://discord.gg/rM9u4MRFBE) · "
            f"[Bot]({self.invite_url})\n"
            "Donate: [ko-fi](https://ko-fi.com/crygup)"
        )
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    f"### {discord.utils.escape_markdown(ctx.bot.user.name)}\n"
                    "tracking, anime, image tools, mudae help, pokétwo help, "
                    "& more."
                ),
                discord.ui.Separator(),
                discord.ui.TextDisplay(details),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"-# Created {discord.utils.format_dt(ctx.bot.user.created_at, 't')} "
                    f"· Created by {owner_name} · "
                    "[Source](https://github.com/crygup/fish)"
                ),
                accent_color=ctx.bot.embedcolor,
            )
        )

        await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="hello", hidden=True)
    async def hello(self, ctx: Context):
        """Displays my hello message"""
        zil = await get_or_fetch_user(self.bot, self.bot.config["ids"]["owner_id"])
        msg = f"Hello! I'm a robot! {zil} made me."

        if ctx.bot.testing:
            msg += "\nThis is the testing version of the bot."

        await ctx.send(msg)

    @commands.command(name="usage", hidden=True)
    @commands.cooldown(1, 30)
    async def usage(self, ctx: Context):
        """This shows a bit more info than about

        Can be hard to read for mobile users, sorry."""
        bot = self.bot
        async with ctx.typing():
            # fmt: off
            members_count: int = sum(g.member_count for g in bot.guilds)  # type: ignore
            start = discord.utils.utcnow() - timedelta(hours=24)

            async def table_counts(table: str, created_at: str = "created_at") -> TableCounts:
                counts = await bot.pool.fetchrow(
                    f"SELECT COUNT(*) AS total, "
                    f"COUNT(*) FILTER (WHERE {created_at} >= $1) AS last_24_hours FROM {table}",
                    start,
                )
                if counts is None:
                    return TableCounts(total=0, last_24_hours=0)
                return TableCounts(
                    total=int(counts["total"]),
                    last_24_hours=int(counts["last_24_hours"]),
                )

            avatars, commands, usernames, nicknames, discrims, server_tags, member_joins = await asyncio.gather(
                table_counts("avatars"),
                table_counts("command_logs"),
                table_counts("username_logs"),
                table_counts("nickname_logs"),
                table_counts("discrim_logs"),
                table_counts("stag_logs"),
                table_counts("member_join_logs", "time"),
            )
            # fmt: on
            psql_start = perf_counter()
            await bot.pool.execute("SELECT 1")
            psql_end = perf_counter()

            current_reminders = await bot.pool.fetchval(
                """
                SELECT COUNT(*)
                FROM reminders
                WHERE event = 'reminder' AND expires > $1
                """,
                discord.utils.utcnow(),
            )

            mem = self.process.memory_full_info()
            memory_usage = mem.uss / 1024**2
            cpu_usage = self.process.cpu_percent() / psutil.cpu_count()  # type: ignore
            server_start_time = datetime.fromtimestamp(
                psutil.boot_time(), tz=timezone.utc
            )

            message = f"""
                        memory : {memory_usage:.2f} MiB
                virtual memory : {natural_size(mem.vms)}
             websocket latency : {round(bot.latency * 1000, 3)}ms
            postgresql latency : {round(psql_end - psql_start, 3)}ms
                 server uptime : {human_timedelta(server_start_time, accuracy=None, suffix=False)}
                    bot uptime : {human_timedelta(bot.start_time, accuracy=None, suffix=False)}
              active reminders : {int(current_reminders or 0):,}
                           cpu : {cpu_usage:.2f}%
                       threads : {self.process.num_threads():,}
                    discord.py : {discord.__version__}
                        guilds : {len(bot.guilds):,}
                       members : {members_count:,}
                         users : {len(bot.users):,}
                        emojis : {len(bot.emojis):,}
                      stickers : {len(bot.stickers):,}
                pokemon stored : {len(bot.pokemon):,}
               cached messages : {len(bot.cached_messages):,}
                avatars logged : {avatars.total:,} - {avatars.last_24_hours:,}
              usernames logged : {usernames.total:,} - {usernames.last_24_hours:,}
               discrims logged : {discrims.total:,} - {discrims.last_24_hours:,}
              nicknames logged : {nicknames.total:,} - {nicknames.last_24_hours:,}
            server tags logged : {server_tags.total:,} - {server_tags.last_24_hours:,}
           member joins logged : {member_joins.total:,} - {member_joins.last_24_hours:,}
                  commands ran : {commands.total:,} - {commands.last_24_hours:,}
                  """

        await ctx.send(f"```yaml{textwrap.dedent(message)}```")
