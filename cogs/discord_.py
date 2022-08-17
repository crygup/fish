from __future__ import annotations

import argparse
import datetime
import json
import math
import random
import re
import shlex
import time, asyncpg
from io import BytesIO
from typing import (
    TYPE_CHECKING,
    Dict,
    List,
    Literal,
    Optional,
    Union,
)

import discord
from bot import Bot
from discord.ext import commands, menus
from PIL import Image
from utils import (
    EmojiConverter,
    FieldPageSource,
    GuildChannel,
    Pager,
    RoleConverter,
    SimplePages,
    get_user_badges,
    resize_to_limit,
    to_thread,
    human_timedelta,
)

from cogs.context import Context
from utils.helpers import AuthorView

blurple = discord.ButtonStyle.blurple
red = discord.ButtonStyle.red

if TYPE_CHECKING:
    from discord.asset import ValidAssetFormatTypes


async def setup(bot: Bot):
    await bot.add_cog(Discord_(bot))


class Arguments(argparse.ArgumentParser):
    def error(self, message):
        raise RuntimeError(message)


class Discord_(commands.Cog, name="discord"):
    """Discord specific commands"""

    def __init__(self, bot: Bot):
        self.bot = bot

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name="discord", id=1006848754944593921)

    def get_emoji(self, channel: GuildChannel) -> str:
        if isinstance(channel, discord.TextChannel):
            if channel.is_nsfw():
                return "\U0001f51e"
            elif channel.is_news():
                return "\U0001f4e2"
            else:
                return "\U0001f4dd"

        elif isinstance(channel, discord.VoiceChannel):
            return "\U0001f50a"

        elif isinstance(channel, discord.CategoryChannel):
            return "\U0001f4c1"

        elif isinstance(channel, discord.StageChannel):
            return "\U0001f399"

        else:
            return "\U00002754"

    async def failed_to_find(
        self, ctx: Context, guild_id: int, channel_id: int, message_id: int
    ) -> None:
        url = f"https://discordapp.com/channels/{guild_id}/{channel_id}/{message_id}"
        await ctx.send(
            f"I was unable to find and verify the message, here is a link, it might not work though. \b{url}"
        )

    @commands.command(name="first_message", aliases=("fmsg", "oldest"))
    async def first_message(
        self,
        ctx: Context,
        channel: Optional[
            Union[discord.TextChannel, discord.Thread, discord.VoiceChannel]
        ],
        *,
        user: Optional[discord.User],
    ):
        """Sends a url to the first message from a member in a channel.

        This is based on when fishie was added to the server."""

        if ctx.guild is None:
            return

        channel = channel or ctx.channel

        if user:
            sql = f"""SELECT * FROM message_logs WHERE author_id = $1 AND guild_id = $2 AND channel_id = $3 ORDER BY created_at ASC LIMIT 1"""
            args = [user.id, ctx.guild.id, channel.id]
        else:
            sql = f"""SELECT * FROM message_logs WHERE guild_id = $1 AND channel_id = $2 ORDER BY created_at ASC LIMIT 1"""
            args = [ctx.guild.id, channel.id]

        record = await self.bot.pool.fetchrow(sql, *args)
        if record is None:
            await ctx.send(f"It seems I have no records in this channel")
            return

        _channel = self.bot.get_channel(channel.id)
        if _channel is None or not isinstance(
            _channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)
        ):
            await self.failed_to_find(
                ctx, ctx.guild.id, channel.id, record["message_id"]
            )
            return

        try:
            _message = await _channel.fetch_message(record["message_id"])
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            await self.failed_to_find(
                ctx, ctx.guild.id, channel.id, record["message_id"]
            )
            return

        await ctx.send(_message.jump_url)

    async def _index_member(
        self, guild: discord.Guild, user: discord.Member | discord.User
    ) -> bool:
        member = guild.get_member(user.id)

        if member is None:
            return False

        joined = member.joined_at

        if joined is None:
            return False

        await self.bot.pool.execute(
            "INSERT INTO member_join_logs (member_id, guild_id, time) VALUES ($1, $2, $3)",
            user.id,
            guild.id,
            joined,
        )

        return True

    @commands.group(name="joins", invoke_without_command=True)
    async def joins(
        self,
        ctx: Context,
        guild: Optional[discord.Guild] = commands.CurrentGuild,
        *,
        user: discord.User = commands.Author,
    ):
        """Shows how many times a user joined a server

        Note: If they joined before I was added then I will not have any data for them."""

        guild = guild or ctx.guild

        if guild is None:
            return

        results: Optional[int] = await self.bot.pool.fetchval(
            "SELECT COUNT(member_id) FROM member_join_logs WHERE member_id = $1 AND guild_id = $2",
            user.id,
            guild.id,
        )

        if results == 0 or results is None:
            results = await self._index_member(guild, user)
            if results:
                results = 1

            else:
                await ctx.send(f"I have no join records for {user!s} in {guild!s}")
                return

        await ctx.send(
            f"{user} has joined {guild} {results:,} time{'s' if results > 1 else ''}."
        )

    @commands.command(name="uptime")
    async def uptime(self, ctx: Context, *, member: Optional[discord.Member]):
        """Shows how long a user has been online."""
        bot = self.bot
        me = bot.user

        if me is None or bot.uptime is None:
            return

        if member is None or member and member.id == me.id:
            await ctx.send(
                f"Hello, I have been awake for {human_timedelta(bot.uptime,suffix=False)}."
            )
            return

        results: Optional[datetime.datetime] = await bot.pool.fetchval(
            "SELECT time FROM uptime_logs WHERE user_id = $1", member.id
        )

        if results is None:
            await ctx.send(
                f'{member} has been {"on " if member.status is discord.Status.dnd else ""}{member.raw_status} as long as I can tell.'
            )
            return

        await ctx.send(
            f'{member} has been {"on " if member.status is discord.Status.dnd else ""}{member.raw_status} for {human_timedelta(results,suffix=False)}.'
        )

    @commands.command(name="usernames", aliases=("names",))
    async def usernames(self, ctx: Context, user: discord.User = commands.Author):

        results = await self.bot.pool.fetch(
            "SELECT * FROM username_logs WHERE user_id = $1 ORDER BY created_at DESC",
            user.id,
        )

        if results == []:
            await ctx.send(f"I have no username records for {user}")
            return

        entries = [
            (
                r["username"],
                f'{discord.utils.format_dt(r["created_at"], "R")}  |  {discord.utils.format_dt(r["created_at"], "d")}',
            )
            for r in results
        ]

        source = FieldPageSource(entries=entries)
        source.embed.color = self.bot.embedcolor
        source.embed.title = f"Usernames for {user}"
        pager = Pager(source, ctx=ctx)
        await pager.start(ctx)

    @commands.command(name="discrims", aliases=("discriminators",))
    async def discrims(self, ctx: Context, user: discord.User = commands.Author):
        """Shows all discriminators a user has had.

        This is the numbers after your username."""

        results = await self.bot.pool.fetch(
            "SELECT * FROM discrim_logs WHERE user_id = $1 ORDER BY created_at DESC",
            user.id,
        )

        if results == []:
            await ctx.send(f"I have no discriminator records for {user}")
            return

        entries = [
            (
                f'#{r["discrim"]}',
                f'{discord.utils.format_dt(r["created_at"], "R")}  |  {discord.utils.format_dt(r["created_at"], "d")}',
            )
            for r in results
        ]

        source = FieldPageSource(entries=entries)
        source.embed.color = self.bot.embedcolor
        source.embed.title = f"Discriminators for {user}"
        pager = Pager(source, ctx=ctx)
        await pager.start(ctx)

    @commands.command(name="nicknames", aliases=("nicks",))
    async def nicknames(
        self,
        ctx: Context,
        *,
        user: discord.User = commands.Author,
    ):
        """Shows all nicknames a user has had in a guild."""
        if ctx.guild is None:
            return

        results = await self.bot.pool.fetch(
            "SELECT * FROM nickname_logs WHERE user_id = $1 AND guild_id = $2 ORDER BY created_at DESC",
            user.id,
            ctx.guild.id,
        )

        if results == []:
            await ctx.send(f"I have no nickname records for {user} in {ctx.guild}")
            return

        entries = [
            (
                r["nickname"],
                f'{discord.utils.format_dt(r["created_at"], "R")}  |  {discord.utils.format_dt(r["created_at"], "d")}',
            )
            for r in results
        ]

        source = FieldPageSource(entries=entries)
        source.embed.title = f"Nicknames for {user} in {ctx.guild}"
        source.embed.color = self.bot.embedcolor
        pager = Pager(source, ctx=ctx)
        await pager.start(ctx)

    # https://github.com/CuteFwan/Koishi/blob/master/cogs/avatar.py#L82-L102
    @to_thread
    def _make_avatars(self, filesize_limit: int, avatars: List[bytes]) -> BytesIO:
        xbound = math.ceil(math.sqrt(len(avatars)))
        ybound = math.ceil(len(avatars) / xbound)
        size = int(2520 / xbound)

        with Image.new(
            "RGBA", size=(xbound * size, ybound * size), color=(0, 0, 0, 0)
        ) as base:
            x, y = 0, 0
            for avy in avatars:
                if avy:
                    im = Image.open(BytesIO(avy)).resize(
                        (size, size), resample=Image.BICUBIC
                    )
                    base.paste(im, box=(x * size, y * size))
                if x < xbound - 1:
                    x += 1
                else:
                    x = 0
                    y += 1
            buffer = BytesIO()
            base.save(buffer, "png")
            buffer.seek(0)
            buffer = resize_to_limit(buffer, filesize_limit)
            return buffer

    async def do_avatar_command(
        self,
        ctx: Context,
        user: discord.User | discord.Member,
        avatars: List[asyncpg.Record],
    ) -> discord.File:

        if ctx.guild is None:
            raise commands.GuildNotFound("Guild not found")

        fp = await self._make_avatars(
            ctx.guild.filesize_limit, [x["avatar"] for x in avatars]
        )
        file = discord.File(
            fp,
            f"{user.id}_avatar_history.png",
        )

        return file

    @commands.group(
        name="avatarhistory", aliases=("avyh",), invoke_without_command=True
    )
    async def avatar_history(
        self, ctx: Context, *, user: discord.User = commands.Author
    ):
        """Shows the avatar history of a user."""
        if ctx.guild is None:
            return

        async with ctx.typing():
            fetch_start = time.perf_counter()
            avatars: List[asyncpg.Record] = await self.bot.pool.fetch(
                "SELECT * FROM avatar_logs WHERE user_id = $1 ORDER BY created_at DESC LIMIT 100",
                user.id,
            )
            fetch_end = time.perf_counter()

            if avatars == []:
                raise TypeError(f"{str(user)} has no avatar history on record.")

            gen_start = time.perf_counter()
            file = await self.do_avatar_command(ctx, user, avatars)
            gen_end = time.perf_counter()

        embed = discord.Embed(timestamp=avatars[-1]["created_at"])
        embed.set_footer(text="First avatar saved")
        embed.set_author(
            name=f"{user}'s avatar history", icon_url=user.display_avatar.url
        )
        embed.description = f"`Fetching  :` {round(fetch_end - fetch_start, 2)}s\n`Generating:` {round(gen_end - gen_start, 2)}s"
        embed.set_image(url=f"attachment://{user.id}_avatar_history.png")

        await ctx.send(embed=embed, file=file)

    @avatar_history.command(name="guild")
    async def avatar_history_guild(
        self, ctx: Context, *, member: discord.Member = commands.Author
    ):
        """Shows the guild avatar history of a user."""
        async with ctx.typing():
            fetch_start = time.perf_counter()
            avatars: List[asyncpg.Record] = await self.bot.pool.fetch(
                "SELECT avatar FROM guild_avatar_logs WHERE user_id = $1 AND guild_id = $2 ORDER BY created_at DESC LIMIT 100",
                member.id,
                ctx.guild.id,
            )
            fetch_end = time.perf_counter()

            if avatars == []:
                raise TypeError(f"{str(member)} has no avatar history on record.")

            gen_start = time.perf_counter()
            file = await self.do_avatar_command(ctx, member, avatars)
            gen_end = time.perf_counter()

        embed = discord.Embed(timestamp=avatars[-1]["created_at"])
        embed.set_footer(text="First avatar saved")
        embed.set_author(
            name=f"{member}'s avatar history in {ctx.guild}",
            icon_url=member.display_avatar.url,
        )
        embed.description = f"`Fetching  :` {round(fetch_end - fetch_start, 2)}s\n`Generating:` {round(gen_end - gen_start, 2)}s"
        embed.set_image(url=f"attachment://{member.id}_avatar_history.png")

        await ctx.send(content=f"Viewing guild avatar log for {member}", file=file)

    @commands.command(
        name="view_as",
        aliases=(
            "viewas",
            "va",
        ),
    )
    async def view_as(
        self,
        ctx: Context,
        *,
        member: discord.Member = commands.Author,
        guild: Optional[discord.Guild] = None,
    ):
        """View the server as another member


        \U0001f51e = NSFW channel
        \U0001f4e2 = News channel
        \U0001f4dd = Text channel
        \U0001f50a = Voice channel
        \U0001f4c1 = Category channel
        \U0001f399 = Stage channel
        """
        guild = guild or ctx.guild

        if guild is None:
            raise commands.GuildNotFound(str(guild))

        mapping: Dict = {}
        channels = [
            c
            for c in guild.channels
            if c.permissions_for(member).view_channel
            and c.permissions_for(member).read_messages
        ]
        categories = [
            c
            for c in guild.categories
            if c.permissions_for(member).view_channel
            and c.permissions_for(member).read_messages
        ]

        mapping[None] = sorted(
            [
                c
                for c in channels
                if not isinstance(c, discord.CategoryChannel) and c.category is None
            ],
            key=lambda ch: ch.position,
        )
        mapping.update(
            {
                c: sorted(c.channels, key=lambda ch: ch.position)
                for c in sorted(categories, key=lambda cat: cat.position)
            }
        )
        to_send = ""
        b = "`" * 3

        for cate, chs in mapping.items():
            to_send += f"{self.get_emoji(cate)}{cate.name}\n" if cate else ""
            for ch in chs:
                a = f"\u200b  " if ch.category is not None else ""
                to_send += f"{a}{self.get_emoji(ch)}{ch.name}\n"

        await ctx.send(f"Viewing {str(guild)} as {str(member)}\n" f"{b}{to_send}{b}")

    @commands.group(
        name="serverinfo",
        aliases=("si", "server", "guild", "guildinfo", "gi"),
        invoke_without_command=True,
    )
    async def serverinfo(self, ctx: Context, guild: Optional[discord.Guild] = None):
        """Get information about a server."""

        guild = guild or ctx.guild

        if not guild:
            raise commands.GuildNotFound("Unknown guild")

        if ctx.bot.user is None:
            raise TypeError("Bot is not logged in.")

        embed = discord.Embed(timestamp=guild.created_at)
        images = []

        name = (
            f"{guild.name}  •  {guild.vanity_url}" if guild.vanity_url else guild.name
        )
        embed.set_author(
            name=name,
            icon_url=guild.icon.url if guild.icon else ctx.bot.user.display_avatar.url,
        )

        if guild.description:
            embed.description = discord.utils.escape_markdown(guild.description)

        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
            images.append(f"[Icon]({guild.icon.url})")

        if guild.banner:
            images.append(f"[Banner]({guild.banner.url})")

        if guild.splash:
            images.append(f"[Splash]({guild.splash.url})")

        humans = sum(1 for m in guild.members if not m.bot)
        bots = sum(1 for m in guild.members if m.bot)
        embed.add_field(
            name=f"{guild.member_count:,} Members",
            value=f"{self.bot.e_replies}{humans:,} Humans\n"
            f"{self.bot.e_reply}{bots:,} Bots",
        )

        embed.add_field(
            name=f"{len(guild.channels):,} Channels",
            value=f"{self.bot.e_replies}{len(guild.text_channels):,} Text\n"
            f"{self.bot.e_reply}{len(guild.voice_channels):,} Voice\n",
        )

        animated = sum(1 for e in guild.emojis if e.animated)
        static = sum(1 for e in guild.emojis if not e.animated)
        embed.add_field(
            name=f"{len(guild.emojis):,} Emojis",
            value=f"{self.bot.e_replies}{animated:,} Animated\n"
            f"{self.bot.e_reply}{static:,} Static\n",
        )

        embed.add_field(
            name=f"Level {guild.premium_tier}",
            value=f"{self.bot.e_replies}{len(guild.premium_subscribers):,} Boosters\n"
            f"{self.bot.e_reply}{guild.premium_subscription_count:,} Boosts",
        )

        owner_mention = guild.owner.mention if guild.owner else "\U00002754"
        embed.add_field(name="Owner", value=f"{self.bot.e_reply}{owner_mention}\n")

        embed.add_field(
            name="Roles", value=f"{self.bot.e_reply}{len(guild.roles):,} Roles\n"
        )

        embed.add_field(
            name="Images",
            value=", ".join(images) if images else "None",
        )

        embed.set_footer(text=f"ID: {guild.id} \nCreated at")
        await ctx.send(embed=embed)

    @serverinfo.command(name="icon")
    async def serverinfo_icon(
        self, ctx: Context, guild: Optional[discord.Guild] = None
    ):
        """Get the server icon."""
        guild = guild or ctx.guild

        if not guild:
            raise commands.GuildNotFound("Unknown guild")

        if not guild.icon:
            raise commands.GuildNotFound("Guild has no icon")

        file = await guild.icon.to_file(
            filename=f'icon.{"gif" if guild.icon.is_animated() else "png"}'
        )
        embed = discord.Embed(title=f"{guild.name}'s icon")
        embed.set_image(
            url=f'attachment://icon.{"gif" if guild.icon.is_animated() else "png"}'
        )
        await ctx.send(file=file, embed=embed)

    @serverinfo.command(name="banner")
    async def serverinfo_banner(
        self, ctx: Context, guild: Optional[discord.Guild] = None
    ):
        """Get the server banner."""
        guild = guild or ctx.guild

        if not guild:
            raise commands.GuildNotFound("Unknown guild")

        if not guild.banner:
            raise commands.GuildNotFound("Guild has no banner")

        file = await guild.banner.to_file(
            filename=f'banner.{"gif" if guild.banner.is_animated() else "png"}'
        )
        embed = discord.Embed(title=f"{guild.name}'s banner")
        embed.set_image(
            url=f'attachment://banner.{"gif" if guild.banner.is_animated() else "png"}'
        )
        await ctx.send(file=file, embed=embed)

    @serverinfo.command(name="splash")
    async def serverinfo_splash(
        self, ctx: Context, guild: Optional[discord.Guild] = None
    ):
        """Get the server splash."""
        guild = guild or ctx.guild

        if not guild:
            raise commands.GuildNotFound("Unknown guild")

        if not guild.splash:
            raise commands.GuildNotFound("Guild has no splash")

        file = await guild.splash.to_file(
            filename=f'splash.{"gif" if guild.splash.is_animated() else "png"}'
        )
        embed = discord.Embed(title=f"{guild.name}'s splash")
        embed.set_image(
            url=f'attachment://splash.{"gif" if guild.splash.is_animated() else "png"}'
        )
        await ctx.send(file=file, embed=embed)

    @commands.group(name="emojis", invoke_without_command=True)
    async def emojis(self, ctx: Context, guild: Optional[discord.Guild] = None):
        """Get the server emojis."""
        guild = guild or ctx.guild

        if not guild:
            raise commands.GuildNotFound("Unknown guild")

        if not guild.emojis:
            raise commands.GuildNotFound("Guild has no emojis")

        if ctx.bot.user is None:
            raise TypeError("Bot is not logged in.")

        order = sorted(guild.emojis, key=lambda e: e.created_at)

        data = [f"{str(e)} `<:{e.name}\u200b:{e.id}>`" for e in order]
        pages = SimplePages(entries=data, per_page=10, ctx=ctx)
        pages.embed.title = f"Emojis for {guild.name}"
        await pages.start(ctx)

    @emojis.command(name="advanced")
    async def emojis_advanced(
        self, ctx: Context, guild: Optional[discord.Guild] = None
    ):
        """Get the server emojis."""
        guild = guild or ctx.guild

        if not guild:
            raise commands.GuildNotFound("Unknown guild")

        if not guild.emojis:
            raise commands.GuildNotFound("Guild has no emojis")

        if ctx.bot.user is None:
            raise TypeError("Bot is not logged in.")

        order = sorted(guild.emojis, key=lambda e: e.created_at)

        entries = [
            (
                e.name,
                f'Preview: {str(e)} \nCreated: {discord.utils.format_dt(e.created_at, "D")} \nRaw: `<:{e.name}\u200b:{e.id}>`',
            )
            for e in order
        ]

        p = FieldPageSource(entries, per_page=2)
        p.embed.title = f"Emojis in {guild.name}"
        menu = Pager(p, ctx=ctx)
        await menu.start(ctx)

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

    @commands.command(name="banner")
    async def banner(self, ctx: Context, *, user: discord.User = commands.Author):
        """Shows a users banner"""

        user = await ctx.bot.fetch_user(user.id)
        if user.banner is None:
            raise TypeError("This user has no banner.")

        file = await user.banner.to_file(
            filename=f'banner.{"gif" if user.banner.is_animated() else "png"}'
        )
        embed = discord.Embed()
        embed.set_author(name=f"{str(user)}'s banner", icon_url=user.display_avatar.url)
        embed.set_image(
            url=f'attachment://banner.{"gif" if user.banner.is_animated() else "png"}'
        )
        await ctx.send(file=file, embed=embed)

    def text_channel_info(self, channel: discord.TextChannel) -> discord.Embed:
        embed = discord.Embed(timestamp=channel.created_at)
        embed.set_footer(text=f"ID: {channel.id} \nCreated at")

        def channel_emoji(channel: discord.TextChannel) -> str:
            if channel.is_nsfw():
                return "\U0001f51e "
            elif channel.is_news():
                return "\U0001f4e2 "
            else:
                return ""

        embed.set_author(
            name=f"{channel_emoji(channel)}{str(channel)}", url=channel.jump_url
        )

        if channel.topic:
            embed.description = channel.topic

        humans = sum(1 for m in channel.members if not m.bot)
        bots = sum(1 for m in channel.members if m.bot)
        embed.add_field(
            name=f"{len(channel.members):,} Members",
            value=f"{self.bot.e_replies}{humans:,} Humans\n"
            f"{self.bot.e_reply}{bots:,} Bots",
        )

        embed.add_field(
            name="Channels",
            value=f"Category: {channel.category}\n" f"Threads: {len(channel.threads)}",
        )
        # fix ugly placement if there is a long topic
        embed.add_field(name="\U00002800", value="\U00002800")

        return embed

    def voice_channel_info(self, channel: discord.VoiceChannel) -> discord.Embed:
        embed = discord.Embed(timestamp=channel.created_at)
        embed.set_footer(text=f"ID: {channel.id} \nCreated at")

        embed.set_author(name=str(channel), url=channel.jump_url)

        humans = sum(1 for m in channel.members if not m.bot)
        bots = sum(1 for m in channel.members if m.bot)
        embed.add_field(
            name=f"{len(channel.members):,} Members",
            value=f"{self.bot.e_replies}{humans:,} Humans\n"
            f"{self.bot.e_reply}{bots:,} Bots",
        )

        vc_limit = (
            "No User limit"
            if channel.user_limit == 0
            else f"{channel.user_limit:,} User limit"
        )
        embed.add_field(
            name="Details",
            value=f"{vc_limit}\n"
            f"{str(channel.bitrate)[:-3]}kbps\n"
            f"{channel.video_quality_mode.name.capitalize()} Video Quality",
        )
        return embed

    def category_channel_info(self, channel: discord.CategoryChannel) -> discord.Embed:
        embed = discord.Embed(timestamp=channel.created_at)
        embed.set_footer(text=f"ID: {channel.id} \nCreated at")

        embed.set_author(name=str(channel), url=channel.jump_url)

        embed.add_field(
            name=f"{len(channel.channels):,} Channels",
            value=f"{self.bot.e_reply}{len(channel.text_channels):,} Text\n"
            f"{self.bot.e_replies}{len(channel.voice_channels):,} Voice\n"
            f"{self.bot.e_reply}{len(channel.stage_channels):,} stage_channels",
        )
        return embed

    async def thread_channel_info(self, channel: discord.Thread) -> discord.Embed:
        embed = discord.Embed(timestamp=channel.created_at)
        embed.set_footer(text=f"ID: {channel.id} \nCreated at")

        embed.set_author(name=str(channel), url=channel.jump_url)

        members = await channel.fetch_members()
        embed.add_field(
            name=f"Members",
            value=f"{len(members):,}",
        )

        embed.add_field(
            name="Parent",
            value=f'{channel.parent.mention if channel.parent else "None"}',
        )

        embed.add_field(
            name="Owner",
            value=f'{channel.owner.mention if channel.owner else "None"}',
        )
        return embed

    def stage_channel_info(self, channel: discord.StageChannel) -> discord.Embed:
        embed = discord.Embed(timestamp=channel.created_at)
        embed.set_footer(text=f"ID: {channel.id} \nCreated at")

        embed.set_author(name=str(channel), url=channel.jump_url)

        if channel.topic:
            embed.description = channel.topic

        humans = sum(1 for m in channel.members if not m.bot)
        bots = sum(1 for m in channel.members if m.bot)
        embed.add_field(
            name=f"{len(channel.members):,} Members",
            value=f"{self.bot.e_replies}{len(channel.moderators):,} Moderators\n"
            f"{self.bot.e_replies}{humans:,} Humans\n"
            f"{self.bot.e_reply}{bots:,} Bots",
        )

        vc_limit = (
            "No User limit"
            if channel.user_limit == 0
            else f"{channel.user_limit:,} User limit"
        )
        embed.add_field(
            name="Details", value=f"{vc_limit}\n" f"{str(channel.bitrate)[:-3]}kbps\n"
        )

        return embed

    @commands.command(name="channelinfo", aliases=("ci",))
    async def channelinfo(
        self,
        ctx: Context,
        *,
        channel: Optional[
            Union[
                discord.TextChannel,
                discord.CategoryChannel,
                discord.VoiceChannel,
                discord.Thread,
                discord.StageChannel,
            ]
        ] = commands.CurrentChannel,
    ):
        """Shows information about a channel"""

        if isinstance(channel, discord.TextChannel):
            embed = self.text_channel_info(channel)

        elif isinstance(channel, discord.VoiceChannel):
            embed = self.voice_channel_info(channel)

        elif isinstance(channel, discord.CategoryChannel):
            embed = self.category_channel_info(channel)

        elif isinstance(channel, discord.Thread):
            embed = await self.thread_channel_info(channel)

        elif isinstance(channel, discord.StageChannel):
            embed = self.stage_channel_info(channel)

        else:
            raise commands.ChannelNotFound(str(channel))

        await ctx.send(embed=embed)

    @commands.command(name="roleinfo", aliases=("ri",))
    async def roleinfo(self, ctx: Context, *, role: Optional[discord.Role]):
        """Shows information about a role"""
        if not ctx.guild:
            raise commands.GuildNotFound("Unknown guild")

        role = (
            role or ctx.author.top_role
            if isinstance(ctx.author, discord.Member)
            else random.choice(ctx.guild.roles)
        )

        if role is None:
            return

        embed = discord.Embed(timestamp=role.created_at)
        embed.set_footer(text=f"ID: {role.id} \nCreated at")

        embed.set_author(
            name=str(role),
            icon_url=role.display_icon.url
            if role.display_icon and isinstance(role.display_icon, discord.Asset)
            else None,
        )

        bots = sum(1 for m in role.members if m.bot)
        humans = sum(1 for m in role.members if not m.bot)
        embed.add_field(
            name=f"{len(role.members):,} Members",
            value=f"{self.bot.e_replies}{humans:,} Humans\n"
            f"{self.bot.e_reply}{bots:,} Bots",
        )

        embed.add_field(
            name="Color",
            value=f"{self.bot.e_replies}{hex(role.color.value).replace('0x', '#')}\n"
            f"{self.bot.e_reply}{str(role.color.to_rgb()).replace('(', '').replace(')', '')}",
        )

        embed.add_field(
            name="Details",
            value=f"Hoisted: {ctx.yes_no(role.hoist)}\n"
            f"Mentionable: {ctx.yes_no(role.mentionable)}\n"
            f"Managed: {ctx.yes_no(role.managed)}\n",
        )

        await ctx.send(embed=embed)

    @commands.group(name="raw")
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def raw(self, ctx: Context):
        """Gets the raw data for an object"""

        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @raw.command(name="user")
    async def raw_user(self, ctx: Context, user: discord.User = commands.Author):
        """Gets the raw data for a user"""
        data = await self.bot.http.get_user(user.id)
        to_send = json.dumps(data, indent=4, sort_keys=True)

        if len(to_send) + 50 + len(str(user)) > 2000:
            file = ctx.too_big(to_send)
            await ctx.send("File too large", file=file)
            return

        await ctx.send(f"```Viewing raw data for {str(user)}``````json\n{to_send}\n```")

    @raw.command(name="member")
    async def raw_member(self, ctx: Context, member: discord.Member = commands.Author):
        """Gets the raw data for a member"""
        data = await self.bot.http.get_member(member.guild.id, member.id)
        to_send = json.dumps(data, indent=4, sort_keys=True)

        if len(to_send) + 50 + len(str(member)) > 2000:
            file = ctx.too_big(to_send)
            await ctx.send("File too large", file=file)
            return

        await ctx.send(
            f"```Viewing raw data for {str(member)}``````json\n{to_send}\n```"
        )

    @raw.command(name="message", aliases=("msg",))
    async def raw_message(self, ctx: Context):
        """Gets the raw data for a message"""

        if ctx.message.reference is None:
            return

        message = ctx.message.reference.resolved

        data = await self.bot.http.get_message(message.channel.id, message.id)  # type: ignore
        to_send = json.dumps(data, indent=4, sort_keys=True)

        if len(to_send) + 50 + len(str(message)) > 2000:
            file = ctx.too_big(to_send)
            await ctx.send("File too large", file=file)
            return

        await ctx.send(f"```json\n{to_send}\n```")

    @raw.command(name="guild")
    async def raw_guild(
        self, ctx: Context, guild: discord.Guild = commands.CurrentGuild
    ):
        """Gets the raw data for a guild"""

        data = await self.bot.http.get_guild(guild.id)
        to_send = json.dumps(data, indent=4, sort_keys=True)

        if len(to_send) + 50 + len(str(guild)) > 2000:
            file = ctx.too_big(to_send)
            await ctx.send("File too large", file=file)
            return

        await ctx.send(
            f"```Viewing raw data for {str(guild)}``````json\n{to_send}\n```"
        )

    @raw.command(name="channel")
    async def raw_channel(
        self,
        ctx: Context,
        channel: Union[
            discord.TextChannel,
            discord.VoiceChannel,
            discord.CategoryChannel,
            discord.StageChannel,
            discord.ForumChannel,
            discord.Thread,
        ] = commands.CurrentChannel,
    ):
        """Gets the raw data for a channel"""
        data = await self.bot.http.get_channel(channel.id)
        to_send = json.dumps(data, indent=4, sort_keys=True)

        if len(to_send) + 50 + len(str(channel)) > 2000:
            file = ctx.too_big(to_send)
            await ctx.send("File too large", file=file)
            return

        await ctx.send(f"```Viewing raw data for {channel}``````json\n{to_send}\n```")

    @commands.command(name="avatar", aliases=("pfp", "avy", "av"))
    async def avatar(
        self, ctx: Context, user: Union[discord.Member, discord.User] = commands.Author
    ):
        """Gets the avatar of a user"""
        embed = discord.Embed(
            color=self.bot.embedcolor
            if user.color == discord.Color.default()
            else user.color
        )
        embed.set_author(name=str(user), icon_url=user.display_avatar.url)

        embed.set_image(url=user.display_avatar.url)
        sql = """SELECT created_at FROM avatar_logs WHERE user_id = $1 ORDER BY created_at DESC"""
        latest_avatar = await self.bot.pool.fetchval(sql, user.id)

        if latest_avatar:
            embed.timestamp = latest_avatar
            embed.set_footer(text="Avatar changed")

        await ctx.send(
            embed=embed,
            view=AvatarView(ctx, user, embed, user.display_avatar),
            check_ref=True,
        )

    @commands.command(
        name="userinfo",
        aliases=(
            "ui",
            "whois",
        ),
    )
    async def userinfo(
        self,
        ctx: Context,
        *,
        user: Union[discord.Member, discord.User] = commands.Author,
    ):
        """Get information about a user."""
        if user is None:
            raise commands.UserNotFound(user)
        fuser = await ctx.bot.fetch_user(user.id)
        badges = await get_user_badges(member=user, fetched_user=fuser, ctx=ctx)

        embed = discord.Embed(timestamp=user.created_at)
        embed.description = user.mention

        filler = "\u2800" * 47
        embed.set_footer(text=f"{filler}\nID: {user.id} \nCreated at")

        embed.add_field(
            name="Badges", value="\n".join(badges) if badges else "\U0000274c No Badges"
        )

        images = []
        images.append(f"[Default Avatar]({user.default_avatar.url})")
        if user.avatar:
            images.append(f"[Avatar]({user.avatar.url})")

        if fuser.banner:
            images.append(f"[Banner]({fuser.banner.url})")

        name = str(user)
        if isinstance(user, discord.Member):
            if user.guild_avatar:
                images.append(f"[Guild Avatar]({user.guild_avatar.url})")

            if user.nick:
                name += f"  •  {user.nick}"

            if user.joined_at and ctx.guild:
                order = (
                    sorted(
                        ctx.guild.members,
                        key=lambda member: member.joined_at or discord.utils.utcnow(),
                    ).index(user)
                    + 1
                )
                embed.add_field(
                    name=f"Joined",
                    value=f"Position #{order:,}\n"
                    f'{discord.utils.format_dt(user.joined_at, "R")}\n'
                    f'{self.bot.e_reply}{discord.utils.format_dt(user.joined_at, "D")}\n',
                )

        embed.add_field(name="Images", value="\n".join(images))

        embed.set_author(name=name, icon_url=user.display_avatar.url)

        await ctx.send(
            embed=embed,
            view=UserInfoView(ctx, user, embed),
        )


