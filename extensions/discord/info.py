from __future__ import annotations

import datetime
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Literal,
    Optional,
    Tuple,
    TypeAlias,
    Union,
)

import discord
from discord.ext import commands
from discord.interactions import Interaction

from core import Cog
from extensions.context import Context
from utils import USER_FLAGS, AuthorView, human_join, reply

if TYPE_CHECKING:
    from extensions.context import Context

statuses: TypeAlias = Union[
    Literal["online"], Literal["offline"], Literal["dnd"], Literal["idle"]
]


class UserDropdown(discord.ui.Select):
    def __init__(
        self,
        ctx: Context,
        user: Union[discord.User, discord.Member],
        index_embed: discord.Embed,
        fetched_user: Optional[discord.User] = None,
    ):
        self.ctx = ctx
        self.user = user
        self.fetched_user = fetched_user
        self.index_embed = index_embed

        self.index_cache: Optional[discord.Embed] = None
        self.avatar_cache: Optional[discord.Embed] = None
        self.banner_cache: Optional[discord.Embed] = None
        self.bot_cache: Optional[discord.Embed] = None
        self.status_cache: Optional[discord.Embed] = None

        options = [
            discord.SelectOption(
                label="Index",
                description=f"Goes back to home page",
                emoji=discord.PartialEmoji(name="\U0001f3e0"),
                value="index",
            ),
            discord.SelectOption(
                label="Avatar",
                description=f"View {user}'s avatar",
                emoji=discord.PartialEmoji(name="\U0001f3a8"),
                value="avatar",
            ),
        ]

        if fetched_user and fetched_user.banner:
            options.append(
                discord.SelectOption(
                    label="Banner",
                    description=f"View {user}'s banner",
                    emoji=discord.PartialEmoji(name="\U0001f3f3"),
                    value="banner",
                )
            )

        if isinstance(user, discord.Member):
            options.append(
                discord.SelectOption(
                    label="Statuses",
                    description=f"View {user}'s statuses",
                    emoji=discord.PartialEmoji(name="\U000023f3"),
                    value="status",
                )
            )

        if user.bot:
            options.append(
                discord.SelectOption(
                    label="Bot",
                    description=f"View {user}'s bot info",
                    emoji=discord.PartialEmoji(name="\U0001f916"),
                    value="bot",
                )
            )

        super().__init__(placeholder="Make a selection", options=options)

    async def index_response(self):
        return self.index_embed

    async def avatar_response(self) -> discord.Embed:
        if self.avatar_cache:
            return self.avatar_cache

        ctx = self.ctx
        user = self.user
        fuser = self.fetched_user or await ctx.bot.fetch_user(user.id)
        color = fuser.accent_color or ctx.bot.embedcolor

        avatars = [f"[Default]({user.default_avatar.url})"]

        if user.avatar:
            avatars.append(f"[Avatar]({user.avatar.url})")

        if isinstance(user, discord.Member) and user.guild_avatar:
            avatars.append(f"[Guild]({user.guild_avatar.url})")

        embed = discord.Embed(
            color=color, description=human_join([a for a in avatars], final="and")
        )
        embed.set_author(name=f"{user}'s avatars", icon_url=user.display_avatar.url)

        embed.set_footer(text=f"Run {ctx.get_prefix}avatar for more details.")

        embed.set_image(url=user.display_avatar.url)

        self.avatar_cache = embed
        return embed

    async def banner_response(self) -> discord.Embed:
        if self.banner_cache:
            return self.banner_cache

        ctx = self.ctx
        user = self.user
        fuser = self.fetched_user or await ctx.bot.fetch_user(user.id)
        color = fuser.accent_color or ctx.bot.embedcolor

        embed = discord.Embed(color=color)
        embed.set_author(name=f"{user}'s banner", icon_url=user.display_avatar.url)
        assert fuser.banner

        embed.set_image(url=fuser.banner.url)

        self.banner_cache = embed
        return embed

    async def bot_response(self) -> discord.Embed:
        if self.bot_cache:
            return self.bot_cache

        ctx = self.ctx
        user = self.user
        fuser = self.fetched_user or await ctx.bot.fetch_user(user.id)
        color = fuser.accent_color or ctx.bot.embedcolor

        url = f"https://discord.com/api/v10/oauth2/applications/{user.id}/rpc"

        async with ctx.session.get(url) as r:
            if r.status != 200:
                raise commands.BadArgument(f"Unable to fetch info on {user}")

            data: Dict[Any, Any] = await r.json()

        embed = discord.Embed(color=color)
        embed.set_author(name=f"{user}'s bot info", icon_url=user.display_avatar.url)

        embed.description = data.get("description")

        public = data.get("bot_public") or False
        requires_code = data.get("bot_require_code_grant") or False
        guild_id: Optional[str] = data.get("guild_id")

        info_text = (
            f"Public: {['no', 'yes'][public]}\n"
            f"Requires code: {['no', 'yes'][requires_code]}\n"
        )

        info_text += f"Guild ID: `{guild_id}`" if guild_id else ""

        embed.add_field(name="Bot info", value=info_text)

        tags: Optional[List[str]] = data.get("tags")

        if tags:
            embed.add_field(name="Tags", value="\n".join([f"`{t}`" for t in tags]))

        admin_perms = discord.Permissions.none()
        admin_perms.administrator = True
        invite_perms = [
            ("Administrator", admin_perms),
            ("Advanced", discord.Permissions.advanced()),
            ("General", discord.Permissions.general()),
            ("None", discord.Permissions.none()),
            ("All", discord.Permissions.all()),
        ]

        embed.add_field(
            name="Invites",
            value="\n".join(
                f"[`{name}`]({discord.utils.oauth_url(user.id, permissions=perms)})"
                for name, perms in invite_perms
            ),
        )

        self.bot_cache = embed
        return embed

    async def status_response(self):
        if self.status_cache:
            return self.status_cache

        ctx = self.ctx
        user = self.user
        fuser = self.fetched_user or await ctx.bot.fetch_user(user.id)
        color = fuser.accent_color or ctx.bot.embedcolor

        embed = discord.Embed(color=color)
        embed.set_author(name=f"{user}'s status info", icon_url=user.display_avatar.url)

        if isinstance(user, discord.Member):
            embed.add_field(
                name="Devices",
                value=f"**Desktop**: `{user.desktop_status.value}`\n"
                f"**Website**: `{user.web_status.value}`\n"
                f"**Mobile**: `{user.mobile_status.value}`",
            )
            if ctx.bot.discord:
                online = await self.get_status(user, "online")
                dnd = await self.get_status(user, "dnd")
                idle = await self.get_status(user, "idle")
                offline = await self.get_status(user, "offline")

                statuses = [s for s in [online, dnd, idle, offline] if s]

                text = ""

                for status in statuses:
                    text += f"**{status[1].capitalize()}**: {discord.utils.format_dt(status[0], 'R')}\n"

                if bool(text):
                    embed.add_field(name="Last status", value=text)

        self.status_cache = embed
        return embed

    async def get_status(
        self, member: discord.Member, status: statuses
    ) -> Optional[Tuple[datetime.datetime, str]]:
        if not self.ctx.bot.discord:
            raise commands.BadArgument("Discord cog not found.")

        try:
            return await self.ctx.bot.discord.last_status(member, status=status)
        except commands.BadArgument:
            return None

    async def callback(self, interaction: Interaction):
        value = self.values[0]

        options = {
            "index": self.index_response,
            "avatar": self.avatar_response,
            "banner": self.banner_response,
            "bot": self.bot_response,
            "status": self.status_response,
        }

        embed = await options[value]()

        if not interaction.message:
            raise commands.BadArgument("Interaction message is gone somehow.")

        await interaction.message.edit(embed=embed)
        await interaction.response.defer()


