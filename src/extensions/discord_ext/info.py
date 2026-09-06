from __future__ import annotations

import datetime
import hashlib
import random
import re
import shlex
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
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
    AllChannels,
    AuthorView,
    LayoutPageModal,
    LayoutPager,
    Review,
    ReviewPageSource,
    ReviewSender,
    build_layout_pagination_row,
    fish_go_back,
    get_user_badges,
    human_join,
    refresh_user_badges,
    render_user_badge,
)

if TYPE_CHECKING:
    from extensions.context import Context, GuildContext

statuses: TypeAlias = Union[
    Literal["online"], Literal["offline"], Literal["dnd"], Literal["idle"]
]


AVATAR_GRID_RE = re.compile(
    r"^(?P<width>\d{1,2})\s*x\s*(?P<height>\d{1,2})$", re.IGNORECASE
)

# Saved avatar/name/icon history can be exposed in the application-command
# tree through this single switch. Keeping the switch next to the command
# declarations makes it straightforward to disable those options again
# without rewriting the text handlers.
HISTORY_APP_COMMANDS_ENABLED = True


BURPLE = discord.ButtonStyle.blurple
GREEN = discord.ButtonStyle.green

UPLOADER_GUILD_ID = 939497177821110272
UPLOADER_ROLE_ID = 1538971877563703427

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
        profile_title: str | None = None,
        relationship_line: str | None = None,
    ) -> None:
        super().__init__(timeout=120)
        self.ctx = ctx
        self.user = user
        self.fetched_user = fetched_user
        self.index_body = index_body
        self.footer_text = footer_text
        self.server_tag = server_tag
        self.avatar_media_url = avatar_media_url or user.display_avatar.url
        default_accent = getattr(ctx, "embedcolor", ctx.bot.embedcolor)
        self.accent_color = default_accent if accent_color is None else accent_color
        self.profile_title = profile_title
        self.relationship_line = relationship_line
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
            profile_lines = [self.relationship_line or self.user.mention]
            if self.profile_title:
                profile_lines.insert(0, f"-# {self.profile_title}")
            if self.index_body:
                profile_lines.append(self.index_body)
            children = [self._profile_section(title, "\n".join(profile_lines))]
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


class BadgeView(discord.ui.LayoutView):
    """Components V2 profile view for a user's active badges."""

    def __init__(
        self,
        ctx: Context,
        user: Union[discord.User, discord.Member],
        entries: list[dict[str, Any]],
        *,
        notice: str | None = None,
    ) -> None:
        super().__init__(timeout=120)
        self.ctx = ctx
        self.user = user
        self.entries = entries
        self.notice = notice
        self._render()

    def _render(self) -> None:
        lines = [str(entry.get("_rendered") or "") for entry in self.entries]
        lines = [line for line in lines if line]
        body = "\n".join(lines) if lines else "No badges."
        # A user can own many custom badges. Keep each TextDisplay under
        # Discord's 4,000-character limit while still showing every badge.
        if len(body) > 3_900:
            body = body[:3_850].rsplit("\n", 1)[0]
            body += "\n…"
        if self.notice:
            body += f"\n\n-# {self.notice}"
        section = discord.ui.Section(
            discord.ui.TextDisplay(f"## {self.user}'s badges"),
            discord.ui.TextDisplay(body),
            accessory=discord.ui.Thumbnail(self.user.display_avatar.url),
        )
        self.add_item(
            discord.ui.Container(section, accent_color=self.ctx.bot.embedcolor)
        )

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            "Only the person who ran this command can use these controls.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False


