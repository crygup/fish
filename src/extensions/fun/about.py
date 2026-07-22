from __future__ import annotations

import asyncio
import textwrap
from time import perf_counter
from typing import TYPE_CHECKING

import discord
import psutil
from discord.ext import commands

from core import Cog
from utils import get_or_fetch_user, human_timedelta, natural_size

if TYPE_CHECKING:
    from extensions.context import Context


class About(Cog):
    process: psutil.Process
    invite_url: str

    @commands.command(name="about")
    async def about(self, ctx: Context):
        """Tells you information about the bot itself."""

        if ctx.bot.user is None:
            return

        start = discord.utils.utcnow().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        counts = await self.bot.pool.fetchrow(
            "SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE created_at >= $1) AS today "
            "FROM command_logs",
            start,
        )
        total = int(counts["total"])
        today = int(counts["today"])
        memory_usage = self.process.memory_full_info().uss / 1024**2
        cpu_usage = self.process.cpu_percent() / psutil.cpu_count()  # type: ignore
        liz = await get_or_fetch_user(
            bot=self.bot, user_id=self.bot.config["ids"]["owner_id"]
        )

        e = discord.Embed(
            description="cool discord bot",
            timestamp=ctx.bot.user.created_at,
            color=ctx.bot.embedcolor,
        )

        e.set_footer(text="Created at")
        e.set_author(name=f"{liz}", icon_url=liz.display_avatar.url)

        e.add_field(name="Commands ran", value=f"{total:,} total\n{today:,} today")
        e.add_field(
            name="Process", value=f"{memory_usage:.2f} MiB\n{cpu_usage:.2f}% CPU"
        )
        e.add_field(name="Invite", value=f"[Click here]({self.invite_url})")
        e.add_field(name="Guilds", value=f"{len(ctx.bot.guilds):,}")

        e.add_field(name="Users", value=f"{len(ctx.bot.users):,}")
        e.add_field(
            name="Uptime",
            value=human_timedelta(
                ctx.bot.start_time, accuracy=None, brief=True, suffix=False
            ),
        )
        e.add_field(name="Donate", value="[ko-fi :D](https://ko-fi.com/crygup)")

        await ctx.send(embed=e)

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
            start = discord.utils.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

            async def table_counts(table: str):
                return await bot.pool.fetchrow(
                    f"SELECT COUNT(*) AS total, "
                    f"COUNT(*) FILTER (WHERE created_at >= $1) AS today FROM {table}",
                    start,
                )

            avatars, commands, usernames, nicknames, discrims = await asyncio.gather(
                table_counts("avatars"),
                table_counts("command_logs"),
                table_counts("username_logs"),
                table_counts("nickname_logs"),
                table_counts("discrim_logs"),
            )
            # fmt: on
            psql_start = perf_counter()
            await bot.pool.execute("SELECT 1")
            psql_end = perf_counter()

            mem = self.process.memory_full_info()
            memory_usage = mem.uss / 1024**2
            cpu_usage = self.process.cpu_percent() / psutil.cpu_count()  # type: ignore

            message = f"""
                        memory : {memory_usage:.2f} MiB
                virtual memory : {natural_size(mem.vms)}
                           cpu : {cpu_usage:.2f}%
                           pid : {self.process.pid}
                       threads : {self.process.num_threads():,}
                    discord.py : {discord.__version__}
                        guilds : {len(bot.guilds):,}
                       members : {members_count:,}
                         users : {len(bot.users):,}
                        emojis : {len(bot.emojis):,}
                      stickers : {len(bot.stickers):,}
               cached messages : {len(bot.cached_messages):,}
             websocket latency : {round(bot.latency * 1000, 3)}ms
            postgresql latency : {round(psql_end - psql_start, 3)}ms
                avatars logged : {avatars['total']:,} - {avatars['today']:,}
              usernames logged : {usernames['total']:,} - {usernames['today']:,}
               discrims logged : {discrims['total']:,} - {discrims['today']:,}
              nicknames logged : {nicknames['total']:,} - {nicknames['today']:,}
                  commands ran : {commands['total']:,} - {commands['today']:,}
                  """

        await ctx.send(f"```yaml{textwrap.dedent(message)}```")
