from __future__ import annotations

import datetime
import random
import re
from collections import Counter
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Literal,
    Optional,
    TypeAlias,
    Union,
    cast,
)

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands
from discord.interactions import Interaction

from core import Cog
from utils import (
    USER_FLAGS,
    AllChannels,
    AuthorView,
    LayoutPageModal,
    LayoutPager,
    Review,
    ReviewPageSource,
    ReviewSender,
    build_layout_pagination_row,
    fish_download,
    fish_edit,
    fish_go_back,
    human_join,
)

if TYPE_CHECKING:
    from extensions.context import Context, GuildContext

statuses: TypeAlias = Union[
    Literal["online"], Literal["offline"], Literal["dnd"], Literal["idle"]
]


AVATAR_GRID_RE = re.compile(
    r"^(?P<width>\d{1,2})\s*x\s*(?P<height>\d{1,2})$", re.IGNORECASE
)


BURPLE = discord.ButtonStyle.blurple
GREEN = discord.ButtonStyle.green

_PERM_LABELS = {
    "create_instant_invite": "Create Invite",
    "kick_members": "Kick Members",
    "ban_members": "Ban Members",
    "administrator": "Administrator",
    "manage_channels": "Manage Channels",
    "manage_guild": "Manage Server",
    "add_reactions": "Add Reactions",
    "view_audit_log": "View Audit Log",
    "priority_speaker": "Priority Speaker",
    "stream": "Stream",
    "read_messages": "Read Messages",
    "send_messages": "Send Messages",
    "send_tts_messages": "Send TTS",
    "manage_messages": "Manage Messages",
    "embed_links": "Embed Links",
    "attach_files": "Attach Files",
    "read_message_history": "Read History",
    "mention_everyone": "Mention Everyone",
    "external_emojis": "External Emojis",
    "connect": "Connect",
    "speak": "Speak",
    "mute_members": "Mute Members",
    "deafen_members": "Deafen Members",
    "move_members": "Move Members",
    "use_voice_activation": "Use VAD",
    "change_nickname": "Change Nickname",
    "manage_nicknames": "Manage Nicknames",
    "manage_roles": "Manage Roles",
    "manage_webhooks": "Manage Webhooks",
    "manage_expressions": "Manage Expressions",
    "use_application_commands": "Use Slash Commands",
    "request_to_speak": "Request to Speak",
    "manage_events": "Manage Events",
    "manage_threads": "Manage Threads",
    "create_public_threads": "Create Public Threads",
    "create_private_threads": "Create Private Threads",
    "external_stickers": "External Stickers",
    "send_messages_in_threads": "Send in Threads",
    "use_embedded_activities": "Use Activities",
    "moderate_members": "Moderate Members",
    "bypass_slowmode": "Bypass Slowmode",
    "pin_messages": "Pin Messages",
    "create_expressions": "Create Expressions",
}


_PERM_CATEGORIES = {
    "Moderation": (
        "kick_members",
        "ban_members",
        "moderate_members",
        "manage_messages",
        "manage_roles",
        "manage_channels",
        "manage_guild",
        "manage_nicknames",
        "manage_webhooks",
        "manage_threads",
        "manage_events",
        "manage_expressions",
        "view_audit_log",
        "mention_everyone",
        "administrator",
    ),
    "Media": (
        "embed_links",
        "attach_files",
        "add_reactions",
        "external_emojis",
        "external_stickers",
        "send_messages_in_threads",
        "create_expressions",
        "use_embedded_activities",
        "stream",
    ),
    "General": (
        "send_messages",
        "read_messages",
        "read_message_history",
        "create_instant_invite",
        "change_nickname",
        "connect",
        "speak",
        "use_voice_activation",
        "priority_speaker",
        "request_to_speak",
        "mute_members",
        "deafen_members",
        "move_members",
        "use_application_commands",
        "bypass_slowmode",
        "pin_messages",
        "create_public_threads",
        "create_private_threads",
        "send_tts_messages",
    ),
}


class RoleDropdown(discord.ui.Select):
    def __init__(self, ctx: Context, role: discord.Role, index_embed: discord.Embed):
        self.ctx = ctx
        self.role = role
        self.index_embed = index_embed

        perms = list(
            _PERM_LABELS.get(perm, perm) for perm, val in role.permissions if val
        )

        options = [
            discord.SelectOption(
                label="Home",
                description="Back to role overview",
                emoji="\U0001f3e0",
                value="index",
            ),
            discord.SelectOption(
                label="Permissions",
                description=f"{len(perms)} permissions",
                emoji="\U0001f512",
                value="permissions",
            ),
            discord.SelectOption(
                label="Members",
                description=f"{len(role.members)} members",
                emoji="\U0001f465",
                value="members",
            ),
        ]
        super().__init__(placeholder="Select a category", options=options)

    async def callback(self, interaction: discord.Interaction):
        try:
            base_view = discord.ui.View(timeout=120)
            base_view.add_item(RoleDropdown(self.ctx, self.role, self.index_embed))

            if self.values[0] == "index":
                await interaction.response.edit_message(
                    embed=self.index_embed, view=base_view
                )
            elif self.values[0] == "permissions":
                embed = discord.Embed(
                    color=self.role.color or self.ctx.bot.embedcolor,
                    title=f"Permissions for {self.role.name}",
                )
                for cat, perm_names in _PERM_CATEGORIES.items():
                    perms = list(
                        _PERM_LABELS.get(p, p)
                        for p in perm_names
                        if getattr(self.role.permissions, p, False)
                    )
                    if perms:
                        embed.add_field(
                            name=cat,
                            value=human_join([f"`{p}`" for p in perms], final="and"),
                            inline=False,
                        )
                if not embed.fields:
                    embed.description = "No permissions."
                await interaction.response.edit_message(embed=embed, view=base_view)
            elif self.values[0] == "members":
                members = self.role.members
                bots = [m for m in members if m.bot]
                humans = [m for m in members if not m.bot]

                embed = discord.Embed(
                    color=self.role.color or self.ctx.bot.embedcolor,
                    title=f"Members with {self.role.name}",
                )
                embed.add_field(name="Total", value=str(len(members)), inline=True)
                embed.add_field(name="Humans", value=str(len(humans)), inline=True)
                embed.add_field(name="Bots", value=str(len(bots)), inline=True)

                if (
                    cast(Any, self.ctx.author).guild_permissions.manage_roles
                    and cast(Any, self.ctx.guild).me.guild_permissions.manage_roles
                ):
                    for btn in RoleMemberButtons(self.ctx, self.role).children:
                        base_view.add_item(btn)
                await interaction.response.edit_message(embed=embed, view=base_view)
        except Exception as e:
            await interaction.response.send_message(
                f"Something went wrong: {e}", ephemeral=True
            )


