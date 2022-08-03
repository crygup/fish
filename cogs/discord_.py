from __future__ import annotations

import argparse
import json
import math
import random
import shlex
import time
from io import BytesIO
from typing import (
    TYPE_CHECKING,
    Dict,
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
)

from cogs.context import Context

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

    @commands.command(name="emoji")
    async def emoji(
        self,
        ctx: Context,
        emoji: Optional[Union[discord.Emoji, discord.PartialEmoji, str]],
    ):
        """Gets information about an emoji."""

        if ctx.message.reference is None and emoji is None:
            raise commands.BadArgument("No emoji provided.")

        if isinstance(emoji, str):
            _emoji = await ctx.get_twemoji(str(emoji))

            if _emoji is None:
                raise commands.BadArgument("No emoji found.")

            await ctx.send(file=discord.File(BytesIO(_emoji), filename=f"emoji.png"))
            return

        if emoji:
            emoji = emoji

        else:
            if not ctx.message.reference:
                raise commands.BadArgument("No emoji provided.")

            reference = ctx.message.reference.resolved

            if (
                isinstance(reference, discord.DeletedReferencedMessage)
                or reference is None
            ):
                raise commands.BadArgument("No emoji found.")

            emoji = (await EmojiConverter().from_message(ctx, reference.content))[0]

        if emoji is None:
            raise TypeError("No emoji found.")

        embed = discord.Embed(timestamp=emoji.created_at, title=emoji.name)

        if isinstance(emoji, discord.Emoji) and emoji.guild:
            if not emoji.available:
                embed.title = f"~~{emoji.name}~~"

            embed.add_field(
                name="Guild",
                value=f"{ctx.bot.e_replies}{str(emoji.guild)}\n"
                f"{ctx.bot.e_reply}{emoji.guild.id}",
            )

            femoji = await emoji.guild.fetch_emoji(emoji.id)
            if femoji.user:
                embed.add_field(
                    name="Created by",
                    value=f"{ctx.bot.e_replies}{str(femoji.user)}\n"
                    f"{ctx.bot.e_reply}{femoji.user.id}",
                )

        embed.add_field(
            name="Raw text", value=f"`<:{emoji.name}\u200b:{emoji.id}>`", inline=False
        )

        embed.set_footer(text=f"ID: {emoji.id} \nCreated at")
        embed.set_image(url=f'attachment://emoji.{"gif" if emoji.animated else "png"}')
        file = await emoji.to_file(
            filename=f'emoji.{"gif" if emoji.animated else "png"}'
        )
        await ctx.send(embed=embed, file=file)

    @commands.command(name="search")
    async def search(self, ctx: Context, *, args: Optional[str] = None):
        """Search for a member with certain flags.

        Flags:
        `-starts`: Name starts with x
        `-ends`: Name ends with x
        `-contains`: Names contains x
        `-length`: Length of name
        `-name`: Name matches x
        `-discrim`: Discriminator matches x
        `-role`: Has role

        Flags (no arguments):
        `-nick`: Nicknames only
        `-no_avatar`: No avatar
        `-spotify`: Listening to spotify
        `-online`: Online status
        `-dnd`: Do not disturb status
        `-idle`: Idle status
        `-offline`: Offline status
        `-streaming`: Streaming status
        `-mobile`: Mobile status
        `-desktop`: Desktop status
        `-web`: Web status
        `-bots`: Bots
        `-voice`: Current in a voice channel

        Example:
        ```
        cr search -name liz -discrim 0333 -length 3 -nick
        ```
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

        embed = discord.Embed(timestamp=user.created_at, description=user.mention)
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
                name += f"  • {user.nick}"
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
            view=UserInfoView(ctx, user, embed)
            if isinstance(user, discord.Member)
            else None,
        )

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

        await ctx.send(
            f"```Viewing raw data for {str(channel)}``````json\n{to_send}\n```"
        )


class UserInfoDropdown(discord.ui.Select):
    def __init__(
        self, ctx: Context, member: discord.Member, original_embed: discord.Embed
    ):
        self.member = member
        self.ctx = ctx
        self.original_embed = original_embed

        # Set the options that will be presented inside the dropdown
        options = [
            discord.SelectOption(
                label="Index",
                description=f"Goes back to home page",
                emoji="\U0001f3e0",
                value="index",
            ),
            discord.SelectOption(
                label="Roles",
                description=f"{member.name}'s roles",
                emoji="\U0001f9fb",
                value="roles",
            ),
            discord.SelectOption(
                label="Devices",
                description=f"{member.name}'s current devices",
                emoji="\U0001f5a5",
                value="devices",
            ),
            discord.SelectOption(
                label="Permissions",
                description=f"{member.name}'s permissions",
                emoji="<:certified_moderator:949147443264622643>",
                value="perms",
            ),
        ]

        super().__init__(min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        ctx = self.ctx
        member = self.member
        if ctx.guild is None:
            raise commands.GuildNotFound("Unknown guild")
        if self.values[0] == "roles":
            roles = sorted(member.roles, key=lambda role: role.position, reverse=True)
            roles = [
                role.mention if role.id != ctx.guild.id else "`@everyone`"
                for role in roles
            ]
            embed = discord.Embed(
                color=ctx.bot.embedcolor,
                description="\n".join(roles),
            )
            embed.set_author(
                name=f"{member.name}'s roles", icon_url=member.display_avatar.url
            )
            embed.set_footer(text="\u2800" * 47)

        elif self.values[0] == "devices":
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

        elif self.values[0] == "perms":
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

        else:
            embed = self.original_embed

        await interaction.response.edit_message(embed=embed)


class UserInfoView(discord.ui.View):
    def __init__(
        self, ctx: Context, member: discord.Member, original_embed: discord.Embed
    ):
        super().__init__()
        self.ctx = ctx
        self.add_item(UserInfoDropdown(ctx, member, original_embed))

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user and interaction.user == self.ctx.author:
            return True
        await interaction.response.send_message(
            f'You can\'t use this, sorry. \nIf you\'d like to use this then run the command `{self.ctx.command}{self.ctx.invoked_subcommand or ""}`',
            ephemeral=True,
        )
        return False