def _badge_order_selectors(value: str) -> list[str]:
    """Split order selectors while allowing quoted display names."""

    value = str(value).strip()
    if not value:
        return []
    # Commas and pipes are convenient for custom display names containing
    # spaces. Otherwise shell-like quoting lets users use ``"Badge name"``.
    if "," in value or "|" in value:
        return [part.strip() for part in re.split(r"\s*[,|]\s*", value) if part.strip()]
    try:
        return [part for part in shlex.split(value) if part]
    except ValueError as error:
        raise commands.BadArgument("Could not parse the badge order.") from error


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

    @discord.ui.button(label="Edit", row=0, style=BURPLE)
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
    async def _marriage_profile_line(
        self, user: Union[discord.Member, discord.User]
    ) -> str | None:
        """Return the relationship line shown in a user's profile.

        The social commands live in the Fun cog, so userinfo deliberately
        uses a small duck-typed hook instead of importing that cog (which
        would create a circular extension dependency).  A profile should
        remain usable while the social migration is being rolled out, hence a
        missing table or unavailable cog simply omits the optional line.
        """

        get_cog = getattr(self.bot, "get_cog", None)
        social = get_cog("Fun") if callable(get_cog) else None
        resolver = getattr(social, "marriage_for", None)
        if not callable(resolver):
            return None
        try:
            marriage = await cast(
                Callable[[int], Awaitable[Any]], resolver
            )(int(user.id))
        except Exception:
            logger = getattr(self.bot, "logger", None)
            if logger is not None:
                logger.debug(
                    "Could not load marriage for user %s", user.id, exc_info=True
                )
            return None
        if marriage is None:
            return None
        proposer_id = int(getattr(marriage, "proposer_id", 0))
        recipient_id = int(getattr(marriage, "recipient_id", 0))
        partner_id = recipient_id if proposer_id == user.id else proposer_id
        if not partner_id:
            return None
        try:
            ring_rows = await self.bot.pool.fetch(
                """
                SELECT user_rings.user_id, ring_catalog.display
                FROM user_rings
                JOIN ring_catalog USING (ring_key)
                WHERE user_rings.user_id = ANY($1::BIGINT[])
                  AND user_rings.quantity > 0
                  AND user_rings.equipped_count > 0
                """,
                [int(user.id), partner_id],
            )
        except Exception:
            logger = getattr(self.bot, "logger", None)
            if logger is not None:
                logger.debug(
                    "Could not load marriage rings for user %s", user.id, exc_info=True
                )
            ring_rows = []
        rings = {
            int(row["user_id"]): str(row["display"] or "").strip()
            for row in ring_rows
            if row["display"]
        }
        left = " ".join(
            part for part in (user.mention, rings.get(int(user.id), "")) if part
        )
        right = " ".join(
            part for part in (rings.get(partner_id, ""), f"<@{partner_id}>") if part
        )
        return f"{left} x {right}"

    async def _active_profile_titles(self, user_id: int) -> list[str]:
        """Return the user's active purchased titles for the profile header."""

        try:
            rows = await self.bot.pool.fetch(
                """
                SELECT text
                FROM user_titles
                WHERE user_id = $1 AND active AND equipped
                ORDER BY purchased_at DESC, title_key ASC
                """,
                int(user_id),
            )
        except asyncpg.PostgresError:
            # Keep userinfo usable while a deployment is applying the titles
            # migration or on an older database that has no title table yet.
            self.bot.logger.debug(
                "Could not load profile titles for user %s", user_id, exc_info=True
            )
            return []

        titles: list[str] = []
        for row in rows:
            value = row.get("text") if isinstance(row, Mapping) else row["text"]
            if value is None:
                continue
            text = str(value).replace("\r", " ").replace("\n", " ").strip()
            if text:
                titles.append(
                    discord.utils.escape_markdown(discord.utils.escape_mentions(text))
                )
        return titles

    async def has_uploader_badge(self, user_id: int) -> bool:
        """Return whether a user currently holds Fishie's uploader role."""

        guild = self.bot.get_guild(UPLOADER_GUILD_ID)
        if guild is None:
            return False
        member = guild.get_member(user_id)
        if member is None and not guild.chunked:
            try:
                member = await guild.fetch_member(user_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return False
        return member is not None and any(
            role.id == UPLOADER_ROLE_ID for role in member.roles
        )

    async def has_nitro(
        self, member: discord.Member, fetched_user: Optional[discord.User] = None
    ) -> bool:
        # Bot accounts cannot have Nitro.  Apart from avoiding unnecessary
        # API requests, this prevents activity/avatar heuristics from adding
        # the Nitro badge to bot profiles.
        if member.bot:
            return False
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

    @staticmethod
    def _badge_entry_key(entry: Mapping[str, Any], fallback: str) -> str:
        """Return the stable key used by the per-user badge order table."""

        key = str(entry.get("badge_key") or "").strip()
        if key:
            return key
        return fallback

    @staticmethod
    def _legacy_badge_key(user_id: int, index: int, entry: Mapping[str, Any]) -> str:
        """Give a legacy ``user_flags`` entry a key that survives restarts.

        Older JSON entries did not carry a badge key.  Hashing their parsed
        contents keeps ordering stable while avoiding user-controlled text in
        SQL values.
        """

        payload = "\x1f".join(
            str(entry.get(name) or "")
            for name in ("emoji_name", "emoji_id", "is_custom", "animated", "text")
        )
        digest = hashlib.sha1(
            payload.encode("utf-8"), usedforsecurity=False
        ).hexdigest()[:16]
        return f"json:{user_id}:{index}:{digest}"

    async def _badge_order(self, user_id: int) -> list[str]:
        """Read a saved order, tolerating installations before migration 64."""

        try:
            row = await self.bot.pool.fetchrow(
                "SELECT badge_keys FROM user_badge_orders WHERE user_id = $1",
                int(user_id),
            )
        except asyncpg.PostgresError:
            # The command remains usable while an operator is rolling out the
            # migration; the first successful order save will persist it.
            return []
        if row is None:
            return []
        values = row["badge_keys"]
        if not isinstance(values, (list, tuple)):
            return []
        return [str(value) for value in values if str(value).strip()]

    async def _save_badge_order(self, user_id: int, keys: list[str]) -> None:
        await self.bot.pool.execute(
            """
            INSERT INTO user_badge_orders (user_id, badge_keys, updated_at)
            VALUES ($1, $2::text[], now())
            ON CONFLICT (user_id) DO UPDATE
            SET badge_keys = EXCLUDED.badge_keys,
                updated_at = now()
            """,
            int(user_id),
            keys,
        )

    async def get_badge_entries(
        self,
        member: Union[discord.Member, discord.User],
        ctx: Context,
        fetched_user: Optional[discord.User] = None,
    ) -> list[dict[str, Any]]:
        """Collect active badges and apply the user's saved display order."""

        public_flags: Dict[Any, Any] = dict(member.public_flags)
        new_values = {
            "owner": await self.bot.is_owner(member),
            "server_owner": isinstance(member, discord.Member)
            and member.guild.owner == member,
            "booster": isinstance(member, discord.Member) and member.premium_since,
            "nitro": (
                await self.has_nitro(member, fetched_user)
                if isinstance(member, discord.Member)
                else False
            ),
            "uploader": await self.has_uploader_badge(member.id),
        }
        public_flags.update(new_values)

        entries: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        seen_rendered: set[str] = set()

        def add_entry(entry: object, key: str) -> None:
            if not isinstance(entry, dict):
                return
            normalized = dict(entry)
            normalized["_badge_key"] = key
            source = str(normalized.get("badge_source") or "").casefold()
            catalog_key = str(
                normalized.get("catalog_key") or normalized.get("badge_key") or ""
            ).casefold()
            # Stat badges and the built-in purchasable badges use a
            # consistent title-cased display label. Owner-given badges,
            # purchased custom emojis, and JSON user flags are authored text
            # and must remain exactly as supplied.
            bought_custom = source == "purchase" and (
                catalog_key == "purchase:custom"
                or catalog_key.startswith("purchase:custom:")
            )
            if source == "stat" or (source == "purchase" and not bought_custom):
                normalized["text"] = str(normalized.get("text") or "").title()
            rendered = render_user_badge(normalized)
            if not rendered or key in seen_keys or rendered in seen_rendered:
                return
            seen_keys.add(key)
            seen_rendered.add(rendered)
            normalized["_rendered"] = rendered
            entries.append(normalized)

        document = refresh_user_badges()
        for flag, entry in document.get("flags", {}).items():
            if public_flags.get(flag):
                add_entry(entry, f"flag:{flag}")

        # JSON is the editable source of the custom badge.  Owner commands
        # keep the database row and this catalog synchronized, so removing or
        # changing an entry in the file takes effect without a restart.
        for index, badge in enumerate(get_user_badges(member.id)):
            add_entry(
                badge,
                self._badge_entry_key(
                    badge, self._legacy_badge_key(member.id, index, badge)
                ),
            )

        # Purchased and stat-derived badges are persisted in PostgreSQL and
        # cached during bot startup. They are intentionally not copied into
        # the editable JSON catalog, so include the active runtime rows here.
        runtime_badges = getattr(self.bot.db_cache, "user_badges", {}).get(
            member.id, []
        )
        # Purchases and achievement rows can be created after startup. Read
        # active rows when the profile is rendered so a newly earned or
        # purchased badge appears immediately instead of waiting for a cache
        # refresh.
        database_badges: list[object] = []
        database_badges_loaded = False
        badge_service = getattr(self.bot, "badges", None)
        if badge_service is not None:
            try:
                database_badges = await badge_service.owned(member.id)
                # An empty result is authoritative: ``owned`` only returns
                # active rows.  Do not merge the startup cache in that case,
                # because it may still contain a badge sold/revoked after the
                # bot started.
                database_badges_loaded = True
            except Exception:
                self.bot.logger.debug(
                    "Could not load runtime badges for user %s",
                    member.id,
                    exc_info=True,
                )
        # Prefer rows read from the database because they retain
        # ``badge_source``/``catalog_key`` metadata. The startup cache is a
        # fallback for rows created before the profile query is available.
        cached_badges = () if database_badges_loaded else tuple(runtime_badges)
        for index, raw_badge in enumerate((*database_badges, *cached_badges)):
            if isinstance(raw_badge, dict):
                badge = raw_badge
            else:
                try:
                    badge = dict(cast(Any, raw_badge))
                except (TypeError, ValueError):
                    continue
            badge_id = badge.get("id", index)
            add_entry(
                badge,
                self._badge_entry_key(badge, f"db:{member.id}:{badge_id}"),
            )

        saved_order = await self._badge_order(member.id)
        if saved_order:
            by_key = {str(entry["_badge_key"]): entry for entry in entries}
            ordered: list[dict[str, Any]] = []
            added: set[str] = set()
            for key in saved_order:
                entry = by_key.get(key)
                if entry is not None:
                    ordered.append(entry)
                    added.add(key)
            ordered.extend(
                entry for entry in entries if str(entry["_badge_key"]) not in added
            )
            entries = ordered

        return entries

    async def get_badges(
        self,
        member: Union[discord.Member, discord.User],
        ctx: Context,
        fetched_user: Optional[discord.User] = None,
    ) -> List[str]:
        """Return rendered badges for compatibility with existing callers."""

        entries = await self.get_badge_entries(member, ctx, fetched_user)
        return [str(entry["_rendered"]) for entry in entries]

    def join_pos(self, member: discord.Member) -> int:
        members = sorted(
            member.guild.members, key=lambda m: m.joined_at or discord.utils.utcnow()
        )
        return members.index(member) + 1

    async def user_info(self, ctx: Context, user: Union[discord.Member, discord.User]):
        fuser = await self.bot.fetch_user(user.id)

        badge_entries = await self.get_badge_entries(user, ctx, fuser)
        badge_lines = [
            str(entry["_rendered"]) for entry in badge_entries if entry.get("_rendered")
        ]
        index_body = "**Badges:**\n" + "\n".join(badge_lines) if badge_lines else ""
        profile_titles = await self._active_profile_titles(user.id)
        profile_title = " · ".join(profile_titles) if profile_titles else None

        primary_guild = getattr(fuser, "primary_guild", None)
        server_tag = getattr(primary_guild, "tag", None)

        created_at = f"<t:{int(user.created_at.timestamp())}:D>"
        footer_details = [f"Created At: {created_at}"]
        if isinstance(user, discord.Member):
            if user.joined_at:
                footer_details.append(
                    f"Joined: <t:{int(user.joined_at.timestamp())}:D> "
                    f"(#{self.join_pos(user)})"
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
            footer_details.append(
                f"{str(row['status']).title()} "
                f"{discord.utils.format_dt(row['last_seen'], 'R')}"
            )

        identity_footer = f"-# ID: {user.id}"
        from extensions.fun.birthday import Birthday, birthday_timestamp

        birthday_row = await self.bot.pool.fetchrow(
            "SELECT month, day FROM user_birthdays WHERE user_id = $1", user.id
        )
        if birthday_row is not None:
            birthday = Birthday(birthday_row["month"], birthday_row["day"])
            stamp = await birthday_timestamp(
                self.bot.pool, birthday, ctx.author.id, user.id
            )
            identity_footer += f" · Birthday on {birthday.label()} ({discord.utils.format_dt(stamp, 'R')})"
        footer_lines = [f"-# {' · '.join(footer_details)}", identity_footer]

        marriage_line = await self._marriage_profile_line(user)

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
            index_body,
            "\n".join(footer_lines),
            server_tag,
            f"attachment://{avatar_file.filename}",
            # The command author's equipped colour takes precedence over the
            # profile accent colour of the user being inspected.
            ctx.embed_color,
            profile_title,
            relationship_line=marriage_line,
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
            accent_color=ctx.embedcolor,
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

    @commands.group(name="badges", invoke_without_command=True)
    async def badges(
        self,
        ctx: Context,
        *,
        user: Union[discord.Member, discord.User] = commands.Author,
    ) -> None:
        """Show a user's active profile badges."""

        await self._send_badges(ctx, user)

    async def _send_badges(
        self,
        ctx: Context,
        user: Union[discord.Member, discord.User],
    ) -> None:
        """Render the badge list used by both ``badges`` and ``profile``."""

        async with ctx.typing():
            fetched = await self.bot.fetch_user(user.id)
            entries = await self.get_badge_entries(user, ctx, fetched)
            view = BadgeView(ctx, user, entries)
            await ctx.send(
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @badges.command(name="order")
    async def badges_order(self, ctx: Context, *, badge_order: str = "") -> None:
        """Set the order of your badges without removing unspecified badges."""

        await self._set_badge_order(ctx, badge_order)

    async def _set_badge_order(self, ctx: Context, badge_order: str = "") -> None:
        """Apply a badge order and render the resulting list."""

        async with ctx.typing():
            user = cast(Union[discord.Member, discord.User], ctx.author)
            fetched = await self.bot.fetch_user(user.id)
            entries = await self.get_badge_entries(user, ctx, fetched)
            if not entries:
                await ctx.send(
                    view=BadgeView(ctx, user, entries, notice="You have no badges."),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            selectors = _badge_order_selectors(badge_order)
            if not selectors:
                await ctx.send(
                    view=BadgeView(ctx, user, entries),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            def find_matches(selector: str) -> list[dict[str, Any]]:
                folded = selector.strip().casefold()
                exact: list[dict[str, Any]] = []
                for entry in entries:
                    aliases = {
                        str(entry.get("_badge_key") or ""),
                        str(entry.get("badge_key") or ""),
                        str(entry.get("emoji_name") or ""),
                        str(entry.get("text") or ""),
                        str(entry.get("_rendered") or ""),
                    }
                    if any(alias.casefold() == folded for alias in aliases if alias):
                        exact.append(entry)
                return exact

            selected: list[dict[str, Any]] = []
            selected_keys: set[str] = set()
            for selector in selectors:
                matches = find_matches(selector)
                if not matches:
                    raise commands.BadArgument(
                        f"Could not find an active badge matching `{selector}`."
                    )
                if len(matches) > 1:
                    raise commands.BadArgument(
                        f"`{selector}` matches multiple badges; use its badge ID or name."
                    )
                entry = matches[0]
                key = str(entry["_badge_key"])
                if key in selected_keys:
                    continue
                selected.append(entry)
                selected_keys.add(key)

            # Any currently active badges omitted by the user stay visible at
            # the end, rather than being accidentally hidden by an old order.
            selected.extend(
                entry
                for entry in entries
                if str(entry["_badge_key"]) not in selected_keys
            )
            await self._save_badge_order(
                user.id, [str(entry["_badge_key"]) for entry in selected]
            )
            await ctx.send(
                view=BadgeView(ctx, user, selected, notice="Badge order updated."),
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _send_user_avatar(
        self,
        ctx: Context,
        user: Union[discord.Member, discord.User] = commands.Author,
    ) -> None:
        fuser = await self.bot.fetch_user(user.id)
        embed = discord.Embed(color=ctx.embed_color)
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

    async def _avatar_dispatch(
        self, ctx: Context, supplied_arguments: list[str]
    ) -> None:
        """Dispatch the shared text and slash avatar argument syntax."""
        logging = self.bot.logging
        if not logging:
            raise commands.BadArgument("Could not find logging cog.")

        raw_arguments = supplied_arguments
        selected_mode: Literal["grid", "list"] | None = None
        use_server = False
        edit_mode = False
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
            if lowered == "edit":
                edit_mode = True
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

        if edit_mode and selected_mode != "list":
            raise commands.BadArgument(
                "Edit mode can only be used with the avatar list."
            )

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
            await logging.avatars_func(
                ctx,
                history_user,
                guild_id,
                edit=edit_mode,
            )

    @commands.command(
        name="avatar",
        aliases=("pfp", "av", "avy", "avi"),
        extras={"usage": "[user] [history|grid|list] [server|guild] [WxH] [edit]"},
    )
    async def avatar(self, ctx: Context, *, arguments: str = "") -> None:
        """Show a user's avatar, history grid, or paginated avatar list."""
        await self._avatar_dispatch(ctx, arguments.split())

    @app_commands.command(name="avatar")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        user="Discord user, mention, ID, or name.",
        history="Show saved avatars in a grid instead of the current avatar.",
        list_mode="Show saved avatars as a paginated list.",
        server="Use this server's avatars instead of global avatars.",
        size="Grid size such as 6x7, used with history.",
        edit="Allow deleting entries from your own avatar list.",
    )
    @app_commands.rename(list_mode="list", size="size")
    async def avatar_app(
        self,
        interaction: discord.Interaction,
        user: discord.User | None = None,
        history: bool = False,
        list_mode: bool = False,
        server: bool = False,
        size: str | None = None,
        edit: bool = False,
    ) -> None:
        """Show a user's current avatar, history grid, or avatar list."""
        if not HISTORY_APP_COMMANDS_ENABLED and (
            history or list_mode or server or size or edit
        ):
            await interaction.response.send_message(
                "Avatar history options are currently unavailable.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if history and list_mode:
            await interaction.response.send_message(
                "Choose either history or list, not both.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if edit and not list_mode:
            await interaction.response.send_message(
                "Edit mode can only be used with the avatar list.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        # ``get_context`` is parameterized with the concrete Fishie client,
        # while discord.py exposes an unparameterized client on interactions.
        # The runtime object is still the same bot instance.
        ctx = cast(Context, await self.bot.get_context(cast(Any, interaction)))
        arguments: list[str] = []
        if user is not None:
            arguments.append(str(user.id))
        if history:
            arguments.append("history")
        if list_mode:
            arguments.append("list")
        if server:
            arguments.append("server")
        if size:
            arguments.append(size)
        if edit:
            arguments.append("edit")
        await self._avatar_dispatch(ctx, arguments)

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

        embed = discord.Embed(color=ctx.embed_color)
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

    @user_avatar_group.command(
        name="history", with_app_command=HISTORY_APP_COMMANDS_ENABLED
    )
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

    @user_avatar_group.command(
        name="list", with_app_command=HISTORY_APP_COMMANDS_ENABLED
    )
    @app_commands.describe(
        user="User whose saved avatars should be listed.",
        edit="Allow deleting entries from your own avatar list.",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def user_avatar_list(
        self,
        ctx: Context,
        user: discord.User = commands.Author,
        edit: bool = False,
    ) -> None:
        """List a user's saved avatars."""
        logging = self.bot.logging
        if logging is None:
            raise commands.BadArgument("Could not find logging cog.")
        await logging.avatars_func(ctx, user, edit=edit)

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

    @userinfo.command(name="birthday")
    @app_commands.describe(user="User whose birthday to show. Defaults to yourself.")
    async def user_birthday(self, ctx: Context, user: discord.User = commands.Author) -> None:
        """Show a user's next birthday."""
        command = self.bot.get_command("birthday")
        if command is None:
            raise commands.BadArgument("Birthday commands are unavailable right now.")
        await ctx.invoke(cast(Any, command), user=user)

    @userinfo.command(name="reviews")
    @app_commands.describe(
        user="User whose reviews should be shown.",
        hidden="Include reviews marked hidden by the user.",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def user_reviews(
        self, ctx: Context, user: discord.User = commands.Author, hidden: bool = False
    ) -> None:
        """Get reviews for a user from ReviewDB."""
        await self.review_func(ctx, user, hidden)

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

    @userinfo.command(name="usernames", with_app_command=HISTORY_APP_COMMANDS_ENABLED)
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

    @userinfo.command(
        name="names",
        aliases=("display_names", "displaynames"),
        with_app_command=HISTORY_APP_COMMANDS_ENABLED,
    )
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

    @userinfo.command(name="servertags", aliases=("stags", "servertag"))
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

    @userinfo.command(name="activity", aliases=("playing",))
    @commands.guild_only()
    @app_commands.describe(query="Activity name or server member to look up")
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def user_activity(
        self, ctx: GuildContext, *, query: str | None = None
    ) -> None:
        """Show activity details and members sharing that activity."""
        logging = self.bot.logging
        if logging is None:
            raise commands.BadArgument("Could not find logging cog.")
        activity_command = getattr(logging, "activity", None)
        callback = getattr(activity_command, "callback", None)
        if callback is None:
            raise commands.BadArgument("Activity commands are unavailable right now.")
        await cast(Any, callback)(logging, ctx, query=query)

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

    @commands.hybrid_group(name="server", aliases=("serverinfo", "si"), fallback="info")
    @commands.guild_only()
    async def serverinfo(
        self, ctx: GuildContext, *, guild: discord.Guild = commands.CurrentGuild
    ):
        """Show information and statistics for a server."""
        await self.server_info(ctx, guild)

    async def _server_settings_cog(self):
        settings = self.bot.settings
        if settings is None:
            raise commands.BadArgument("Could not find the server settings cog.")
        return settings

    @serverinfo.command(name="settings")
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def serverinfo_settings(self, ctx: GuildContext) -> None:
        """Open the server's tracking and automation settings panel."""
        settings = await self._server_settings_cog()
        await settings._send_server_settings_panel(ctx)

    @serverinfo.command(
        name="firstmessage",
        aliases=("firstmsg", "fmsg"),
    )
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def serverinfo_firstmessage(self, ctx: GuildContext) -> None:
        """Link to the first message sent in this channel and reply to it."""
        fun = self.bot.get_cog("Fun")
        firstmessage_command = getattr(fun, "firstmessage", None)
        callback = getattr(firstmessage_command, "callback", None)
        if callback is None:
            raise commands.BadArgument(
                "First-message commands are unavailable right now."
            )
        await cast(Any, callback)(fun, ctx)

    @serverinfo.group(name="edit")
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def serverinfo_edit(self, ctx: GuildContext) -> None:
        """Edit a server automation destination or its filters."""
        settings = await self._server_settings_cog()
        await settings._send_server_settings_panel(ctx)

    async def _server_edit_destination(self, ctx: GuildContext, kind: str) -> None:
        settings = await self._server_settings_cog()
        await settings._edit_server_destination(ctx, kind)

    @serverinfo_edit.command(
        name="hourly-posts", aliases=("hourly", "hourlyposts", "autoposts")
    )
    @commands.has_guild_permissions(manage_guild=True)
    async def serverinfo_edit_hourly_posts(self, ctx: GuildContext) -> None:
        """Set, move, or edit the hourly-post interval and media filters."""
        await self._server_edit_destination(ctx, "hourly_posts")

    @serverinfo_edit.command(name="honeypot", aliases=("honey-pot",))
    @commands.has_guild_permissions(manage_guild=True)
    async def serverinfo_edit_honeypot(self, ctx: GuildContext) -> None:
        """Set, move, or disable the honeypot channel."""
        await self._server_edit_destination(ctx, "honeypot")

    @serverinfo_edit.command(
        name="auto-reactions", aliases=("auto-reaction", "reactions")
    )
    @commands.has_guild_permissions(manage_guild=True)
    async def serverinfo_edit_auto_reactions(self, ctx: GuildContext) -> None:
        """Set, move, or disable the automatic reaction channel."""
        await self._server_edit_destination(ctx, "auto_reactions")

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

    @serverinfo.group(name="emojis", fallback="get")
    @app_commands.describe(
        guild="Server whose emojis should be listed.",
        name="Show emoji names instead of the emoji itself.",
        ids="Include each emoji ID in the results.",
    )
    async def server_emojis(
        self,
        ctx: GuildContext,
        guild: discord.Guild = commands.CurrentGuild,
        name: bool = False,
        ids: bool = False,
    ):
        """List the custom emojis in a server."""
        # The emoji cog owns the implementation and this Discord cog exposes
        # it under the ``server`` command group for a consistent UX.
        # ``Emojis`` is mixed into the ``Discord`` cog, rather than loaded as
        # its own cog. Reuse the existing callback directly so both commands
        # share the same paginator and formatting.
        emoji_command = getattr(self, "emojis", None)
        if emoji_command is None:
            raise commands.BadArgument("Emoji commands are unavailable right now.")
        await cast(Any, emoji_command).callback(
            self, ctx, guild=guild, name=name, ids=ids
        )

    @server_emojis.command(name="download")
    @app_commands.describe(disabled="Include emojis that are currently disabled.")
    @commands.has_permissions(manage_emojis=True)
    @commands.bot_has_permissions(manage_emojis=True)
    async def server_emojis_download(
        self,
        ctx: GuildContext,
        disabled: bool = commands.param(
            default=True, description="Download includes disabled emojis"
        ),
    ):
        """Download a server's custom emojis as a zip file."""
        emoji_download = getattr(self, "emoji_download", None)
        if emoji_download is None:
            raise commands.BadArgument("Emoji commands are unavailable right now.")
        await cast(Any, emoji_download).callback(self, ctx, disabled=disabled)

    async def _icon_dispatch(self, ctx: Context, arguments: list[str]) -> None:
        """Dispatch current-server icon and saved icon history views."""
        selected_mode: Literal["grid", "list"] | None = None
        edit_mode = False
        grid_size: tuple[int, int] | None = None
        for argument in arguments:
            value = argument.casefold()
            if value in {"history", "grid"}:
                if selected_mode == "list":
                    raise commands.BadArgument("Choose either history/grid or list.")
                selected_mode = "grid"
                continue
            if value == "list":
                if selected_mode == "grid":
                    raise commands.BadArgument("Choose either history/grid or list.")
                selected_mode = "list"
                continue
            if value == "edit":
                edit_mode = True
                continue
            match = AVATAR_GRID_RE.fullmatch(argument)
            if match:
                if grid_size is not None:
                    raise commands.BadArgument("Only one icon grid size is allowed.")
                width = int(match.group("width"))
                height = int(match.group("height"))
                if not 1 <= width <= 10 or not 1 <= height <= 10:
                    raise commands.BadArgument(
                        "Icon grid sizes must be between 1x1 and 10x10."
                    )
                grid_size = (width, height)
                continue
            raise commands.BadArgument(f"Unknown icon option: {argument}")

        if grid_size is not None:
            selected_mode = "grid"
        if edit_mode and selected_mode != "list":
            raise commands.BadArgument(
                "Edit mode can only be used with the server icon list."
            )
        if selected_mode is None:
            if ctx.guild is None:
                raise commands.NoPrivateMessage(
                    "This command can only be used in a server."
                )
            await self.server_icon(ctx, ctx.guild)
            return

        logging = self.bot.logging
        if logging is None or ctx.guild is None:
            raise commands.NoPrivateMessage(
                "Icon history can only be viewed in a server."
            )
        if selected_mode == "grid":
            await logging.icons_grid(ctx, ctx.guild, grid_size)
        else:
            await logging.icons_func(ctx, ctx.guild, edit=edit_mode)

    @commands.command(
        name="icon",
        extras={"usage": "[history|grid|list] [WxH] [edit]"},
    )
    async def icon(self, ctx: Context, *, arguments: str = ""):
        """Show a server icon or its saved icon history."""
        await self._icon_dispatch(ctx, arguments.split())

    @app_commands.command(name="icon")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        history="Show saved server icons in a grid.",
        list_mode="Show saved server icons as a paginated list.",
        size="Grid size such as 6x7, used with history.",
        edit="Allow deleting entries from the server icon list.",
    )
    @app_commands.rename(list_mode="list", size="size")
    async def icon_app(
        self,
        interaction: discord.Interaction,
        history: bool = False,
        list_mode: bool = False,
        size: str | None = None,
        edit: bool = False,
    ) -> None:
        """Show a server's current icon or its saved icon history."""
        if not HISTORY_APP_COMMANDS_ENABLED and (history or list_mode or size or edit):
            await interaction.response.send_message(
                "Icon history options are currently unavailable.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if history and list_mode:
            await interaction.response.send_message(
                "Choose either history or list, not both.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if edit and not list_mode:
            await interaction.response.send_message(
                "Edit mode can only be used with the server icon list.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        ctx = cast(Context, await self.bot.get_context(cast(Any, interaction)))
        if ctx.guild is None:
            raise commands.NoPrivateMessage(
                "This command can only be used in a server."
            )
        arguments: list[str] = []
        if history:
            arguments.append("history")
        if list_mode:
            arguments.append("list")
        if size:
            arguments.append(size)
        if edit:
            arguments.append("edit")
        await self._icon_dispatch(ctx, arguments)

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

    @commands.command(name="reviews")
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