class RoleMemberButtons(discord.ui.View):
    def __init__(self, ctx: Context, role: discord.Role):
        super().__init__(timeout=120)
        self.ctx = ctx
        self.role = role

    @discord.ui.button(label="Add Member", style=discord.ButtonStyle.green)
    async def add_member(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not cast(Any, interaction.user).guild_permissions.manage_roles:
            return await interaction.response.send_message(
                "You need Manage Roles permission.", ephemeral=True
            )
        modal = _RoleMemberModal(self.ctx, self.role, "add")
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Remove Member", style=discord.ButtonStyle.red)
    async def remove_member(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not cast(Any, interaction.user).guild_permissions.manage_roles:
            return await interaction.response.send_message(
                "You need Manage Roles permission.", ephemeral=True
            )
        modal = _RoleMemberModal(self.ctx, self.role, "remove")
        await interaction.response.send_modal(modal)


class _RoleMemberModal(discord.ui.Modal, title="Member ID"):
    member_id = discord.ui.TextInput(
        label="Enter the member's ID", placeholder="Discord user ID…"
    )

    def __init__(self, ctx: Context, role: discord.Role, action: str):
        super().__init__()
        self.ctx = ctx
        self.role = role
        self.action = action

    async def on_submit(self, interaction: discord.Interaction):
        try:
            member = await cast(Any, interaction.guild).fetch_member(
                int(self.member_id.value.strip())
            )
        except (ValueError, discord.HTTPException):
            await interaction.response.send_message(
                "Invalid member ID.", ephemeral=True
            )
            return

        if self.action == "add":
            await member.add_roles(self.role, reason=f"Added by {interaction.user}")
            await interaction.response.send_message(
                f"✅ Added {self.role.name} to {member.mention}.", ephemeral=True
            )
        else:
            await member.remove_roles(
                self.role, reason=f"Removed by {interaction.user}"
            )
            await interaction.response.send_message(
                f"✅ Removed {self.role.name} from {member.mention}.", ephemeral=True
            )


async def _fetch_review_entries(
    ctx: Context, user: Union[discord.User, discord.Member]
) -> List[Review]:
    url = f"https://manti.vendicated.dev/api/reviewdb/users/{user.id}/reviews"
    async with ctx.session.get(url) as resp:
        if resp.status != 200:
            return []
        payload = await resp.json()

    reviews = payload.get("reviews", []) if isinstance(payload, dict) else []
    if not isinstance(reviews, list) or not reviews:
        return []

    # ReviewDB normally prefixes the list with a summary record. Some users
    # receive only review records, so only discard that prefix when it is not
    # shaped like a review.
    first = reviews[0]
    rows = (
        reviews[1:] if not isinstance(first, dict) or "sender" not in first else reviews
    )

    entries: List[Review] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sender = row.get("sender")
        if not isinstance(sender, dict):
            continue
        try:
            review_id = int(row["id"])
            if review_id == 0:
                continue
            entries.append(
                Review(
                    id=review_id,
                    sender=ReviewSender(
                        user_id=int(sender["discordID"]),
                        profilePhoto=str(sender.get("profilePhoto") or ""),
                        username=str(sender.get("username") or "Unknown user"),
                    ),
                    comment=str(row.get("comment") or ""),
                    timestamp=int(row["timestamp"]),
                    target_id=user.id,
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return entries


class UserDropdown(discord.ui.Select):
    def __init__(self, user_view: "UserView"):
        self.user_view = user_view
        user = user_view.user

        options = [
            discord.SelectOption(
                label="Index",
                description="Goes back to home page",
                emoji=discord.PartialEmoji(name="\U0001f3e0"),
                value="index",
            ),
            discord.SelectOption(
                label="Avatar",
                description=f"View {user}'s avatar",
                emoji=discord.PartialEmoji(name="\U0001f3a8"),
                value="avatar",
            ),
            discord.SelectOption(
                label="Reviews",
                description=f"View {user}'s reviews",
                emoji=discord.PartialEmoji(name="\U0001f4d4"),
                value="reviews",
            ),
            discord.SelectOption(
                label="Statuses",
                description=f"View {user}'s last seen statuses",
                emoji=discord.PartialEmoji(name="\U0001f7e2"),
                value="statuses",
            ),
        ]

        if user_view.fetched_user.banner:
            options.append(
                discord.SelectOption(
                    label="Banner",
                    description=f"View {user}'s banner",
                    emoji=discord.PartialEmoji(name="\U0001f3f3"),
                    value="banner",
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

    async def callback(self, interaction: Interaction) -> None:
        await self.user_view.select_page(interaction, self.values[0])


class UserView(discord.ui.LayoutView):
    def __init__(
        self,
        ctx: Context,
        user: Union[discord.User, discord.Member],
        fetched_user: discord.User,
        index_body: str,
        footer_text: str,
        server_tag: str | None = None,
        avatar_media_url: str = "",
        accent_color: discord.Colour | None = None,
    ) -> None:
        super().__init__(timeout=120)
        self.ctx = ctx
        self.user = user
        self.fetched_user = fetched_user
        self.index_body = index_body
        self.footer_text = footer_text
        self.server_tag = server_tag
        self.avatar_media_url = avatar_media_url or user.display_avatar.url
        self.accent_color = accent_color or ctx.bot.embedcolor
        self._guild_id = (
            user.guild.id
            if isinstance(user, discord.Member)
            else ctx.guild.id if ctx.guild else None
        )
        self._page = "index"
        self._reviews: List[Review] = []
        self._review_index = 0
        self._review_source: ReviewPageSource | None = None
        self._review_navigation_buttons: tuple[discord.ui.Button, ...] = ()
        self._bot_data: Dict[Any, Any] | None = None
        self.message: Optional[discord.Message] = None
        self._render()

    def _footer(self, extra: str | None = None) -> list[discord.ui.TextDisplay]:
        lines = self.footer_text.splitlines()
        if extra:
            lines.extend(extra.splitlines())

        regular_lines = [line for line in lines if not line.startswith("-# ")]
        subtext_lines = [line for line in lines if line.startswith("-# ")]
        displays: list[discord.ui.TextDisplay] = []
        if regular_lines:
            displays.append(discord.ui.TextDisplay("\n".join(regular_lines)))
        if subtext_lines:
            displays.append(discord.ui.TextDisplay("\n".join(subtext_lines)))
        return displays

    def _profile_section(self, title: str, body: str) -> discord.ui.Section:
        return discord.ui.Section(
            discord.ui.TextDisplay(f"## {title}"),
            discord.ui.TextDisplay(body or "No additional information."),
            accessory=discord.ui.Thumbnail(self.avatar_media_url),
        )

    def _render(self) -> None:
        self.clear_items()
        children: list[discord.ui.Item[Any]]

        if self._page == "index":
            title = str(self.user)
            if self.server_tag:
                title += f" ({self.server_tag})"
            children = [
                self._profile_section(title, f"{self.user.mention}\n{self.index_body}")
            ]
            children.extend([discord.ui.Separator(), *self._footer()])
        elif self._page == "avatar":
            avatars = [f"[Default]({self.user.default_avatar.url})"]
            if self.user.avatar:
                avatars.append(f"[Avatar]({self.user.avatar.url})")
            if isinstance(self.user, discord.Member) and self.user.guild_avatar:
                avatars.append(f"[Guild]({self.user.guild_avatar.url})")
            children = [
                self._profile_section(
                    f"{self.user}'s avatars",
                    human_join(avatars, final="and")
                    + f"\n\nRun {self.ctx.get_prefix}avatar for more details.",
                ),
                discord.ui.Separator(),
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(self.avatar_media_url)
                ),
            ]
        elif self._page == "banner":
            banner = self.fetched_user.banner
            children = (
                [
                    discord.ui.TextDisplay(f"## {self.user}'s banner"),
                    discord.ui.MediaGallery(discord.MediaGalleryItem(banner.url)),
                ]
                if banner
                else [discord.ui.TextDisplay("This user has no banner.")]
            )
        elif self._page == "statuses":
            children = [
                discord.ui.TextDisplay(f"## {self.user}'s statuses"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(self._status_text),
            ]
        elif self._page == "bot":
            children = self._bot_children()
        elif self._page == "reviews":
            children = self._review_children()
        else:
            children = [discord.ui.TextDisplay("No information available.")]

        container = discord.ui.Container(*children, accent_color=self.accent_color)
        self.add_item(container)
        self.dropdown = UserDropdown(self)
        self.add_item(discord.ui.ActionRow(self.dropdown))

        self._review_navigation_buttons = ()
        if self._page == "reviews" and self._reviews:
            row, buttons = build_layout_pagination_row(
                page=self._review_index,
                page_count=len(self._reviews),
                previous=self._previous_review,
                next_page=self._next_review,
                shuffle=self._shuffle_review,
                go_to_page=self._open_review_page_modal,
                trash=self._delete_review_view,
            )
            self._review_navigation_buttons = buttons
            self.add_item(row)

    def _review_children(self) -> list[discord.ui.Item[Any]]:
        source = self._review_source or ReviewPageSource(
            self._reviews,
            user_label=str(self.user),
        )
        return list(source.format_page(self._review_index))

    def _bot_children(self) -> list[discord.ui.Item[Any]]:
        data = self._bot_data or {}
        public = "yes" if data.get("bot_public") else "no"
        requires_code = "yes" if data.get("bot_require_code_grant") else "no"
        info = [
            f"**Public:** {public}",
            f"**Requires code:** {requires_code}",
        ]
        if data.get("guild_id"):
            info.append(f"**Guild ID:** `{data['guild_id']}`")
        tags = data.get("tags")
        if tags:
            info.append(f"**Tags:** {' '.join(f'`{tag}`' for tag in tags)}")
        invite_perms = [
            ("Administrator", discord.Permissions(administrator=True)),
            ("Advanced", discord.Permissions.advanced()),
            ("General", discord.Permissions.general()),
            ("None", discord.Permissions.none()),
            ("All", discord.Permissions.all()),
        ]
        invites = "\n".join(
            f"[`{name}`]({discord.utils.oauth_url(self.user.id, permissions=perms)})"
            for name, perms in invite_perms
        )
        return [
            self._profile_section(
                f"{self.user}'s bot info",
                str(data.get("description") or "No description.")
                + "\n\n"
                + "\n".join(info)
                + f"\n\n**Invites:**\n{invites}",
            )
        ]

    @property
    def _status_text(self) -> str:
        return getattr(self, "_statuses", "No status data recorded yet.")

    async def select_page(self, interaction: Interaction, value: str) -> None:
        await interaction.response.defer()
        try:
            if value == "reviews":
                self._reviews = await _fetch_review_entries(self.ctx, self.user)
                self._review_index = 0
                self._review_source = ReviewPageSource(
                    self._reviews,
                    user_label=str(self.user),
                )
            elif value == "statuses":
                rows: list[asyncpg.Record] = []
                if self._guild_id:
                    rows = await self.ctx.pool.fetch(
                        "SELECT status, last_seen FROM user_statuses WHERE user_id = $1 AND guild_id = $2 ORDER BY last_seen DESC",
                        self.user.id,
                        self._guild_id,
                    )
                if not rows:
                    rows = await self.ctx.pool.fetch(
                        "SELECT status, last_seen FROM user_statuses WHERE user_id = $1 ORDER BY last_seen DESC",
                        self.user.id,
                    )
                self._statuses = (
                    "\n".join(
                        f"**{row['status'].title()}** • {discord.utils.format_dt(row['last_seen'], 'R')}"
                        for row in rows
                    )
                    if rows
                    else "No status data recorded yet."
                )
            elif value == "bot":
                url = f"https://discord.com/api/v10/oauth2/applications/{self.user.id}/rpc"
                async with self.ctx.session.get(url) as response:
                    if response.status != 200:
                        raise commands.BadArgument(
                            f"Unable to fetch info on {self.user}"
                        )
                    self._bot_data = await response.json()
            self._page = value
            self._render()
            await interaction.edit_original_response(
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as error:
            self.ctx.bot.logger.error(
                "Could not render userinfo dropdown",
                exc_info=(type(error), error, error.__traceback__),
            )
            await interaction.followup.send(
                "Could not load that section right now.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _previous_review(self, interaction: Interaction) -> None:
        if self._reviews:
            await self._set_layout_page(
                interaction, (self._review_index - 1) % len(self._reviews)
            )

    async def _next_review(self, interaction: Interaction) -> None:
        if self._reviews:
            await self._set_layout_page(
                interaction, (self._review_index + 1) % len(self._reviews)
            )

    async def _shuffle_review(self, interaction: Interaction) -> None:
        if len(self._reviews) <= 1:
            await interaction.response.send_message(
                "There are no other reviews to choose from.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        choices = [
            index for index in range(len(self._reviews)) if index != self._review_index
        ]
        self._review_index = random.choice(choices)
        self._render()
        await interaction.response.edit_message(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _open_review_page_modal(self, interaction: Interaction) -> None:
        if not self._reviews:
            await interaction.response.send_message(
                "This user has no reviews.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.send_modal(LayoutPageModal(self, len(self._reviews)))

    async def _set_layout_page(self, interaction: Interaction, page: int) -> None:
        if page < 0 or page >= len(self._reviews):
            await interaction.response.send_message(
                "That page does not exist.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        self._review_index = page
        self._render()
        if interaction.response.is_done():
            if self.message:
                await self.message.edit(
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await interaction.edit_original_response(
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        else:
            await interaction.response.edit_message(
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _delete_review_view(self, interaction: Interaction) -> None:
        await interaction.response.defer()
        if self.message:
            await self.message.delete()
        else:
            await interaction.delete_original_response()
        self.stop()

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            "Only the person who ran this command can use these controls.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    async def on_timeout(self) -> None:
        self.dropdown.disabled = True
        for item in self.children:
            if isinstance(item, discord.ui.ActionRow):
                for child in item.children:
                    if isinstance(child, discord.ui.Button):
                        child.disabled = True
        if self.message:
            await self.message.edit(
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def on_error(
        self,
        interaction: Interaction,
        error: Exception,
        item: discord.ui.Item[Any],
    ) -> None:
        self.ctx.bot.logger.error(
            "Userinfo view failed for %s on %s",
            self.ctx.author,
            type(item).__name__,
            exc_info=(type(error), error, error.__traceback__),
        )
        try:
            await self.ctx.bot.log_error(
                error,
                context=self.ctx,
                interaction=interaction,
            )
        except Exception:
            self.ctx.bot.logger.exception("Could not send userinfo view error report")
        try:
            message = f"Could not update the review page: {error}"
            if interaction.response.is_done():
                await interaction.followup.send(
                    message,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await interaction.response.send_message(
                    message,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except discord.DiscordException:
            self.ctx.bot.logger.exception("Could not send userinfo view error")


class QualityDropdown(discord.ui.Select):
    view: EditDropdownView

    def __init__(self):
        sizes = ["16", "32", "64", "128", "256", "512", "1024", "2048", "4096"]
        options = [
            discord.SelectOption(label=f"{size}px", value=size) for size in sizes
        ]

        super().__init__(placeholder="Select a quality", options=options)

    async def callback(self, interaction: discord.Interaction):
        self.view.asset = self.view.asset.with_size(int(self.values[0]))
        self.view.embed.set_image(url=self.view.asset.url)

        await interaction.response.edit_message(embed=self.view.embed)


class FormatDropdown(discord.ui.Select):
    view: EditDropdownView

    def __init__(
        self,
        asset: discord.Asset,
    ):
        formats = ["webp", "jpeg", "jpg", "png"]

        if asset.is_animated():
            formats.append("gif")

        options = [
            discord.SelectOption(label=_format, value=_format) for _format in formats
        ]

        super().__init__(placeholder="Select a format", options=options)

    async def callback(self, interaction: discord.Interaction):
        self.view.asset = self.view.asset.with_format(self.values[0])  # type: ignore
        self.view.embed.set_image(url=self.view.asset.url)

        await interaction.response.edit_message(embed=self.view.embed)


class EditDropdownView(AuthorView):
    asset: discord.Asset

    def __init__(
        self,
        ctx: Context,
        user: Union[discord.User, discord.Member],
        embed: discord.Embed,
        asset: discord.Asset,
        fetched_user: Optional[discord.User] = None,
        option: Union[Literal["quality"], Literal["format"]] = "quality",
    ):
        super().__init__(ctx)
        self.user = user
        self.fetched_user = fetched_user
        self.embed = embed
        self.asset = asset

        (
            self.add_item(QualityDropdown())
            if option == "quality"
            else self.add_item(FormatDropdown(asset))
        )

    @discord.ui.button(emoji=fish_go_back, row=1, style=BURPLE)
    async def go_back(self, interaction: discord.Interaction, _):
        await interaction.response.edit_message(
            embed=self.embed,
            view=EditView(
                self.ctx, self.user, self.embed, self.asset, self.fetched_user
            ),
        )


class EditView(AuthorView):
    asset: discord.Asset

    def __init__(
        self,
        ctx: Context,
        user: Union[discord.User, discord.Member],
        embed: discord.Embed,
        asset: discord.Asset,
        fetched_user: Optional[discord.User] = None,
    ):
        super().__init__(ctx)
        self.user = user
        self.fetched_user = fetched_user
        self.embed = embed
        self.asset = asset

        self.server.disabled = isinstance(user, discord.User) or not hasattr(
            user, "guild_avatar"
        )

        self.avatar.disabled = not user.avatar or user.avatar.key == asset.key

    async def edit_message(self, interaction: discord.Interaction):
        self.embed.set_image(url=self.asset.url)
        await interaction.response.edit_message(embed=self.embed)

    @discord.ui.button(label="Default Avatar", row=0, style=BURPLE)
    async def avatar(self, interaction: discord.Interaction, _):
        if not self.user.avatar:
            self.avatar.disabled = True
            raise commands.BadArgument("User's avatar does not exist")

        self.asset = self.user.avatar

        await self.edit_message(interaction)

    @discord.ui.button(label="Server Avatar", row=0, style=BURPLE)
    async def server(self, interaction: discord.Interaction, _):
        if not isinstance(self.user, discord.Member) or self.user.guild_avatar is None:
            self.server.disabled = True
            raise commands.BadArgument("User's server avatar does not exist")

        self.asset = self.user.guild_avatar

        await self.edit_message(interaction)

    @discord.ui.button(emoji=fish_go_back, row=0, style=BURPLE)
    async def go_back(self, interaction: discord.Interaction, _):
        await interaction.response.edit_message(
            embed=self.embed,
            view=AvatarView(
                self.ctx, self.user, self.embed, self.fetched_user, self.asset
            ),
        )

    @discord.ui.button(label="Avatar Quality", row=1, style=GREEN)
    async def quality(self, interaction: discord.Interaction, _):
        await interaction.response.edit_message(
            embed=self.embed,
            view=EditDropdownView(
                self.ctx,
                self.user,
                self.embed,
                self.asset,
                self.fetched_user,
                "quality",
            ),
        )

    @discord.ui.button(label="Avatar Format", row=1, style=GREEN)
    async def _format(self, interaction: discord.Interaction, _):
        await interaction.response.edit_message(
            embed=self.embed,
            view=EditDropdownView(
                self.ctx, self.user, self.embed, self.asset, self.fetched_user, "format"
            ),
        )


class AvatarView(AuthorView):
    asset: discord.Asset

    def __init__(
        self,
        ctx: Context,
        user: Union[discord.User, discord.Member],
        embed: discord.Embed,
        fetched_user: Optional[discord.User] = None,
        asset: Optional[discord.Asset] = None,
    ):
        super().__init__(ctx)
        self.user = user
        self.fetched_user = fetched_user
        self.embed = embed
        self.asset = asset or user.display_avatar

        self.edit.disabled = self.asset.key.isdigit()

    @discord.ui.button(label="Edit", row=0, style=BURPLE, emoji=fish_edit)
    async def edit(self, interaction: discord.Interaction, _):
        if interaction.message is None:
            raise commands.BadArgument("Somehow message was none, try again.")

        view = EditView(self.ctx, self.user, self.embed, self.asset)

        await interaction.message.edit(view=view)
        await interaction.response.defer()

    @discord.ui.button(
        label="Save",
        row=0,
        style=GREEN,
        emoji=fish_download,
    )
    async def save(self, interaction: discord.Interaction, _):
        if interaction.message is None:
            raise commands.BadArgument("Somehow message was none, try again.")

        file = await self.asset.to_file()
        self.embed.set_image(url=f"attachment://{file.filename}")
        await interaction.message.edit(embed=self.embed, attachments=[file], view=None)
        await interaction.response.send_message(
            "Avatar saved! Here's how saving avatars works: the avatar is saved as a "
            "file in this message instead of using the avatar URL. This means that "
            "unless the message with the file gets deleted, the url to the file "
            "will remain forever, unlike avatars URLs, which will be deleted if "
            "the user decides to change their avatar later on.",
            ephemeral=True,
        )


class Info(Cog):
    async def has_nitro(
        self, member: discord.Member, fetched_user: Optional[discord.User] = None
    ) -> bool:
        fetched_user = fetched_user or await self.bot.fetch_user(member.id)
        custom_activity: discord.CustomActivity | None = discord.utils.find(  # type: ignore
            lambda a: isinstance(a, discord.CustomActivity), member.activities
        )
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
            "server_owner": isinstance(member, discord.Member)
            and member.guild.owner == member,
            "booster": isinstance(member, discord.Member) and member.premium_since,
            "nitro": (
                await self.has_nitro(member, fetched_user)
                if isinstance(member, discord.Member)
                else False
            ),
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

    async def user_info(self, ctx: Context, user: Union[discord.Member, discord.User]):
        fuser = await self.bot.fetch_user(user.id)

        badges = await self.get_badges(user, ctx, fuser)
        body_parts: list[str] = []
        if badges:
            body_parts.append("**Badges:**\n" + "\n".join(badges))

        primary_guild = getattr(fuser, "primary_guild", None)
        server_tag = getattr(primary_guild, "tag", None)

        footer_lines = [
            f"Created at: <t:{int(user.created_at.timestamp())}:D>",
        ]
        if isinstance(user, discord.Member):
            if user.joined_at:
                footer_lines.append(
                    f"Joined: <t:{int(user.joined_at.timestamp())}:D> "
                    f"(position #{self.join_pos(user)})"
                )

        guild_id = (
            user.guild.id
            if isinstance(user, discord.Member)
            else (ctx.guild.id if ctx.guild else None)
        )

        row = None
        if guild_id:
            row = await self.bot.pool.fetchrow(
                "SELECT status, last_seen FROM user_statuses WHERE user_id = $1 AND guild_id = $2 ORDER BY last_seen DESC LIMIT 1",
                user.id,
                guild_id,
            )

        if row is None:
            row = await self.bot.pool.fetchrow(
                "SELECT status, last_seen FROM user_statuses WHERE user_id = $1 ORDER BY last_seen DESC LIMIT 1",
                user.id,
            )

        if row:
            footer_lines.append(
                f"Last seen: {row['status'].title()} "
                f"{discord.utils.format_dt(row['last_seen'], 'R')}"
            )

        footer_lines.append(
            f"-# ID: {user.id}",
        )

        avatar_asset = user.display_avatar
        avatar_filename = (
            "userinfo-avatar.gif"
            if avatar_asset.is_animated()
            else "userinfo-avatar.png"
        )
        avatar_file = await avatar_asset.to_file(filename=avatar_filename)
        view = UserView(
            ctx,
            user,
            fuser,
            "\n\n".join(body_parts) or "No additional information.",
            "\n".join(footer_lines),
            server_tag,
            f"attachment://{avatar_file.filename}",
            fuser.accent_color or discord.Colour(self.bot.embedcolor),
        )
        view.message = await ctx.send(
            view=view,
            file=avatar_file,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def review_func(
        self, ctx: Context, user: discord.User | discord.Member, hidden: bool = False
    ):
        data = await _fetch_review_entries(ctx, user)
        source = ReviewPageSource(data, user_label=str(user))
        pager = LayoutPager(
            source,
            ctx=ctx,
            accent_color=self.bot.embedcolor,
        )
        await pager.start(ctx, e=hidden)

    @commands.hybrid_group(name="user", aliases=("userinfo", "ui"), fallback="info")
    @app_commands.describe(user="User to inspect. Defaults to yourself.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def userinfo(
        self,
        ctx: Context,
        *,
        user: Union[discord.Member, discord.User] = commands.Author,
    ):
        """
        Get information about a user
        """
        async with ctx.typing():
            await self.user_info(ctx, user)

    async def _send_user_avatar(
        self,
        ctx: Context,
        user: Union[discord.Member, discord.User] = commands.Author,
    ) -> None:
        fuser = await self.bot.fetch_user(user.id)
        embed = discord.Embed(color=fuser.accent_color or self.bot.embedcolor)
        embed.set_author(name=f"{user}'s avatar", icon_url=user.display_avatar.url)
        embed.set_image(url=user.display_avatar.url)

        sql = """SELECT created_at FROM avatars WHERE user_id = $1 ORDER BY created_at DESC"""
        last_av: Optional[datetime.datetime] = await self.bot.pool.fetchval(
            sql, user.id
        )
        if last_av:
            embed.timestamp = last_av
            embed.set_footer(text="Last avatar saved")

        await ctx.send(embed=embed, view=AvatarView(ctx, user, embed, fuser))

    def channel_embed(self, channel: AllChannels) -> discord.Embed:
        embed = discord.Embed(
            color=self.bot.embedcolor,
            timestamp=channel.created_at,
            title=channel.name,
        )

        embed.set_footer(text=f"ID: {channel.id} \nCreated at")

        if isinstance(
            channel, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel)
        ):
            embed.add_field(
                name="Members",
                value=f"{len(channel.members):,} ({sum(m.bot for m in channel.members):,} bots)",
            )

        return embed

    async def text_info(self, ctx: Context, channel: discord.TextChannel):
        embed = self.channel_embed(channel)
        embed.description = channel.topic
        await ctx.send(embed=embed)

    async def category_info(self, ctx: Context, channel: discord.CategoryChannel):
        embed = self.channel_embed(channel)

        if isinstance(ctx.author, discord.Member):
            text = f"{len(channel.channels)}"
            private = sum(
                not c.permissions_for(ctx.author).read_messages
                for c in channel.channels
            )
            if private > 0:
                text += f" ({private:,} private)"

            embed.add_field(
                name="Channels",
                value=text,
            )

        await ctx.send(embed=embed)

    async def voice_info(
        self, ctx: Context, channel: Union[discord.VoiceChannel, discord.StageChannel]
    ):
        embed = self.channel_embed(channel)

        vc_limit = (
            "No User limit"
            if not bool(channel.user_limit)
            else f"{channel.user_limit:,} User limit"
        )

        embed.add_field(
            name="Details",
            value=f"{channel.video_quality_mode.name.capitalize()} Video Quality\n"
            f"{channel.rtc_region.capitalize() if channel.rtc_region else 'Automatic'} region\n"
            f"{vc_limit}\n"
            f"{str(channel.bitrate)[:-3]}kbps",
        )

        await ctx.send(embed=embed)

    async def thread_info(self, ctx: Context, channel: discord.Thread):
        embed = self.channel_embed(channel)
        members = await channel.fetch_members()
        embed.add_field(
            name="Members",
            value=f"{len(members):,}",
        )

        if channel.parent:
            parent = channel.parent
            embed.add_field(
                name="Parent channel", value=f"{parent} (`{parent.id}`)", inline=False
            )

        if channel.owner:
            owner = channel.owner
            embed.add_field(name="Owner", value=f"{owner} (`{owner.id}`)", inline=False)

        await ctx.send(embed=embed)

    async def forum_info(self, ctx: Context, channel: discord.ForumChannel):
        embed = self.channel_embed(channel)

        embed.description = channel.topic

        embed.add_field(name="Posts", value=f"{len(channel.threads)}")

        embed.add_field(
            name="Tags",
            value=human_join(
                [f"{s.emoji or ''}{s.name}" for s in channel.available_tags],
                final="and",
            ),
        )

        await ctx.send(embed=embed)

    @commands.command(name="channelinfo", aliases=("channel", "ci"))
    async def channelinfo(
        self,
        ctx: Context,
        channel: AllChannels = commands.param(
            displayed_default="[channel=<current channel>]",
            default=lambda ctx: ctx.channel,
        ),
    ):
        """
        Get information about a channel
        """
        types = {
            discord.TextChannel: self.text_info,
            discord.CategoryChannel: self.category_info,
            discord.VoiceChannel: self.voice_info,
            discord.StageChannel: self.voice_info,
            discord.Thread: self.thread_info,
            discord.ForumChannel: self.forum_info,
        }

        await types[type(channel)](ctx, channel)

    @commands.hybrid_command(
        name="avatar",
        aliases=("pfp", "av", "avy", "avi"),
        extras={"usage": "[user] [history|grid|list] [server|guild] [WxH]"},
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        user="Discord user, mention, ID, or name.",
        mode="history/grid for a grid, or list for a paginated list.",
        server="Use server/guild to show this server's avatar history.",
        grid_size="Grid size such as 6x7, used with history/grid.",
    )
    async def avatar(
        self,
        ctx: Context,
        *,
        user: str | None = None,
        mode: str | None = None,
        server: str | None = None,
        grid_size: str | None = None,
    ):
        """Show a user's avatar, history grid, or paginated avatar list."""
        logging = self.bot.logging
        if not logging:
            raise commands.BadArgument("Could not find logging cog.")

        supplied_arguments = [
            value for value in (user, mode, server, grid_size) if value
        ]
        # Text commands expose keyword-only arguments as one remaining string.
        # Split that string so ``avatar history 6x7 @user`` remains order
        # independent. Slash commands provide each option separately, so keep
        # a user name containing spaces intact there.
        raw_arguments = (
            [part for value in supplied_arguments for part in value.split()]
            if ctx.interaction is None
            else supplied_arguments
        )
        selected_mode: Literal["grid", "list"] | None = None
        use_server = False
        selected_grid_size: tuple[int, int] | None = None
        user_arguments: list[str] = []

        for argument in raw_arguments:
            value = argument.strip()
            lowered = value.casefold()
            if lowered.startswith("-"):
                lowered = lowered.lstrip("-")

            if lowered in {"history", "grid"}:
                if selected_mode == "list":
                    raise commands.BadArgument(
                        "Choose either history/grid or list, not both."
                    )
                selected_mode = "grid"
                continue
            if lowered == "list":
                if selected_mode == "grid":
                    raise commands.BadArgument(
                        "Choose either history/grid or list, not both."
                    )
                selected_mode = "list"
                continue
            if lowered in {"server", "guild"}:
                use_server = True
                continue

            match = AVATAR_GRID_RE.fullmatch(value)
            if match:
                if selected_grid_size is not None:
                    raise commands.BadArgument("Only one avatar grid size is allowed.")
                width = int(match.group("width"))
                height = int(match.group("height"))
                if not 1 <= width <= 10 or not 1 <= height <= 10:
                    raise commands.BadArgument(
                        "Avatar grid sizes must be between 1x1 and 10x10."
                    )
                selected_grid_size = (width, height)
                continue

            user_arguments.append(value)

        if (getattr(ctx, "invoked_with", "") or "").casefold() == "guilds":
            use_server = True

        if user_arguments:
            raw_user = " ".join(user_arguments).strip()
            try:
                target_user = await commands.UserConverter().convert(ctx, raw_user)
            except commands.CommandError as error:
                raise commands.BadArgument(
                    f"Could not find a user matching {raw_user}."
                ) from error
        else:
            target_user = ctx.author

        if selected_grid_size is not None and selected_mode == "list":
            raise commands.BadArgument("Grid size can only be used with history/grid.")
        if selected_grid_size is not None:
            selected_mode = "grid"

        # ``server`` without another mode keeps the old server-avatar list
        # behavior. Supplying ``server history`` or ``server 6x7`` selects the
        # grid instead.
        if use_server and selected_mode is None:
            selected_mode = "list"

        if selected_mode is None:
            await self._send_user_avatar(ctx, target_user)
            return

        if not await self._ensure_avatar_history_consent(ctx):
            return

        guild_id: int | None = None
        if use_server:
            if ctx.guild is None:
                raise commands.NoPrivateMessage(
                    "Server avatar history can only be viewed in a server."
                )
            guild_id = ctx.guild.id
            if not isinstance(target_user, discord.Member):
                member = ctx.guild.get_member(target_user.id)
                if member is None:
                    try:
                        member = await ctx.guild.fetch_member(target_user.id)
                    except discord.HTTPException as error:
                        raise commands.BadArgument(
                            "That user is not a member of this server."
                        ) from error
                target_user = member

        history_user = cast(discord.User, target_user)
        if selected_mode == "grid":
            await logging.avatars_grid(
                ctx, history_user, guild_id, grid_size=selected_grid_size
            )
        else:
            await logging.avatars_func(ctx, history_user, guild_id)

    async def _ensure_avatar_history_consent(self, ctx: Context) -> bool:
        """Ask for saved-avatar consent only when ``avatar`` opens history."""
        if (
            ctx.author.bot
            or ctx.bot.db_cache.tracking_consent_given(ctx.author.id)
            or ctx.author.id in ctx.bot.db_cache.tracking_disabled_users
        ):
            return True

        # Import lazily to avoid making the info cog depend on the bot module
        # while the core package is still initializing.
        from core.bot import TrackingConsentView

        view = TrackingConsentView(ctx)
        send_kwargs: dict[str, Any] = {
            "view": view,
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if ctx.interaction is not None:
            send_kwargs["ephemeral"] = True
        view.message = await ctx.send(**send_kwargs)
        return False

    @commands.command(name="banner")
    async def user_banner(
        self,
        ctx: Context,
        *,
        user: Union[discord.Member, discord.User] = commands.Author,
    ):
        """Get or edit a user's banner"""
        await self._send_user_banner(ctx, user)

    async def _send_user_banner(
        self,
        ctx: Context,
        user: Union[discord.Member, discord.User] = commands.Author,
    ) -> None:
        user = await self.bot.fetch_user(user.id)
        if not user.banner:
            raise commands.BadArgument("User has no banner.")

        embed = discord.Embed(color=user.accent_color or self.bot.embedcolor)
        embed.set_author(name=f"{user}'s banner", icon_url=user.display_avatar.url)

        file = await user.banner.to_file()

        embed.set_image(url=f"attachment://{file.filename}")

        await ctx.send(embed=embed, file=file)

    @userinfo.group(name="avatar", fallback="get")
    @app_commands.describe(user="User whose avatar should be shown.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def user_avatar_group(
        self,
        ctx: Context,
        *,
        user: Union[discord.Member, discord.User] = commands.Author,
    ) -> None:
        """Get a user's current avatar."""
        await self._send_user_avatar(ctx, user)

    @user_avatar_group.command(name="history")
    @app_commands.describe(user="User whose avatar history should be shown.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def user_avatar_history(
        self, ctx: Context, *, user: discord.User = commands.Author
    ) -> None:
        """Show a user's saved avatar history in a grid."""
        logging = self.bot.logging
        if logging is None:
            raise commands.BadArgument("Could not find logging cog.")
        await logging.avatars_grid(ctx, user)

    @user_avatar_group.command(name="list")
    @app_commands.describe(user="User whose saved avatars should be listed.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def user_avatar_list(
        self, ctx: Context, *, user: discord.User = commands.Author
    ) -> None:
        """List a user's saved avatars."""
        logging = self.bot.logging
        if logging is None:
            raise commands.BadArgument("Could not find logging cog.")
        await logging.avatars_func(ctx, user)

    @userinfo.command(name="banner")
    @app_commands.describe(user="User whose banner should be shown.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def user_banner_subcommand(
        self,
        ctx: Context,
        *,
        user: Union[discord.Member, discord.User] = commands.Author,
    ) -> None:
        """Get a user's banner."""
        await self._send_user_banner(ctx, user)

    @userinfo.command(name="nicknames", aliases=("nicks",))
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    @app_commands.describe(
        member="Server member whose nickname history should be shown."
    )
    async def user_nicknames(
        self, ctx: GuildContext, *, member: discord.Member = commands.Author
    ) -> None:
        """Show a member's nickname history in this server."""
        logging = self.bot.logging
        if logging is None:
            raise commands.BadArgument("Could not find logging cog.")
        await logging._nicknames(ctx, member)

    @userinfo.command(name="usernames")
    @app_commands.describe(user="User whose username history should be shown.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def user_usernames(
        self, ctx: Context, *, user: discord.User = commands.Author
    ) -> None:
        """Show a user's previous usernames."""
        logging = self.bot.logging
        if logging is None:
            raise commands.BadArgument("Could not find logging cog.")
        await logging._usernames(ctx, user)

    @userinfo.command(name="names", aliases=("display_names", "displaynames"))
    @app_commands.describe(user="User whose display-name history should be shown.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def user_display_names(
        self, ctx: Context, *, user: discord.User = commands.Author
    ) -> None:
        """Show a user's previous display names."""
        logging = self.bot.logging
        if logging is None:
            raise commands.BadArgument("Could not find logging cog.")
        await logging._display_names(ctx, user)

    @userinfo.command(name="discrims", aliases=("discriminators",))
    @app_commands.describe(user="User whose discriminator history should be shown.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def user_discrims(
        self, ctx: Context, *, user: discord.User = commands.Author
    ) -> None:
        """Show a user's previous discriminators."""
        logging = self.bot.logging
        if logging is None:
            raise commands.BadArgument("Could not find logging cog.")
        await logging._discrims(ctx, user)

    @userinfo.command(name="servertags", aliases=("stags",))
    @app_commands.describe(user="User whose server tag history should be shown.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def user_servertags(
        self, ctx: Context, *, user: discord.User = commands.Author
    ) -> None:
        """Show a user's previous primary server tags."""
        logging = self.bot.logging
        if logging is None:
            raise commands.BadArgument("Could not find logging cog.")
        await logging._server_tags(ctx, user)

    @userinfo.command(name="joins")
    @app_commands.describe(user="User whose server join history should be shown.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def user_joins(
        self, ctx: Context, *, user: discord.User = commands.Author
    ) -> None:
        """Show a user's server join statistics."""
        logging = self.bot.logging
        if logging is None:
            raise commands.BadArgument("Could not find logging cog.")
        await logging._joins_user_stats(ctx, user)

    @userinfo.command(name="stats")
    @app_commands.describe(user="User whose command statistics should be shown.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def user_stats(
        self, ctx: Context, *, user: discord.User = commands.Author
    ) -> None:
        """Show a user's most-used commands."""
        tools = self.bot.tools
        if tools is None:
            raise commands.BadArgument("Could not find tools cog.")
        await tools._user_top(ctx, user)

    @userinfo.command(
        name="status-calendar",
        aliases=("statuscalendar", "statuscal", "statushistory", "statuses", "status"),
    )
    @commands.guild_only()
    @app_commands.describe(
        member="Server member whose status calendar should be shown."
    )
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def user_status_calendar(
        self, ctx: GuildContext, *, member: discord.Member = commands.Author
    ) -> None:
        """Show a member's daily status activity over the last 31 days."""
        logging = self.bot.logging
        if logging is None:
            raise commands.BadArgument("Could not find logging cog.")
        async with ctx.typing():
            await logging._status_calendar(ctx, member)

    async def server_info(self, ctx: GuildContext, guild: discord.Guild):
        embed = discord.Embed(timestamp=guild.created_at)
        images = []

        name = (
            f"{guild.name}  •  {guild.vanity_url}" if guild.vanity_url else guild.name
        )
        embed.set_author(name=name)

        if guild.description:
            embed.description = discord.utils.escape_markdown(guild.description)

        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
            images.append(f"[Icon]({guild.icon.url})")

        if guild.banner:
            images.append(f"[Banner]({guild.banner.url})")

        if guild.splash:
            images.append(f"[Splash]({guild.splash.url})")

        bots = sum(m.bot for m in guild.members)
        embed.add_field(
            name="Members",
            value=f"{guild.member_count:,} ({bots:,} bots)",
        )

        channels_text = f"{len(guild.channels):,}"
        private = sum(
            not c.permissions_for(ctx.author).read_messages for c in guild.channels
        )
        if private > 0:
            channels_text += f" ({private:,} private)"

        embed.add_field(name="Channels", value=channels_text)

        embed.add_field(name="Roles", value=f"{len(guild.roles):,} Roles")

        embed.add_field(name="Owner", value=f"<@{guild.owner_id}>", inline=True)

        embed.add_field(
            name=f"Level {guild.premium_tier}",
            value=f"{len(guild.premium_subscribers):,} Boosters\n"
            f"{guild.premium_subscription_count:,} Boosts",
        )

        emoji_stats = Counter()
        for emoji in guild.emojis:
            if emoji.animated:
                emoji_stats["animated"] += 1
                emoji_stats["animated_disabled"] += not emoji.available
            else:
                emoji_stats["regular"] += 1
                emoji_stats["disabled"] += not emoji.available

        fmt = (
            f'{emoji_stats["regular"]}/{guild.emoji_limit} Regular\n'
            f'{emoji_stats["animated"]}/{guild.emoji_limit} Animated\n'
        )
        if emoji_stats["disabled"] or emoji_stats["animated_disabled"]:
            fmt = f'{fmt}Disabled: {emoji_stats["disabled"]} regular, {emoji_stats["animated_disabled"]} animated\n'

        embed.add_field(name="Emojis", value=fmt)

        if bool(images):
            embed.add_field(name="Images", value=", ".join(images), inline=False)

        embed.set_footer(text=f"ID: {guild.id} \nCreated at")
        await ctx.send(embed=embed)

    @commands.group(name="serverinfo", aliases=("server", "si"))
    @commands.guild_only()
    async def serverinfo(
        self, ctx: GuildContext, *, guild: discord.Guild = commands.CurrentGuild
    ):
        """Show information and statistics for a server."""
        await self.server_info(ctx, guild)

    async def server_icon(self, ctx: Context, guild: discord.Guild):
        if guild.icon is None:
            raise commands.BadArgument(f"{guild} has no icon.")

        embed = discord.Embed(color=self.bot.embedcolor, title=f"{guild}'s icon")
        file = await guild.icon.to_file()

        embed.set_image(url=f"attachment://{file.filename}")

        await ctx.send(embed=embed, file=file)

    async def server_banner(self, ctx: Context, guild: discord.Guild):
        if guild.banner is None:
            raise commands.BadArgument(f"{guild} has no banner.")

        embed = discord.Embed(color=self.bot.embedcolor, title=f"{guild}'s banner")

        file = await guild.banner.to_file()

        embed.set_image(url=f"attachment://{file.filename}")

        await ctx.send(embed=embed, file=file)

    async def server_splash(self, ctx: Context, guild: discord.Guild):
        if guild.splash is None:
            raise commands.BadArgument(f"{guild} has no splash.")

        embed = discord.Embed(color=self.bot.embedcolor, title=f"{guild}'s splash")

        file = await guild.splash.to_file()

        embed.set_image(url=f"attachment://{file.filename}")

        await ctx.send(embed=embed, file=file)

    @serverinfo.command(name="icon")
    async def serverinfo_icon(
        self, ctx: Context, *, guild: discord.Guild = commands.CurrentGuild
    ):
        """Show a server's icon."""
        await self.server_icon(ctx, guild)

    @serverinfo.command(name="banner")
    async def serverinfo_banner(
        self, ctx: Context, *, guild: discord.Guild = commands.CurrentGuild
    ):
        """Show a server's banner."""
        await self.server_banner(ctx, guild)

    @serverinfo.command(name="splash", aliases=("invitebackground", "invitebg", "ibg"))
    async def serverinfo_splash(
        self, ctx: Context, *, guild: discord.Guild = commands.CurrentGuild
    ):
        """Show a server's invite background."""
        await self.server_splash(ctx, guild)

    @commands.command(name="icon")
    async def icon(self, ctx: Context, *, guild: discord.Guild = commands.CurrentGuild):
        """Show a server's icon."""
        await self.server_icon(ctx, guild)

    @commands.command(name="serverbanner", aliases=("sbanner",))
    async def server_banner_command(
        self, ctx: Context, *, guild: discord.Guild = commands.CurrentGuild
    ):
        """Show a server's banner."""
        await self.server_banner(ctx, guild)

    @commands.command(name="splash", aliases=("invitebackground", "invitebg", "ibg"))
    async def splash(
        self, ctx: Context, *, guild: discord.Guild = commands.CurrentGuild
    ):
        """Show a server's invite background."""
        await self.server_splash(ctx, guild)

    @commands.hybrid_command(name="reviews")
    @app_commands.describe(
        user="User whose reviews should be shown.",
        hidden="Include reviews marked hidden by the user.",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def reviews(
        self, ctx: Context, user: discord.User = commands.Author, hidden: bool = False
    ):
        """Get reviews for a user.

        These reviews are provided by ReviewDB ONLY. (for now)
        """

        await self.review_func(ctx, user, hidden)

    @commands.command(name="roleinfo")
    @commands.guild_only()
    async def roleinfo(self, ctx: Context, *, role: discord.Role):
        """Shows information about a role."""

        color_rgb = role.color.to_rgb()

        embed = discord.Embed(color=role.color or self.bot.embedcolor)
        embed.set_author(
            name=f"{role.name}  ·  {role.id}",
            icon_url=(
                cast(Any, ctx.guild).icon.url
                if cast(Any, ctx.guild).icon
                else ctx.author.display_avatar.url
            ),
        )
        info = f"""
        Colo{'' if random.randint(0,1) == 1 else "u"}r: `#{role.color.value:06X}` - `RGB({color_rgb[0]}, {color_rgb[1]}, {color_rgb[2]})`
        Mentionable: {'Yes' if role.mentionable else 'No'}
        Members: {len(role.members):,}
        Bots: {sum(1 for b in role.members if b.bot):,}
        """

        embed.set_footer(text="Created at")
        embed.timestamp = role.created_at
        embed.description = info

        view = discord.ui.View(timeout=120)
        view.add_item(RoleDropdown(ctx, role, embed))
        await ctx.send(embed=embed, view=view)