class UserViewButtons(AuthorView):
    def __init__(self, ctx: Context, *, timeout: float | None = None):
        super().__init__(ctx, timeout=timeout)


class UserView(AuthorView):
    def __init__(
        self,
        ctx: Context,
        user: Union[discord.User, discord.Member],
        index_embed: discord.Embed,
        fetched_user: Optional[discord.User] = None,
    ):
        super().__init__(ctx)
        self.add_item(UserDropdown(ctx, user, index_embed, fetched_user))


class Info(Cog):
    async def has_nitro(
        self, member: discord.Member, fetched_user: Optional[discord.User] = None
    ) -> bool:
        fetched_user = fetched_user or await self.bot.fetch_user(member.id)
        custom_activity: discord.CustomActivity | None = discord.utils.find(lambda a: isinstance(a, discord.CustomActivity), member.activities)  # type: ignore
        return any(
            [
                member.display_avatar.is_animated(),
                fetched_user.banner,
                custom_activity
                and custom_activity.emoji
                and custom_activity.emoji.is_custom_emoji(),
            ]
        )

    async def get_badges(
        self,
        member: Union[discord.Member, discord.User],
        ctx: Context,
        fetched_user: Optional[discord.User] = None,
    ) -> List[str]:
        public_flags: Dict[Any, Any] = dict(member.public_flags)
        new_values = {
            member.id: True,
            "owner": await ctx.bot.is_owner(member),
            "server_owner": isinstance(member, discord.Member)
            and member.guild.owner == member,
            "booster": isinstance(member, discord.Member) and member.premium_since,
            "nitro": await self.has_nitro(member, fetched_user)
            if isinstance(member, discord.Member)
            else False,
        }
        public_flags.update(new_values)

        user_flags: List[str] = []

        for flag, text in USER_FLAGS.items():
            try:
                if public_flags[flag]:
                    user_flags.append(text)
            except (KeyError, IndexError):
                continue

        return user_flags

    def join_pos(self, member: discord.Member) -> int:
        members = sorted(
            member.guild.members, key=lambda m: m.joined_at or discord.utils.utcnow()
        )
        return members.index(member) + 1

    async def last_status(
        self,
        member: discord.Member,
        status: Optional[statuses] = None,
    ) -> Tuple[datetime.datetime, str]:
        sql = """SELECT created_at, status_name FROM status_logs WHERE user_id = $1 AND guild_id = $2"""
        args = (member.id, member.guild.id)

        if status:
            sql += " AND status_name = $3"
            args = (member.id, member.guild.id, status)

        sql += " ORDER BY created_at DESC"

        results = await self.bot.pool.fetchrow(sql, *args)

        if not bool(results):
            if status:
                raise commands.BadArgument(
                    "Could not find a specific status for that member."
                )

            now = discord.utils.utcnow()
            sql = """
            INSERT INTO status_logs (   user_id, status_name,
                                        guild_id, created_at)
            VALUES ($1, $2, $3, $4, $5)
            """
            await self.bot.pool.execute(
                sql, member.id, member.status.name, member.guild.id, now
            )
            return now, member.status.name

        return results["created_at"], results["status_name"]

    def format_status(self, status: str) -> str:
        return f"{['','on '][status == 'dnd']}{status}"

    async def user_info(self, ctx: Context, user: Union[discord.Member, discord.User]):
        fuser = await self.bot.fetch_user(user.id)

        badges = await self.get_badges(user, ctx, fuser)

        embed = discord.Embed(
            color=fuser.accent_colour or self.bot.embedcolor,
            description=user.mention,
            timestamp=user.created_at,
        )
        embed.set_author(name=str(user), icon_url=user.display_avatar.url)
        bar = "\u2800" * 47
        embed.set_footer(text=f"{bar} \nID: {user.id} \nCreated at")

        embed.add_field(name="Badges", value="\n".join(badges))

        if isinstance(user, discord.Member):
            joined = user.joined_at or discord.utils.utcnow()
            pos_text = (
                f"Position #{self.join_pos(user)}\n"
                f"{discord.utils.format_dt(joined, 'D')}\n"
                f"{reply} {discord.utils.format_dt(joined, 'R')}"
            )
            embed.add_field(name="Joined", value=pos_text)

            status, status_name = await self.last_status(user)

            status_text = (
                f"{discord.utils.format_dt(status, 'D')}\n"
                f"{reply} {discord.utils.format_dt(status, 'R')}"
            )

            embed.add_field(
                name=f"{self.format_status(status_name)} since".capitalize(),
                value=status_text,
            )

        await ctx.send(embed=embed, view=UserView(ctx, user, embed, fuser))

    @commands.command(name="userinfo", aliases=("ui", "user"))
    async def userinfo(
        self,
        ctx: Context,
        *,
        user: Union[discord.Member, discord.User] = commands.Author,
    ):
        await self.user_info(ctx, user)
