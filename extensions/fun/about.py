from __future__ import annotations

import textwrap
from time import perf_counter
from typing import TYPE_CHECKING, List

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

        sql = """SELECT * FROM command_logs"""
        results = await self.bot.pool.fetch(sql)

        total = len(results)
        start = discord.utils.utcnow().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        today = len([result for result in results if result["created_at"] >= start])
        memory_usage = self.process.memory_full_info().uss / 1024**2
        cpu_usage = self.process.cpu_percent() / psutil.cpu_count()
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

        await ctx.send(embed=e)

    @commands.command(name="hello", hidden=True)
    async def hello(self, ctx: Context):
        """Displays my hello message"""
        liz = await get_or_fetch_user(self.bot, self.bot.config["ids"]["owner_id"])
        msg = f"Hello! I'm a robot! {liz} made me."

        if ctx.bot.testing:
            msg += "\nThis is the testing version of the bot."

        await ctx.send(msg)

    @commands.command(name="stats", hidden=True)
    @commands.cooldown(1, 30)
    async def stats(self, ctx: Context):
        """This shows a bit more info than about

        Can be hard to read for mobile users, sorry."""
        bot = self.bot
        async with ctx.typing():
            # fmt: off
            members_count: int = sum(g.member_count for g in bot.guilds)  # type: ignore
            start = discord.utils.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

            avatars = await bot.pool.fetch("SELECT created_at FROM avatars")
            avatars_today = len([result for result in avatars if result["created_at"] >= start])

            commands = await bot.pool.fetch("SELECT created_at FROM command_logs")
            commands_today = len([result for result in commands if result["created_at"] >= start])

            usernames = await bot.pool.fetch("SELECT created_at FROM username_logs")
            usernames_today = len([result for result in usernames if result["created_at"] >= start])

            nicknames = await bot.pool.fetch("SELECT created_at FROM nickname_logs")
            nicknames_today = len([result for result in nicknames if result["created_at"] >= start])

            discrims = await bot.pool.fetch("SELECT created_at FROM discrim_logs")
            discrims_today = len([result for result in discrims if result["created_at"] >= start])
            # fmt: on
            psql_start = perf_counter()
            await bot.pool.execute("SELECT 1")
            psql_end = perf_counter()

            redis_start = perf_counter()
            await self.bot.redis.ping()
            redis_end = perf_counter()

            mem = self.process.memory_full_info()
            memory_usage = mem.uss / 1024**2
            cpu_usage = self.process.cpu_percent() / psutil.cpu_count()

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
                 redis latency : {round(redis_end - redis_start, 3)}ms
                avatars logged : {len(avatars):,} - {avatars_today:,}
              usernames logged : {len(usernames):,} - {usernames_today:,}
               discrims logged : {len(discrims):,} - {discrims_today:,}
              nicknames logged : {len(nicknames):,} - {nicknames_today:,}
                  commands ran : {len(commands):,} - {commands_today:,}
                  """

        await ctx.send(f"```yaml{textwrap.dedent(message)}```")