class UserInfoDropdown(discord.ui.Select):
    def __init__(
        self,
        ctx: Context,
        user: Union[discord.Member, discord.User],
        original_embed: discord.Embed,
    ):
        self.user = user
        self.ctx: Context = ctx
        self.original_embed = original_embed

        # Set the options that will be presented inside the dropdown
        options = [
            discord.SelectOption(
                label="Index",
                description=f"Goes back to home page",
                emoji="\U0001f3e0",
                value="index",
            ),
        ]
        member_options = [
            discord.SelectOption(
                label="Roles",
                description=f"{user.name}'s roles",
                emoji="\U0001f9fb",
                value="roles",
            ),
            discord.SelectOption(
                label="Devices",
                description=f"{user.name}'s current devices",
                emoji="\U0001f5a5",
                value="devices",
            ),
            discord.SelectOption(
                label="Permissions",
                description=f"{user.name}'s permissions",
                emoji="<:certified_moderator:949147443264622643>",
                value="perms",
            ),
        ]
        bot_options = [
            discord.SelectOption(
                label="Bot Specific",
                description=f"{user.name}'s bot information",
                emoji="\U0001f916",
                value="botinfo",
            ),
        ]

        if user.bot:
            options.extend(bot_options)

        if isinstance(user, discord.Member):
            options.extend(member_options)

        super().__init__(min_values=1, max_values=1, options=options)

    async def role_callback(
        self, member: discord.Member, ctx: Context
    ) -> discord.Embed:
        roles = sorted(member.roles, key=lambda role: role.position, reverse=True)
        roles = [
            role.mention if role.id != ctx.guild.id else "`@everyone`" for role in roles
        ]
        embed = discord.Embed(
            color=ctx.bot.embedcolor,
            description="\n".join(roles),
        )
        embed.set_author(
            name=f"{member.name}'s roles", icon_url=member.display_avatar.url
        )
        embed.set_footer(text="\u2800" * 47)
        return embed

    async def device_callback(
        self, member: discord.Member, ctx: Context
    ) -> discord.Embed:
        device_types = {
            "dnd": "Do Not Disturb",
            "idle": "Idle",
            "offline": "Offline",
            "online": "Online",
        }

        devices = [
            f"**Desktop**: {device_types[member.desktop_status.value]}",
            f"**Mobile**: {device_types[member.mobile_status.value]}",
            f"**Web**: {device_types[member.web_status.value]}",
        ]

        embed = discord.Embed(
            color=ctx.bot.embedcolor,
            description="\n".join(devices),
        )
        embed.set_author(
            name=f"{member.name}'s devices", icon_url=member.display_avatar.url
        )
        embed.set_footer(text="\u2800" * 47)
        return embed

    async def perms_callback(
        self, member: discord.Member, ctx: Context
    ) -> discord.Embed:
        permissions = member.guild_permissions

        allowed = []
        for name, value in permissions:
            name = name.replace("_", " ").replace("guild", "server").title()
            if value:
                allowed.append(name)

        embed = discord.Embed(
            color=ctx.bot.embedcolor,
            description="\n".join(allowed),
        )
        embed.set_author(
            name=f"{member.name}'s permissions", icon_url=member.display_avatar.url
        )

        embed.set_footer(text="\u2800" * 47)
        return embed

    async def botinfo_callback(
        self, user: Union[discord.Member, discord.User], ctx: Context
    ) -> discord.Embed:
        url = f"https://discord.com/api/v10/oauth2/applications/{user.id}/rpc"
        async with self.ctx.bot.session.get(url) as r:
            if r.status != 200:
                raise commands.BadArgument(f"Unable to fetch info on {user}")

            results = await r.json()

        embed = discord.Embed(color=ctx.bot.embedcolor)
        embed.set_author(
            name=f"{user.name}'s bot information", icon_url=user.display_avatar.url
        )
        embed.set_footer(text="\u2800" * 47)

        embed.description = results["description"]

        embed.add_field(
            name="Bot Info",
            value=f"Public: {ctx.yes_no(results['bot_public'])}\n"
            f"Require Code Grant: {ctx.yes_no(results['bot_require_code_grant'])}\n",
        )
        if results.get("tags"):
            embed.add_field(
                name="Tags",
                value="\n".join(
                    f"`{tag}`"
                    for tag in sorted(
                        results["tags"], key=lambda x: len(x), reverse=True
                    )
                ),
            )

        admin_perms = discord.Permissions.none()
        admin_perms.administrator = True
        invite_perms = [
            ("Advanced", discord.Permissions.advanced()),
            ("None", discord.Permissions.none()),
            ("All", discord.Permissions.all()),
            ("General", discord.Permissions.general()),
            ("Administrator", admin_perms),
        ]

        embed.add_field(
            name="Invites",
            value=f"\n".join(
                f"[`{name}`]({discord.utils.oauth_url(user.id, permissions=perms)})"
                for name, perms in sorted(
                    invite_perms, key=lambda x: len(x[0]), reverse=True
                )
            ),
        )

        return embed

    async def callback(self, interaction: discord.Interaction):
        ctx = self.ctx
        member = self.user

        if ctx.guild is None:
            raise commands.GuildNotFound("Unknown guild")

        if self.values[0] == "roles":
            if not isinstance(member, discord.Member):
                return
            embed = await self.role_callback(member, ctx)

        elif self.values[0] == "devices":
            if not isinstance(member, discord.Member):
                return
            embed = await self.device_callback(member, ctx)

        elif self.values[0] == "perms":
            if not isinstance(member, discord.Member):
                return
            embed = await self.perms_callback(member, ctx)

        elif self.values[0] == "botinfo":
            embed = await self.botinfo_callback(member, ctx)

        else:
            embed = self.original_embed

        await interaction.response.edit_message(embed=embed)


