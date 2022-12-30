from __future__ import annotations
import asyncio

import datetime
import imghdr
import random
import re
from io import BytesIO
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple, Union

import asyncpg
import discord
from discord.ext import commands

from utils import (
    REPLIES,
    REPLY,
    EmojiConverter,
    GuildChannel,
    InfoArgument,
    NotTenorUrl,
    TenorUrlConverter,
    UserInfoView,
    emoji_extras,
    format_status,
    get_twemoji,
    get_user_badges,
    human_join,
    to_bytes,
    format_bytes,
    BlankException,
    Pager,
    AvatarsPageSource,
    FieldPageSource,
)

from ._base import CogBase

if TYPE_CHECKING:
    from bot import Bot
    from cogs.context import Context


class InfoCommands(CogBase):
    def __init__(self, bot: Bot):
        self.bot = bot

    async def user_info(self, ctx: Context, user: Union[discord.Member, discord.User]):
        fetched_user = await ctx.bot.fetch_user(user.id)
        badges = await get_user_badges(member=user, fetched_user=fetched_user, ctx=ctx)

        embed = discord.Embed(timestamp=user.created_at)
        embed.description = user.mention

        filler = "\u2800" * 47
        embed.set_footer(text=f"{filler}\nID: {user.id} \nCreated at")

        embed.add_field(
            name="Badges", value="\n".join(badges) if badges else "\U0000274c No Badges"
        )

        name = str(user)
        if isinstance(user, discord.Member):
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
                    f'{REPLY}{discord.utils.format_dt(user.joined_at, "D")}\n',
                )

            results: Optional[datetime.datetime] = await self.bot.pool.fetchval(
                "SELECT time FROM uptime_logs WHERE user_id = $1", user.id
            )
            formatted = (
                f'{discord.utils.format_dt(results, "D")}\n{REPLY}{discord.utils.format_dt(results, "R")}'
                if results
                else "As long as I remember."
            )
            embed.add_field(
                name=f"{format_status(user)} {'since' if results else ''}".capitalize(),
                value=formatted,
            )

        embed.set_author(name=name, icon_url=user.display_avatar.url)

        await ctx.send(
            embed=embed,
            view=UserInfoView(ctx, user, fetched_user, embed),
        )

    @commands.command(
        name="userinfo",
        aliases=(
            "ui",
            "whois",
        ),
    )
    async def user_info_command(
        self,
        ctx: Context,
        *,
        user: Union[discord.Member, discord.User] = commands.Author,
    ):
        """Get information about a user."""

        await self.user_info(ctx, user)

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
            value=f"{REPLIES}{humans:,} Humans\n" f"{REPLY}{bots:,} Bots",
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
            value=f"{REPLIES}{humans:,} Humans\n" f"{REPLY}{bots:,} Bots",
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
            value=f"{REPLY}{len(channel.text_channels):,} Text\n"
            f"{REPLIES}{len(channel.voice_channels):,} Voice\n"
            f"{REPLY}{len(channel.stage_channels):,} stage_channels",
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
            value=f"{REPLIES}{len(channel.moderators):,} Moderators\n"
            f"{REPLIES}{humans:,} Humans\n"
            f"{REPLY}{bots:,} Bots",
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
        channel = channel or ctx.channel

        type_to_info = {
            discord.TextChannel: self.text_channel_info,
            discord.CategoryChannel: self.category_channel_info,
            discord.VoiceChannel: self.voice_channel_info,
            discord.Thread: self.thread_channel_info,
            discord.StageChannel: self.stage_channel_info,
        }

        if type(channel) not in type_to_info:
            commands.ChannelNotFound(str(channel))

        embed = type_to_info[type(channel)](channel)

        await ctx.send(embed=embed)

    async def server_info(self, ctx: Context, guild: discord.Guild):
        if ctx.bot.user is None:
            return

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
            value=f"{REPLIES}{humans:,} Humans\n" f"{REPLY}{bots:,} Bots",
        )

        embed.add_field(
            name=f"{len(guild.channels):,} Channels",
            value=f"{REPLIES}{len(guild.text_channels):,} Text\n"
            f"{REPLY}{len(guild.voice_channels):,} Voice\n",
        )

        animated = sum(1 for e in guild.emojis if e.animated)
        static = sum(1 for e in guild.emojis if not e.animated)
        embed.add_field(
            name=f"{len(guild.emojis):,} Emojis",
            value=f"{REPLIES}{animated:,} Animated\n" f"{REPLY}{static:,} Static\n",
        )

        embed.add_field(
            name=f"Level {guild.premium_tier}",
            value=f"{REPLIES}{len(guild.premium_subscribers):,} Boosters\n"
            f"{REPLY}{guild.premium_subscription_count:,} Boosts",
        )

        owner_mention = guild.owner.mention if guild.owner else "\U00002754"
        embed.add_field(name="Owner", value=f"{REPLY}{owner_mention}\n")

        embed.add_field(name="Roles", value=f"{REPLY}{len(guild.roles):,} Roles\n")

        embed.add_field(
            name="Images",
            value=", ".join(images) if images else "None",
        )

        embed.set_footer(text=f"ID: {guild.id} \nCreated at")
        await ctx.send(embed=embed)

    @commands.group(
        name="serverinfo",
        aliases=("si", "server", "guild", "guildinfo", "gi"),
        invoke_without_command=True,
    )
    async def server_info_command(
        self, ctx: Context, *, guild: Optional[discord.Guild] = None
    ):
        """Get information about a server."""

        guild = guild or ctx.guild

        await self.server_info(ctx, guild)

    @server_info_command.command(name="icons")
    async def server_info_icons(
        self, ctx: Context, *, guild: Optional[discord.Guild] = commands.CurrentGuild
    ):
        """Shows all of a guilds icons"""
        guild = guild or ctx.guild

        sql = (
            """SELECT * FROM guild_icons WHERE guild_id = $1 ORDER BY created_at DESC"""
        )
        results = await self.bot.pool.fetch(sql, guild.id)

        if results == []:
            raise BlankException(f"{guild} has no icon history on record.")

        entries: List[Tuple[str, datetime.datetime]] = [
            (
                r["icon"],
                r["created_at"],
            )
            for r in results
        ]

        source = AvatarsPageSource(entries=entries)
        source.embed.color = self.bot.embedcolor
        source.embed.title = f"Icons for {guild}"
        pager = Pager(source, ctx=ctx)
        await pager.start(ctx)

    @server_info_command.command("iconhistory", aliases=("iconh",))
    async def serverinfo_iconhistory(
        self, ctx: Context, *, guild: Optional[discord.Guild] = commands.CurrentGuild
    ):
        """Shows the icon history of a guild.

        This will only show the first 100, to view them all and in HD run the command `serverinfo icons`
        """
        guild = guild or ctx.guild

        async with ctx.typing():
            sql = """
            SELECT * FROM guild_icons WHERE guild_id = $1
            ORDER BY created_at DESC LIMIT 100
            """

            records: List[asyncpg.Record] = await self.bot.pool.fetch(
                sql,
                guild.id,
            )

            if records == []:
                raise BlankException(f"{guild} has no icon history on record.")

            icons = await asyncio.gather(
                *[to_bytes(ctx.session, row["icon"]) for row in records]
            )

            fp = await format_bytes(ctx.guild.filesize_limit, icons)
            file = discord.File(
                fp,
                f"{guild.id}_icon_history.png",
            )

        if len(records) >= 100:
            first_icon: datetime.datetime = await self.bot.pool.fetchval(
                """SELECT created_at FROM guild_icons WHERE guild_id = $1 ORDER BY created_at ASC""",
                guild.id,
            )
        else:
            first_icon = records[-1]["created_at"]

        embed = discord.Embed(timestamp=first_icon)
        embed.set_footer(text="First icon saved")
        embed.set_author(
            name=f"{guild}'s icon history",
            icon_url=guild.icon.url if guild.icon else None,
        )
        embed.set_image(url=f"attachment://{guild.id}_icon_history.png")

        await ctx.send(embed=embed, file=file)

    @server_info_command.command(name="names")
    async def serverinfo_names(
        self, ctx: Context, *, guild: Optional[discord.Guild] = commands.CurrentGuild
    ):
        """Shows the past names for a guild"""
        guild = guild or ctx.guild

        results = await self.bot.pool.fetch(
            "SELECT * FROM guild_name_logs WHERE guild_id = $1 ORDER BY created_at DESC",
            guild.id,
        )

        if results == []:
            raise BlankException(f"I have no name history on record for {guild}.")

        entries = [
            (
                r["name"],
                f'{discord.utils.format_dt(r["created_at"], "R")}  |  {discord.utils.format_dt(r["created_at"], "d")}',
            )
            for r in results
        ]

        source = FieldPageSource(entries=entries)
        source.embed.color = self.bot.embedcolor
        source.embed.title = f"Names for {guild}"
        pager = Pager(source, ctx=ctx)
        await pager.start(ctx)

    @server_info_command.command(name="icon")
    async def serverinfo_icon(
        self, ctx: Context, *, guild: Optional[discord.Guild] = None
    ):
        """Get the server icon."""
        guild = guild or ctx.guild

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

    @server_info_command.command(name="banner")
    async def serverinfo_banner(
        self, ctx: Context, *, guild: Optional[discord.Guild] = None
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

    @server_info_command.command(name="splash")
    async def serverinfo_splash(
        self, ctx: Context, *, guild: Optional[discord.Guild] = None
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

    async def view_as(self, ctx: Context, member: discord.Member, guild: discord.Guild):
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

    @commands.command(
        name="view_as",
        aliases=(
            "viewas",
            "va",
        ),
    )
    async def view_as_command(
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

        await self.view_as(ctx, member, guild)

    async def role_info(self, ctx: Context, role: discord.Role):
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
            value=f"{REPLIES}{humans:,} Humans\n" f"{REPLY}{bots:,} Bots",
        )

        embed.add_field(
            name="Color",
            value=f"{REPLIES}{hex(role.color.value).replace('0x', '#')}\n"
            f"{REPLY}{str(role.color.to_rgb()).replace('(', '').replace(')', '')}",
        )

        embed.add_field(
            name="Details",
            value=f"Hoisted: {ctx.yes_no(role.hoist)}\n"
            f"Mentionable: {ctx.yes_no(role.mentionable)}\n"
            f"Managed: {ctx.yes_no(role.managed)}\n",
        )

        await ctx.send(embed=embed)

    @commands.command(name="roleinfo", aliases=("ri",))
    async def roleinfo(
        self, ctx: Context, *, role: discord.Role = commands.CurrentChannel
    ):
        """Shows information about a role"""
        role = (
            role or ctx.author.top_role
            if isinstance(ctx.author, discord.Member)
            else random.choice(ctx.guild.roles)
        )

        await self.role_info(ctx, role)

    @commands.command(name="info")
    async def info(self, ctx: Context, object: InfoArgument = commands.Author):
        if isinstance(object, (discord.Member, discord.User)):
            await self.user_info(ctx, object)
        elif isinstance(object, discord.Role):
            await self.role_info(ctx, object)
        elif isinstance(object, discord.Guild):
            await self.server_info(ctx, object)
        elif isinstance(object, discord.TextChannel):
            embed = self.text_channel_info(object)
            await ctx.send(embed=embed)
        elif isinstance(object, discord.VoiceChannel):
            embed = self.voice_channel_info(object)
            await ctx.send(embed=embed)
        elif isinstance(object, discord.CategoryChannel):
            embed = self.category_channel_info(object)
            await ctx.send(embed=embed)
        elif isinstance(object, discord.Thread):
            embed = self.thread_channel_info(object)
            await ctx.send(embed=embed)
        else:
            await ctx.send("Invalid argument")

    @commands.group(name="emoji", invoke_without_command=True)
    async def emoji(
        self,
        ctx: Context,
        emoji: Optional[Union[discord.Emoji, discord.PartialEmoji, str]],
    ):
        """Gets information about an emoji."""

        if ctx.message.reference is None and emoji is None:
            raise commands.BadArgument("No emoji provided.")

        if isinstance(emoji, str):
            _emoji = await get_twemoji(ctx.session, str(emoji))

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
                value=f"{REPLIES}{str(emoji.guild)}\n" f"{REPLY}{emoji.guild.id}",
            )

            femoji = await emoji.guild.fetch_emoji(emoji.id)
            if femoji.user:
                embed.add_field(
                    name="Created by",
                    value=f"{REPLIES}{str(femoji.user)}\n" f"{REPLY}{femoji.user.id}",
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

    @emoji.command(name="create", extras=emoji_extras)
    @commands.has_guild_permissions(manage_emojis=True)
    @commands.bot_has_guild_permissions(manage_emojis=True)
    async def emoji_create(
        self,
        ctx: Context,
        name,
        *,
        image: Union[discord.Emoji, discord.PartialEmoji, discord.Attachment, str],
    ):
        if isinstance(image, (discord.Emoji, discord.PartialEmoji)):
            try:
                to_upload = await image.read()
            except ValueError:
                raise TypeError("Image is not a custom emoji.")
        elif isinstance(image, discord.Attachment):
            to_upload = await image.read()
        else:
            try:
                url = await TenorUrlConverter().convert(ctx, image)

            except NotTenorUrl:
                url = image

            async with ctx.bot.session.get(url) as resp:
                file = await resp.read()
                if imghdr.what(BytesIO(file)):  # type: ignore # tested and works shut up
                    to_upload = file
                else:
                    raise ValueError("Invalid image supplied.")

        try:
            emoji = await ctx.guild.create_custom_emoji(
                name=name,
                image=to_upload,
                reason=f"Created by {ctx.author} ({ctx.author.id})",
            )
        except discord.HTTPException as e:
            await ctx.send(str(e))
            return

        await ctx.send(f"Successfully created {emoji}")

    @emoji.command(name="rename", extras=emoji_extras)
    @commands.has_guild_permissions(manage_emojis=True)
    @commands.bot_has_guild_permissions(manage_emojis=True)
    async def emoji_rename(self, ctx: Context, emoji: discord.Emoji, *, name: str):
        pattern = re.compile(r"[a-zA-Z0-9_ ]")
        if not pattern.match(name):
            raise commands.BadArgument(
                "Name can only contain letters, numbers, and underscores."
            )

        await emoji.edit(name=name)
        await ctx.send(f"Renamed {emoji} to **`{name}`**.")

    @emoji.command(name="delete", extras=emoji_extras)
    @commands.has_guild_permissions(manage_emojis=True)
    @commands.bot_has_guild_permissions(manage_emojis=True)
    async def emoji_delete(self, ctx: Context, *emojis: discord.Emoji):
        value = await ctx.prompt(
            f"Are you sure you want to delete {len(emojis):,} emoji{'s' if len(emojis) > 1 else ''}?"
        )
        if not value:
            await ctx.send(
                f"Well I didn't want to delete {'them' if len(emojis) > 1 else 'it'} anyway."
            )
            return

        message = await ctx.send(
            f"Deleting {len(emojis):,} emoji{'s' if len(emojis) > 1 else ''}..."
        )

        if message is None:
            return

        deleted_emojis = []

        for emoji in emojis:
            deleted_emojis.append(f"`{emoji}`")
            await emoji.delete()
            await message.edit(
                content=f"Successfully deleted {human_join(deleted_emojis, final='and')} *({len(deleted_emojis)}/{len(emojis)})*."
            )
