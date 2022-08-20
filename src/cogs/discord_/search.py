import argparse
import shlex
from typing import Optional

import discord
from bot import Bot, Context
from discord.ext import commands
from utils import FieldPageSource, Pager, RoleConverter

from ._base import CogBase


class Arguments(argparse.ArgumentParser):
    def error(self, message):
        raise RuntimeError(message)


class SearchCommand(CogBase):
    def __init__(self, bot: Bot):
        self.bot = bot

    @commands.command(
        name="search",
        extras={"examples": ["cr search -name liz -discrim 0333 -length 3 -nick"]},
    )
    async def search(self, ctx: Context, *, args: Optional[str] = None):
        """Search for a member with certain flags.

        Flags:
        -starts: Name starts with x
        -ends: Name ends with x
        -contains: Names contains x
        -length: Length of name
        -name: Name matches x
        -discrim: Discriminator matches x
        -role: Has role

        Flags (no arguments):
        -nick: Nicknames only
        -no_avatar: No avatar
        -spotify: Listening to spotify
        -online: Online status
        -dnd: Do not disturb status
        -idle: Idle status
        -offline: Offline status
        -streaming: Streaming status
        -mobile: Mobile status
        -desktop: Desktop status
        -web: Web status
        -bots: Bots
        -voice: Current in a voice channel
        """

        if args is None:
            await ctx.send_help(ctx.command)
            return

        if ctx.guild is None:
            raise commands.GuildNotFound("No guild found")

        parser = Arguments(add_help=False, allow_abbrev=False)

        # check flags, can use multiple
        parser.add_argument("-starts", nargs="+")
        parser.add_argument("-ends", nargs="+")
        parser.add_argument("-length", type=int)
        parser.add_argument("-contains", nargs="+")
        parser.add_argument("-name", nargs="+")
        parser.add_argument("-discrim", nargs="+")
        parser.add_argument("-role", type=str)

        # no arg flags
        parser.add_argument("-nick", action="store_true")
        parser.add_argument("-no_avatar", action="store_true")
        parser.add_argument("-spotify", action="store_true")
        parser.add_argument("-online", action="store_true")
        parser.add_argument("-dnd", action="store_true")
        parser.add_argument("-idle", action="store_true")
        parser.add_argument("-offline", action="store_true")
        parser.add_argument("-streaming", action="store_true")
        parser.add_argument("-mobile", action="store_true")
        parser.add_argument("-desktop", action="store_true")
        parser.add_argument("-web", action="store_true")
        parser.add_argument("-bots", action="store_true")
        parser.add_argument("-voice", action="store_true")

        # parse flags
        parser.add_argument("-not", action="store_true", dest="_not")
        parser.add_argument("-or", action="store_true", dest="_or")

        try:
            flags = parser.parse_args(shlex.split(args))
        except Exception as e:
            return await ctx.send(str(e))

        predicates = []

        def name(m: discord.Member):
            return m.nick.lower() if flags.nick and m.nick else m.name.lower()

        if flags.nick:
            predicates.append(lambda m: m.nick is not None)

        if flags.starts:
            predicates.append(
                lambda m: any(name(m).startswith(s.lower()) for s in flags.starts)
            )

        if flags.ends:
            predicates.append(
                lambda m: any(name(m).endswith(s.lower()) for s in flags.ends)
            )

        if flags.length:
            predicates.append(lambda m: len(name(m)) == flags.length)

        if flags.contains:
            predicates.append(
                lambda m: any(sub.lower() in name(m) for sub in flags.contains)
            )

        if flags.name:
            predicates.append(lambda m: any(name(m) == s.lower() for s in flags.name))

        if flags.discrim:
            predicates.append(
                lambda m: any(m.discriminator == s for s in flags.discrim)
            )

        if flags.role:
            role = await RoleConverter().convert(ctx, flags.role)
            if role is None:
                raise commands.RoleNotFound(flags.role)

            predicates.append(lambda m: role in m.roles)

        if flags.no_avatar:
            predicates.append(lambda m: m.avatar is None)

        if flags.spotify:
            predicates.append(
                lambda m: discord.utils.find(
                    lambda a: isinstance(a, discord.Spotify), m.activities
                )
            )

        status = []

        if flags.online:
            status.append(discord.Status.online)

        if flags.dnd:
            status.append(discord.Status.dnd)

        if flags.idle:
            status.append(discord.Status.idle)

        if flags.offline:
            status.append(discord.Status.offline)

        if status:
            predicates.append(lambda m: m.status in status)

        if flags.streaming:
            predicates.append(
                lambda m: discord.utils.find(
                    lambda a: isinstance(a, discord.Streaming), m.activities
                )
            )

        if flags.mobile:
            predicates.append(lambda m: m.mobile_status is not discord.Status.offline)

        if flags.desktop:
            predicates.append(lambda m: m.desktop_status is not discord.Status.offline)

        if flags.web:
            predicates.append(lambda m: m.web_status is not discord.Status.offline)

        if flags.bots:
            predicates.append(lambda m: m.bot)

        if flags.voice:
            predicates.append(lambda m: m.voice is not None)

        op = all if not flags._or else any

        def predicate(m):
            r = op(p(m) for p in predicates)
            if flags._not:
                return not r
            return r

        members = [m for m in ctx.guild.members if predicate(m)]
        if not members:
            raise TypeError("No members found that meet the criteria.")

        entries = [
            (
                f'{str(m)} {f"| {m.nick}" if flags.nick else ""}',
                f'**ID**: `{m.id}`\n**Created**: {discord.utils.format_dt(m.created_at, "R")}\n'
                f'**Joined**: {discord.utils.format_dt(m.joined_at, "R")}',
            )
            for m in members
            if m.joined_at
        ]

        p = FieldPageSource(entries, per_page=2)
        p.embed.title = f"Members in {ctx.guild.name} that meet the criteria"
        menu = Pager(p, ctx=ctx)
        await menu.start(ctx)