class UserInfoView(AuthorView):
    def __init__(
        self,
        ctx: Context,
        user: Union[discord.Member, discord.User],
        original_embed: discord.Embed,
    ):
        super().__init__(ctx)
        self.ctx = ctx
        self.user = user
        if isinstance(user, discord.Member) or user.bot:
            self.add_item(UserInfoDropdown(ctx, user, original_embed))

        if not isinstance(user, discord.Member):
            self.nicknames.disabled = True

        self.avyh_check: bool = False
        self.usernames_check: bool = False
        self.nicknames_check: bool = False

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, __
    ) -> None:
        if isinstance(error, commands.BadArgument):
            await interaction.response.send_message(content=str(error), ephemeral=True)
        else:
            await interaction.response.send_message(
                "Oops! Something went wrong.", ephemeral=True
            )

        self.ctx.bot.logger.error(error)

    @discord.ui.button(label="Avatar History", row=2, style=discord.ButtonStyle.blurple)
    async def avyh(self, interaction: discord.Interaction, __):
        command = self.ctx.bot.get_command("avyh")
        if command is None:
            return

        if self.avyh_check:
            await interaction.response.defer()
            return

        self.avyh_check = True
        await interaction.response.defer()
        await command(self.ctx, user=self.user)

    @discord.ui.button(
        label="Username History", row=2, style=discord.ButtonStyle.blurple
    )
    async def usernames(self, interaction: discord.Interaction, __):
        command = self.ctx.bot.get_command("usernames")
        if command is None:
            return

        if self.usernames_check:
            await interaction.response.defer()
            return

        self.usernames_check = True
        await interaction.response.defer()
        await command(self.ctx, user=self.user)

    @discord.ui.button(
        label="Nickname History", row=2, style=discord.ButtonStyle.blurple
    )
    async def nicknames(self, interaction: discord.Interaction, __):
        command = self.ctx.bot.get_command("nicknames")
        if command is None:
            return

        if self.nicknames_check:
            await interaction.response.defer()
            return

        self.nicknames_check = True
        await interaction.response.defer()
        await command(self.ctx, user=self.user)


class AvatarView(AuthorView):
    def __init__(
        self,
        ctx: Context,
        user: discord.Member | discord.User,
        embed: discord.Embed,
        asset: discord.Asset,
    ):
        super().__init__(ctx)
        self.user = user
        self.ctx = ctx
        self.embed = embed
        self.asset = asset
        self.files: List[discord.File] = []

        self.server.disabled = (
            not isinstance(user, discord.Member)
            or isinstance(user, discord.Member)
            and user.guild_avatar is None
        )
        self.avatar.disabled = user.avatar is None

    async def edit_message(self, interaction: discord.Interaction):
        self.embed.set_image(url=self.asset.url)
        await interaction.response.edit_message(embed=self.embed)

    @discord.ui.button(label="Avatar", row=0, style=discord.ButtonStyle.blurple)
    async def avatar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.user.avatar is None:
            return

        self.asset = self.user.avatar

        await self.edit_message(interaction)

    @discord.ui.button(label="Default", row=0, style=discord.ButtonStyle.blurple)
    async def default(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        self.asset = self.user.default_avatar

        await self.edit_message(interaction)

    @discord.ui.button(label="Server", row=0, style=discord.ButtonStyle.blurple)
    async def server(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(self.user, discord.Member) or self.user.guild_avatar is None:
            return

        self.asset = self.user.guild_avatar

        await self.edit_message(interaction)

    async def check_asset(self, interaction: discord.Interaction) -> bool:
        if self.asset.url.startswith("https://cdn.discordapp.com/embed"):
            await interaction.response.send_message(
                "This is a default discord avatar and cannot be changed.",
                ephemeral=True,
            )
            return False

        return True

    @discord.ui.button(label="Quality", row=2, style=discord.ButtonStyle.green)
    async def quality(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        results = await self.check_asset(interaction)
        if interaction.message is None or not results:
            return

        await interaction.message.edit(
            view=QualityView(self.ctx, self.user, self.embed, self.asset)
        )
        await interaction.response.defer()

    @discord.ui.button(label="Format", row=2, style=discord.ButtonStyle.green)
    async def _format(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        results = await self.check_asset(interaction)
        if interaction.message is None or not results:
            return

        await interaction.message.edit(
            view=FormatView(self.ctx, self.user, self.embed, self.asset)
        )
        await interaction.response.defer()

    @discord.ui.button(label="Save", row=2, style=discord.ButtonStyle.green)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.message is None:
            return

        file_type = re.search(r".(jpg|png|webp|jpeg|gif)", self.asset.url)

        if file_type is None:
            return

        avatar_file = await self.asset.to_file(filename=f"avatar.{file_type.group()}")
        self.embed.set_image(url=f"attachment://avatar.{file_type.group()}")
        await interaction.message.edit(
            view=None, embed=self.embed, attachments=[avatar_file]
        )
        await interaction.response.defer()


class QualityView(AuthorView):
    def __init__(
        self,
        ctx: Context,
        user: Union[discord.Member, discord.User],
        embed: discord.Embed,
        asset: discord.Asset,
    ):
        super().__init__(ctx)
        self.user = user
        self.ctx = ctx
        self.embed = embed
        self.asset = asset
        self.add_item(QualityDropdown(ctx, user, embed, asset))

    @discord.ui.button(label="Go back", row=1, style=discord.ButtonStyle.red)
    async def go_back(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.message is None:
            return

        await interaction.message.edit(
            view=AvatarView(self.ctx, self.user, self.embed, self.asset)
        )
        await interaction.response.defer()


class QualityDropdown(discord.ui.Select):
    view: QualityView

    def __init__(
        self,
        ctx: Context,
        user: Union[discord.Member, discord.User],
        embed: discord.Embed,
        asset: discord.Asset,
    ):
        self.ctx = ctx
        self.user = user
        self.embed = embed

        valid_sizes = ["16", "32", "64", "128", "256", "512", "1024", "2048", "4096"]
        options = [
            discord.SelectOption(label=f"{size}px", value=size) for size in valid_sizes
        ]

        super().__init__(
            min_values=1, max_values=1, options=options, placeholder="Select a size"
        )

    async def callback(self, interaction: discord.Interaction):
        value = int(self.values[0])

        self.view.asset = self.view.asset.with_size(value)
        if interaction.message is None:
            return

        self.embed.set_image(url=self.view.asset.url)

        await interaction.message.edit(embed=self.embed)
        await interaction.response.defer()


class FormatView(AuthorView):
    def __init__(
        self,
        ctx: Context,
        user: Union[discord.Member, discord.User],
        embed: discord.Embed,
        asset: discord.Asset,
    ):
        super().__init__(ctx)
        self.user = user
        self.ctx = ctx
        self.embed = embed
        self.asset = asset
        self.add_item(FormatDropdown(ctx, user, embed, asset))

    @discord.ui.button(label="Go back", row=1, style=discord.ButtonStyle.red)
    async def go_back(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.message is None:
            return

        await interaction.message.edit(
            view=AvatarView(self.ctx, self.user, self.embed, self.asset)
        )
        await interaction.response.defer()


class FormatDropdown(discord.ui.Select):
    view: FormatView

    def __init__(
        self,
        ctx: Context,
        user: Union[discord.Member, discord.User],
        embed: discord.Embed,
        asset: discord.Asset,
    ):
        self.ctx = ctx
        self.user = user
        self.embed = embed
        self.asset = asset

        valid_formats = ["webp", "jpeg", "jpg", "png"]

        if asset.is_animated():
            valid_formats.append("gif")

        options = [
            discord.SelectOption(label=_format, value=_format)
            for _format in valid_formats
        ]

        super().__init__(
            min_values=1, max_values=1, options=options, placeholder="Select a format"
        )

    async def callback(self, interaction: discord.Interaction):
        value = self.values[0]

        self.view.asset = self.view.asset.with_format(value)  # type: ignore
        if interaction.message is None:
            return

        self.embed.set_image(url=self.view.asset.url)

        await interaction.message.edit(embed=self.embed)
        await interaction.response.defer()
