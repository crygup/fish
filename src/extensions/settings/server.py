from __future__ import annotations

import re
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from core import Cog
from utils import AuthorLayoutView, LayoutPager

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context, GuildContext


def to_lower(argument: str):
    return argument.lower()


async def _authorize_server_interaction(
    interaction: discord.Interaction,
    ctx: Context,
    guild: discord.Guild,
) -> bool:
    """Re-check the author and Manage Server permission for settings controls.

    A component can outlive the command invocation that created it. The
    command decorators therefore do not protect callbacks after a member's
    roles change (or when a stale component is submitted). Keep the original
    author boundary and verify the member's current guild permission before
    every mutation, including modal submissions.
    """

    if interaction.guild_id != guild.id:
        message = "These server settings can only be used in their original server."
    elif interaction.user.id != ctx.author.id:
        message = "Only the person who opened these server settings can use them."
    else:
        member: discord.Member | None = (
            interaction.user if isinstance(interaction.user, discord.Member) else None
        )
        if member is None or member.guild.id != guild.id:
            member = guild.get_member(interaction.user.id)
        allowed = bool(
            member
            and (
                getattr(guild, "owner_id", None) == member.id
                or bool(
                    getattr(
                        getattr(member, "guild_permissions", None),
                        "manage_guild",
                        False,
                    )
                )
            )
        )
        if allowed:
            return True
        message = (
            "You no longer have Manage Server permission to change these settings."
        )

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
    return False


class Dropdown(discord.ui.ChannelSelect):
    def __init__(self, ctx: GuildContext):
        self.ctx = ctx
        super().__init__(
            placeholder="Choose a channel.",
            min_values=1,
            max_values=1,
            channel_types=[discord.ChannelType.text],
        )

    async def callback(self, interaction: discord.Interaction):
        channel: discord.TextChannel = self.values[0]  # type: ignore

        if self.ctx.bot.settings is None:
            raise commands.BadArgument("Settings cog could not be found somehow.")

        await self.ctx.bot.settings.add_adl_channel(channel)

        if isinstance(self.view, DropdownView):
            self.view.status.content = f"## Auto-download settings\nAuto-download channel set to {channel.mention}."
            self.disabled = True
            await interaction.response.edit_message(
                view=self.view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await interaction.response.send_message(
            f"Auto-download channel set to {channel.mention}.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class DropdownView(AuthorLayoutView):
    def __init__(self, ctx: GuildContext):
        super().__init__(ctx, timeout=180)
        self.status = discord.ui.TextDisplay(
            "## Auto-download settings\nChoose the channel for automatic downloads."
        )
        self.dropdown = Dropdown(ctx)
        self.add_item(
            discord.ui.Container(
                self.status,
                discord.ui.ActionRow(self.dropdown),
                accent_color=ctx.bot.embedcolor,
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.ctx.guild is None:
            return False
        return await _authorize_server_interaction(
            interaction, self.ctx, self.ctx.guild
        )


class SettingsMessageView(AuthorLayoutView):
    """Render a one-off server-settings response as Components V2."""

    def __init__(self, ctx: GuildContext, text: str) -> None:
        super().__init__(ctx, timeout=120)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(text),
                accent_color=ctx.bot.embedcolor,
            )
        )


class PrefixPageSource:
    def __init__(self, entries: list[tuple[str, str]], guild_name: str) -> None:
        self.entries = entries
        self.guild_name = guild_name

    def get_max_pages(self) -> int:
        return max(1, (len(self.entries) + 3) // 4)

    def format_page(self, page_number: int) -> list[discord.ui.Item[Any]]:
        start = page_number * 4
        page_entries = self.entries[start : start + 4]
        lines = [f"## Prefixes in {self.guild_name}"]
        lines.extend(f"**{prefix}**\n{details}" for prefix, details in page_entries)
        lines.append(f"\n-# Page {page_number + 1}/{self.get_max_pages()}")
        return [discord.ui.TextDisplay("\n\n".join(lines))]


class _LegacyServerSettingsView(AuthorLayoutView):
    """Interactive server settings panel used by ``settings server``."""

    def __init__(self, ctx: GuildContext, values: dict[str, object]) -> None:
        super().__init__(ctx, timeout=300)
        if ctx.guild is None:
            raise ValueError("Server settings can only be used in a guild.")
        self.guild = ctx.guild
        self.values = values
        self._render()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _authorize_server_interaction(interaction, self.ctx, self.guild)

    @classmethod
    async def create(cls, ctx: GuildContext) -> "_LegacyServerSettingsView":
        row = await ctx.bot.pool.fetchrow(
            """
            SELECT tracking_enabled, history_public, auto_download, poketwo,
                   auto_reactions, pinboard
            FROM guild_settings WHERE guild_id = $1
            """,
            ctx.guild.id,
        )
        honeypot = await ctx.bot.pool.fetchval(
            "SELECT channel_id FROM honeypot_channels WHERE guild_id = $1",
            ctx.guild.id,
        )
        values = {
            "tracking_enabled": bool(row["tracking_enabled"]) if row else True,
            # Guild history is public by default; migration 0075 updates
            # existing rows and this fallback covers a guild before its first
            # settings row is created.
            "history_public": bool(row["history_public"]) if row else True,
            "auto_download": row["auto_download"] if row else None,
            "poketwo": bool(row["poketwo"]) if row else False,
            "auto_reactions": bool(row["auto_reactions"]) if row else False,
            "pinboard": row["pinboard"] if row else None,
            "honeypot": honeypot,
        }
        return cls(ctx, values)

    @staticmethod
    def _channel_mention(channel_id: object) -> str:
        return f"<#{channel_id}>" if channel_id else "Disabled"

    @property
    def content(self) -> str:
        tracking = "Enabled" if self.values["tracking_enabled"] else "Disabled"
        visibility = "Public" if self.values["history_public"] else "Private"
        poketwo = "Enabled" if self.values["poketwo"] else "Disabled"
        reactions = "Enabled" if self.values["auto_reactions"] else "Disabled"
        return (
            f"## Server settings · {self.guild.name}\n"
            f"**Tracking:** {tracking}\n"
            f"**Saved history:** {visibility}\n"
            f"**Pokétwo auto-solving:** {poketwo}\n"
            f"**Auto reactions:** {reactions}\n"
            f"**Auto-download:** {self._channel_mention(self.values['auto_download'])}\n"
            f"**Pinboard:** {self._channel_mention(self.values['pinboard'])}\n"
            f"**Honeypot:** {self._channel_mention(self.values['honeypot'])}\n\n"
            "Use the channel selectors to choose destinations. Server history is "
            "private until an administrator makes it public."
        )

    def _render(self) -> None:
        self.clear_items()
        buttons: list[discord.ui.Button] = []
        controls = (
            (
                (
                    "Disable tracking"
                    if self.values["tracking_enabled"]
                    else "Enable tracking"
                ),
                "tracking_enabled",
            ),
            (
                (
                    "Make history private"
                    if self.values["history_public"]
                    else "Make history public"
                ),
                "history_public",
            ),
            (
                "Disable Pokétwo" if self.values["poketwo"] else "Enable Pokétwo",
                "poketwo",
            ),
            (
                (
                    "Disable auto reactions"
                    if self.values["auto_reactions"]
                    else "Enable auto reactions"
                ),
                "auto_reactions",
            ),
        )
        for label, setting in controls:
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary)

            async def callback(
                interaction: discord.Interaction,
                selected: str = setting,
            ) -> None:
                value = not bool(self.values[selected])
                await self.ctx.bot.pool.execute(
                    f"""
                    INSERT INTO guild_settings (guild_id, {selected})
                    VALUES ($1, $2)
                    ON CONFLICT (guild_id) DO UPDATE
                    SET {selected} = EXCLUDED.{selected}
                    """,
                    self.guild.id,
                    value,
                )
                self.values[selected] = value
                cache = self.ctx.bot.db_cache
                if selected == "tracking_enabled":
                    if value:
                        cache.guild_tracking_disabled.discard(self.guild.id)
                    else:
                        cache.guild_tracking_disabled.add(self.guild.id)
                elif selected == "history_public":
                    cache.set_guild_history_public(self.guild.id, value)
                elif selected == "poketwo":
                    (cache.add_poketwo if value else cache.remove_poketwo)(
                        self.guild.id
                    )
                elif selected == "auto_reactions":
                    (
                        cache.add_reaction_guilds
                        if value
                        else cache.remove_reaction_guilds
                    )(self.guild.id)
                self._render()
                await interaction.response.edit_message(
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

            button.callback = callback
            buttons.append(button)

        controls = [discord.ui.ActionRow(*buttons)]
        for kind, label in (
            ("auto_download", "Set auto-download channel"),
            ("pinboard", "Set pinboard channel"),
            ("honeypot", "Set honeypot channel"),
        ):
            select = discord.ui.ChannelSelect(
                placeholder=label,
                min_values=1,
                max_values=1,
                channel_types=[discord.ChannelType.text],
            )

            async def select_callback(
                interaction: discord.Interaction,
                selected_kind: str = kind,
                channel_select: discord.ui.ChannelSelect = select,
            ) -> None:
                channel = channel_select.values[0]
                if not isinstance(channel, discord.TextChannel):
                    await interaction.response.send_message(
                        "Please choose a text channel.", ephemeral=True
                    )
                    return
                await self._set_channel(selected_kind, channel.id)
                self._render()
                await interaction.response.edit_message(
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

            select.callback = select_callback
            controls.append(discord.ui.ActionRow(select))

        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(self.content),
                *controls,
                accent_color=self.ctx.bot.embedcolor,
            )
        )

    async def _set_channel(self, kind: str, channel_id: int) -> None:
        guild_id = self.guild.id
        if kind in {"auto_download", "pinboard"}:
            old = self.values.get(kind)
            await self.ctx.bot.pool.execute(
                f"""
                INSERT INTO guild_settings (guild_id, {kind}) VALUES ($1, $2)
                ON CONFLICT (guild_id) DO UPDATE SET {kind} = EXCLUDED.{kind}
                """,
                guild_id,
                channel_id,
            )
            if kind == "auto_download":
                if isinstance(old, int):
                    self.ctx.bot.db_cache.remove_adl(old)
                self.ctx.bot.db_cache.add_adl(channel_id)
            else:
                self.ctx.bot.db_cache.add_pinboard(guild_id, channel_id)
        else:
            await self.ctx.bot.pool.execute(
                """
                INSERT INTO honeypot_channels (guild_id, channel_id) VALUES ($1, $2)
                ON CONFLICT (guild_id) DO UPDATE SET channel_id = EXCLUDED.channel_id
                """,
                guild_id,
                channel_id,
            )
            self.ctx.bot.cached_honeypots[guild_id] = channel_id
        self.values[kind] = channel_id


# ---------------------------------------------------------------------------
# The server-settings panel below supersedes the original selector-only view.
# Keep the old classes above for compatibility with older extensions, but use
# this implementation for ``settings server`` and ``settings edit``.  It keeps
# all channel controls behind buttons so a stale/deleted channel can always be
# cleared without attempting to resolve it first.

_SERVER_CHANNEL_LABELS = {
    "auto_upload": "auto-upload",
    "hourly_posts": "hourly-posts",
    "auto_download": "auto-download",
    "poketwo": "Pokétwo",
    "auto_reactions": "auto-reactions",
    "pinboard": "pinboard",
    "honeypot": "honeypot",
}
_SERVER_CHANNEL_VALUE_KEYS = {
    "poketwo": "poketwo_channel",
    "auto_reactions": "auto_reactions_channel",
}
_SERVER_MEDIA_LABELS = {"images": "Images", "gifs": "GIFs", "videos": "Videos"}
HOURLY_POST_INTERVALS = (
    (10, "Every 10 minutes"),
    (15, "Every 15 minutes"),
    (30, "Every 30 minutes"),
    (60, "Every hour"),
    (360, "Every 6 hours"),
    (720, "Every 12 hours"),
    (1_440, "Once a day"),
    (10_080, "Once a week"),
    (43_200, "Once a month"),
)
_HOURLY_POST_INTERVAL_LABELS = dict(HOURLY_POST_INTERVALS)
_SERVER_CHANNEL_ID_RE = re.compile(r"(?:<#)?(?P<id>\d{15,25})>?")
_DEFAULT_HONEYPOT_MESSAGE = (
    "This channel was made to catch people who spam in every channel, if you type "
    "here there will be no coming back."
)


def _media_set(value: object) -> set[str]:
    if not isinstance(value, (set, frozenset, list, tuple)):
        return set()
    return {item for item in value if isinstance(item, str)}


def _hourly_interval(value: object) -> int:
    return (
        value
        if isinstance(value, int) and value in _HOURLY_POST_INTERVAL_LABELS
        else 60
    )


async def _server_text_channel(ctx: Context, value: str) -> discord.TextChannel:
    """Resolve a text-channel name, ID, or mention inside ``ctx.guild``."""
    guild = ctx.guild
    if guild is None:
        raise commands.BadArgument(
            "That text channel could not be found in this server."
        )
    raw = value.strip()
    match = _SERVER_CHANNEL_ID_RE.fullmatch(raw)
    channel: discord.abc.GuildChannel | None = None
    if match:
        channel = guild.get_channel(int(match.group("id")))
        if channel is None:
            try:
                fetched = await ctx.bot.fetch_channel(int(match.group("id")))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                fetched = None
            if isinstance(fetched, discord.abc.GuildChannel):
                channel = fetched
    else:
        channel = next(
            (
                item
                for item in guild.text_channels
                if item.name.casefold() == raw.removeprefix("#").casefold()
            ),
            None,
        )
    if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild.id:
        raise commands.BadArgument(
            "That text channel could not be found in this server."
        )
    return channel


class _ServerChannelModal(discord.ui.Modal, title="Server channel"):
    channel_input = discord.ui.TextInput(
        label="Channel name, ID, or mention",
        placeholder="#media, media, or 123456789012345678",
        required=True,
        max_length=100,
    )

    def __init__(self, picker: "_ServerChannelPicker") -> None:
        super().__init__()
        self.picker = picker

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _authorize_server_interaction(
            interaction, self.picker.ctx, self.picker.parent_view.guild
        ):
            return
        try:
            channel = await _server_text_channel(
                self.picker.ctx, str(self.channel_input.value)
            )
            await self.picker.set_channel(channel)
        except (commands.BadArgument, discord.HTTPException) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        # ``settings edit`` sends the picker itself, while ``settings server``
        # keeps it as a follow-up to the main panel. Refresh either message so
        # a newly configured honeypot immediately exposes its editor button.
        self.picker._render()
        if self.picker.message is not None:
            try:
                await self.picker.message.edit(
                    view=self.picker,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        if self.picker.kind in {"auto_upload", "hourly_posts"}:
            await interaction.response.send_message(
                view=_ServerMediaView(
                    self.picker.parent_view,
                    self.picker.kind,
                    notice=(
                        f"{channel.mention} is now used for "
                        f"{_SERVER_CHANNEL_LABELS[self.picker.kind]}."
                    ),
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        elif self.picker.kind == "auto_reactions":
            await interaction.response.send_message(
                view=_AutoReactionChannelsView(self.picker.parent_view),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        elif self.picker.kind == "honeypot":
            await interaction.response.send_message(
                view=_ServerChannelPicker(self.picker.parent_view, "honeypot"),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await interaction.response.send_message(
                f"{channel.mention} is now used for "
                f"{_SERVER_CHANNEL_LABELS[self.picker.kind]}.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )


class _ServerChannelPicker(AuthorLayoutView):
    """Button-first destination picker used by the server settings panel."""

    def __init__(self, parent: "_ServerSettingsView", kind: str) -> None:
        super().__init__(parent.ctx, timeout=300)
        self.parent_view = parent
        self.ctx = parent.ctx
        self.kind = kind
        self._render()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _authorize_server_interaction(
            interaction, self.ctx, self.parent_view.guild
        )

    def _render(self) -> None:
        self.clear_items()
        label = _SERVER_CHANNEL_LABELS[self.kind]
        text_button = discord.ui.Button(
            label="Enter channel", style=discord.ButtonStyle.secondary
        )
        text_button.callback = self._open_modal
        picker_button = discord.ui.Button(
            label="Choose channels", style=discord.ButtonStyle.secondary
        )
        picker_button.callback = self._open_picker
        current_button = discord.ui.Button(
            label="Set current channel", style=discord.ButtonStyle.secondary
        )
        current_button.callback = self._set_current
        create_button = discord.ui.Button(
            label="Create channel", style=discord.ButtonStyle.secondary
        )
        create_button.callback = self._create_channel
        back_button = discord.ui.Button(
            label="Back", style=discord.ButtonStyle.secondary
        )
        back_button.callback = self._back
        quit_button = discord.ui.Button(
            label="Quit", style=discord.ButtonStyle.secondary
        )
        quit_button.callback = self._quit
        disable_button = discord.ui.Button(
            label=f"Disable {label}", style=discord.ButtonStyle.danger
        )
        disable_button.callback = self._disable
        controls: list[discord.ui.Item[Any]] = [
            discord.ui.ActionRow(
                current_button,
                create_button,
                back_button,
                quit_button,
                disable_button,
            )
        ]
        if self.kind == "honeypot" and self.parent_view.values.get("honeypot"):
            edit_message = discord.ui.Button(
                label="Edit message", style=discord.ButtonStyle.secondary
            )
            edit_message.callback = self._edit_honeypot_message
            controls.append(discord.ui.ActionRow(edit_message))

        description = (
            "Hourly posts sends approved library media to this channel. Choose the "
            "channel first, then select the media types, repeat interval, and "
            "blocked users."
            if self.kind == "hourly_posts"
            else (
                "Messages sent in this channel trigger honeypot protection. You can "
                "edit the warning shown there after choosing a channel."
                if self.kind == "honeypot"
                else (
                    "Choose one or more channels for automatic reactions, or use "
                    "the server-wide option."
                    if self.kind == "auto_reactions"
                    else "Enter a channel name, ID, or mention, choose from the channel "
                    "picker, or use this command's channel."
                )
            )
        )
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(f"## Configure {label}"),
                discord.ui.TextDisplay(description),
                discord.ui.ActionRow(text_button, picker_button),
                *controls,
                accent_color=self.ctx.bot.embedcolor,
            )
        )

    async def _open_modal(self, interaction: discord.Interaction) -> None:
        if self.kind == "auto_reactions":
            await interaction.response.send_modal(
                _AutoReactionChannelsModal(self.parent_view)
            )
        else:
            await interaction.response.send_modal(_ServerChannelModal(self))

    async def _open_picker(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            view=_ServerChannelPages(self),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _set_current(self, interaction: discord.Interaction) -> None:
        if not isinstance(self.ctx.channel, discord.TextChannel):
            await interaction.response.send_message(
                "The command must be run in a text channel to use this option.",
                ephemeral=True,
            )
            return
        await self.set_channel(self.ctx.channel)
        target: discord.ui.LayoutView = self._after_channel_view()
        await interaction.response.edit_message(
            view=target, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _create_channel(self, interaction: discord.Interaction) -> None:
        guild = self.parent_view.guild
        try:
            channel = await guild.create_text_channel(
                name=_SERVER_CHANNEL_LABELS[self.kind]
            )
        except discord.HTTPException:
            await interaction.response.send_message(
                "I could not create a channel. Please choose an existing channel or "
                "grant me Manage Channels.",
                ephemeral=True,
            )
            return
        await self.set_channel(channel)
        target = self._after_channel_view()
        await interaction.response.edit_message(
            view=target, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _back(self, interaction: discord.Interaction) -> None:
        self.parent_view._render()
        await interaction.response.edit_message(
            view=self.parent_view, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _quit(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(view=None)

    async def _disable(self, interaction: discord.Interaction) -> None:
        await self.parent_view._clear_channel(self.kind)
        self.parent_view._render()
        self.stop()
        await interaction.response.edit_message(
            view=self.parent_view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _edit_honeypot_message(self, interaction: discord.Interaction) -> None:
        current = self.parent_view.values.get("honeypot_message")
        modal = _HoneypotMessageModal(
            self,
            str(current) if isinstance(current, str) else _DEFAULT_HONEYPOT_MESSAGE,
        )
        await interaction.response.send_modal(modal)

    async def set_channel(self, channel: discord.TextChannel) -> None:
        if self.kind == "auto_reactions":
            await self.parent_view._set_auto_reaction_channels({channel.id})
        else:
            await self.parent_view._set_channel(self.kind, channel.id)
        self.parent_view._render()
        if self.parent_view.message is not None:
            try:
                await self.parent_view.message.edit(
                    view=self.parent_view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

    def _after_channel_view(self) -> discord.ui.LayoutView:
        if self.kind in {"auto_upload", "hourly_posts"}:
            return _ServerMediaView(self.parent_view, self.kind)
        if self.kind == "auto_reactions":
            return _AutoReactionChannelsView(self.parent_view)
        return self.parent_view


class _ServerChannelPages(AuthorLayoutView):
    """Three dropdowns per page, with 75 text channels available per page."""

    def __init__(self, picker: _ServerChannelPicker) -> None:
        super().__init__(picker.ctx, timeout=300)
        self.picker = picker
        self.ctx = picker.ctx
        self.channels = sorted(
            picker.parent_view.guild.text_channels, key=lambda c: c.position
        )
        self.page = 0
        self.page_count = max(1, (len(self.channels) + 74) // 75)
        self._render()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _authorize_server_interaction(
            interaction, self.ctx, self.picker.parent_view.guild
        )

    def _render(self) -> None:
        self.clear_items()
        page_channels = self.channels[self.page * 75 : (self.page + 1) * 75]
        rows: list[discord.ui.Item[Any]] = []
        for offset in range(0, len(page_channels), 25):
            options = [
                discord.SelectOption(
                    label=channel.name[:100],
                    value=str(channel.id),
                    description=f"Position {channel.position}",
                )
                for channel in page_channels[offset : offset + 25]
            ]
            if not options:
                continue
            select = discord.ui.Select(
                placeholder="Choose a text channel",
                min_values=1,
                max_values=(
                    min(25, len(options)) if self.picker.kind == "auto_reactions" else 1
                ),
                options=options,
            )
            select.callback = self._select_channel  # type: ignore[assignment]
            rows.append(discord.ui.ActionRow(select))
        previous = discord.ui.Button(label="<", style=discord.ButtonStyle.secondary)
        previous.callback = self._previous
        next_button = discord.ui.Button(label=">", style=discord.ButtonStyle.secondary)
        next_button.callback = self._next
        current = discord.ui.Button(
            label="Set current channel", style=discord.ButtonStyle.secondary
        )
        current.callback = self._set_current
        back = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary)
        back.callback = self._back
        quit_button = discord.ui.Button(
            label="Quit", style=discord.ButtonStyle.secondary
        )
        quit_button.callback = self._quit
        rows.append(
            discord.ui.ActionRow(previous, next_button, current, back, quit_button)
        )
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    f"## Choose {_SERVER_CHANNEL_LABELS[self.picker.kind]} channel\n"
                    f"Page {self.page + 1}/{self.page_count} · {len(self.channels)} channels"
                ),
                *rows,
                accent_color=self.ctx.bot.embedcolor,
            )
        )

    async def _select_channel(self, interaction: discord.Interaction) -> None:
        data = interaction.data or {}
        selected = data.get("values", [])
        if not selected:
            await interaction.response.send_message(
                "Please choose a channel.", ephemeral=True
            )
            return
        if self.picker.kind == "auto_reactions":
            selected_ids = {int(value) for value in selected if str(value).isdigit()}
            if not selected_ids:
                await interaction.response.send_message(
                    "Please choose one or more text channels.", ephemeral=True
                )
                return
            await self.picker.parent_view._set_auto_reaction_channels(selected_ids)
            await interaction.response.edit_message(
                view=_AutoReactionChannelsView(self.picker.parent_view),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        channel = self.picker.parent_view.guild.get_channel(int(selected[0]))
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "Please choose a text channel.", ephemeral=True
            )
            return
        await self.picker.set_channel(channel)
        await interaction.response.edit_message(
            view=(
                _ServerMediaView(self.picker.parent_view, self.picker.kind)
                if self.picker.kind in {"auto_upload", "hourly_posts"}
                else (
                    _AutoReactionChannelsView(self.picker.parent_view)
                    if self.picker.kind == "auto_reactions"
                    else self.picker.parent_view
                )
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _previous(self, interaction: discord.Interaction) -> None:
        self.page = (self.page - 1) % self.page_count
        self._render()
        await interaction.response.edit_message(
            view=self, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _next(self, interaction: discord.Interaction) -> None:
        self.page = (self.page + 1) % self.page_count
        self._render()
        await interaction.response.edit_message(
            view=self, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _set_current(self, interaction: discord.Interaction) -> None:
        if not isinstance(self.ctx.channel, discord.TextChannel):
            await interaction.response.send_message(
                "The command must be run in a text channel to use this option.",
                ephemeral=True,
            )
            return
        await self.picker.set_channel(self.ctx.channel)
        await interaction.response.edit_message(
            view=(
                _ServerMediaView(self.picker.parent_view, self.picker.kind)
                if self.picker.kind in {"auto_upload", "hourly_posts"}
                else self.picker.parent_view
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _back(self, interaction: discord.Interaction) -> None:
        self.picker._render()
        await interaction.response.edit_message(
            view=self.picker, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _quit(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(view=None)


class _HourlyPostBlocksModal(discord.ui.Modal, title="Hourly-post blocked users"):
    users = discord.ui.TextInput(
        label="User IDs or mentions",
        placeholder="123..., <@456...>, 789... (blank clears the list)",
        required=False,
        max_length=2_000,
        style=discord.TextStyle.paragraph,
    )

    def __init__(self, parent: "_ServerSettingsView") -> None:
        super().__init__()
        self.parent_view = parent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _authorize_server_interaction(
            interaction, self.parent_view.ctx, self.parent_view.guild
        ):
            return
        values = re.findall(r"\d{15,25}", str(self.users.value))
        user_ids = {int(value) for value in values}
        guild_id = self.parent_view.guild.id
        await self.parent_view.ctx.bot.pool.execute(
            "DELETE FROM guild_hourly_post_blocks WHERE guild_id = $1", guild_id
        )
        if user_ids:
            await self.parent_view.ctx.bot.pool.executemany(
                "INSERT INTO guild_hourly_post_blocks (guild_id, user_id, blocked_by) "
                "VALUES ($1, $2, $3) ON CONFLICT (guild_id, user_id) DO UPDATE "
                "SET blocked_by = EXCLUDED.blocked_by",
                [(guild_id, user_id, interaction.user.id) for user_id in user_ids],
            )
        self.parent_view.ctx.bot.db_cache.hourly_post_blocks[guild_id] = user_ids
        await interaction.response.send_message(
            f"Hourly posts will exclude {len(user_ids)} user(s).",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class _HoneypotMessageModal(discord.ui.Modal, title="Honeypot message"):
    message_input = discord.ui.TextInput(
        label="Message shown in the honeypot channel",
        placeholder="Enter the warning message.",
        required=True,
        # The warning is stored in an embed field, which Discord limits to 1024
        # characters even though a modal text input can accept more.
        max_length=1_024,
        style=discord.TextStyle.paragraph,
    )

    def __init__(self, picker: "_ServerChannelPicker", current: str) -> None:
        super().__init__()
        self.picker = picker
        self.message_input.default = current[:1_024]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _authorize_server_interaction(
            interaction, self.picker.ctx, self.picker.parent_view.guild
        ):
            return
        message = str(self.message_input.value).strip()
        if not message:
            await interaction.response.send_message(
                "The honeypot message cannot be empty.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        updated = await self.picker.parent_view._set_honeypot_message(message)
        self.picker.parent_view._render()
        try:
            if self.picker.message is not None:
                await self.picker.message.edit(
                    view=self.picker,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
        status = (
            "The honeypot message was updated."
            if updated
            else "The honeypot message was saved, but the existing warning could not be found."
        )
        await interaction.response.send_message(
            status,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class _AutoReactionChannelsModal(discord.ui.Modal, title="Auto-reaction channels"):
    channels = discord.ui.TextInput(
        label="Channel names, IDs, or mentions",
        placeholder="#media, #clips, or 123456789012345678",
        required=True,
        max_length=4_000,
        style=discord.TextStyle.paragraph,
    )

    def __init__(self, parent: "_ServerSettingsView") -> None:
        super().__init__()
        self.parent_view = parent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _authorize_server_interaction(
            interaction, self.parent_view.ctx, self.parent_view.guild
        ):
            return
        values = [
            value.strip()
            for value in re.split(r"[,\n\s]+", str(self.channels.value))
            if value.strip()
        ]
        channels: set[int] = set()
        invalid: list[str] = []
        for value in values:
            try:
                channel = await _server_text_channel(self.parent_view.ctx, value)
            except commands.BadArgument:
                invalid.append(value)
                continue
            channels.add(channel.id)

        if invalid or not channels:
            detail = f" Invalid: {', '.join(invalid[:5])}" if invalid else ""
            await interaction.response.send_message(
                "Provide one or more text channels." + detail,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await self.parent_view._set_auto_reaction_channels(channels)
        view = _AutoReactionChannelsView(self.parent_view)
        await interaction.response.send_message(
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class _AutoReactionChannelsView(AuthorLayoutView):
    """Configure server-wide or channel-scoped automatic reactions."""

    def __init__(self, parent: "_ServerSettingsView") -> None:
        super().__init__(parent.ctx, timeout=300)
        self.parent_view = parent
        self.ctx = parent.ctx
        self._render()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _authorize_server_interaction(
            interaction, self.ctx, self.parent_view.guild
        )

    def _render(self) -> None:
        self.clear_items()
        selected = self.parent_view.values.get("auto_reaction_channels", set())
        selected_ids = (
            {int(value) for value in selected}
            if isinstance(selected, (set, frozenset, list, tuple))
            else set()
        )
        channel_text = (
            "Server-wide"
            if not selected_ids
            else " ".join(f"<#{channel_id}>" for channel_id in sorted(selected_ids))
        )

        enter = discord.ui.Button(
            label="Enter channels", style=discord.ButtonStyle.secondary
        )
        enter.callback = self._enter
        choose = discord.ui.Button(
            label="Choose channels", style=discord.ButtonStyle.secondary
        )
        choose.callback = self._choose
        server_wide = discord.ui.Button(
            label="Use server-wide", style=discord.ButtonStyle.secondary
        )
        server_wide.callback = self._server_wide
        current = discord.ui.Button(
            label="Set current channel", style=discord.ButtonStyle.secondary
        )
        current.callback = self._current
        back = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary)
        back.callback = self._back
        disable = discord.ui.Button(
            label="Disable auto reactions", style=discord.ButtonStyle.danger
        )
        disable.callback = self._disable

        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("## Configure auto-reactions"),
                discord.ui.TextDisplay(
                    "Choose one or more text channels, or use auto-reactions "
                    f"server-wide.\nCurrent scope: {channel_text}"
                ),
                discord.ui.ActionRow(enter, choose),
                discord.ui.ActionRow(server_wide, current),
                discord.ui.ActionRow(back, disable),
                accent_color=self.ctx.bot.embedcolor,
            )
        )

    async def _enter(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            _AutoReactionChannelsModal(self.parent_view)
        )

    async def _choose(self, interaction: discord.Interaction) -> None:
        picker = _ServerChannelPicker(self.parent_view, "auto_reactions")
        await interaction.response.edit_message(
            view=_ServerChannelPages(picker),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _server_wide(self, interaction: discord.Interaction) -> None:
        await self.parent_view._set_auto_reaction_channels(None)
        self._render()
        await interaction.response.edit_message(
            view=self, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _current(self, interaction: discord.Interaction) -> None:
        if not isinstance(self.ctx.channel, discord.TextChannel):
            await interaction.response.send_message(
                "The command must be run in a text channel to use this option.",
                ephemeral=True,
            )
            return
        await self.parent_view._set_auto_reaction_channels({self.ctx.channel.id})
        self._render()
        await interaction.response.edit_message(
            view=self, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _back(self, interaction: discord.Interaction) -> None:
        self.parent_view._render()
        await interaction.response.edit_message(
            view=self.parent_view, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _disable(self, interaction: discord.Interaction) -> None:
        await self.parent_view._clear_channel("auto_reactions")
        self.parent_view._render()
        self.stop()
        await interaction.response.edit_message(
            view=self.parent_view, allowed_mentions=discord.AllowedMentions.none()
        )


class _ServerMediaView(AuthorLayoutView):
    """Select images, GIFs, and videos for an automatic destination."""

    def __init__(
        self,
        parent: "_ServerSettingsView",
        kind: str,
        *,
        notice: str | None = None,
    ) -> None:
        super().__init__(parent.ctx, timeout=300)
        self.parent_view = parent
        self.kind = kind
        self.notice = notice
        self._render()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _authorize_server_interaction(
            interaction, self.parent_view.ctx, self.parent_view.guild
        )

    def _render(self) -> None:
        self.clear_items()
        values = _media_set(self.parent_view.values.get(f"{self.kind}_media"))
        buttons: list[discord.ui.Button] = []
        for media, label in _SERVER_MEDIA_LABELS.items():
            button = discord.ui.Button(
                label=f"{label}: {'On' if media in values else 'Off'}",
                style=discord.ButtonStyle.secondary,
            )

            async def toggle(
                interaction: discord.Interaction, selected: str = media
            ) -> None:
                await self.parent_view._set_media(
                    self.kind, selected, selected not in values
                )
                self._render()
                await interaction.response.edit_message(
                    view=self, allowed_mentions=discord.AllowedMentions.none()
                )

            button.callback = toggle
            buttons.append(button)
        done = discord.ui.Button(label="Done", style=discord.ButtonStyle.secondary)
        done.callback = self._done
        change_channel = discord.ui.Button(
            label="Change channel", style=discord.ButtonStyle.secondary
        )
        change_channel.callback = self._change_channel
        disable = discord.ui.Button(
            label=f"Disable {_SERVER_CHANNEL_LABELS[self.kind]}",
            style=discord.ButtonStyle.danger,
        )
        disable.callback = self._disable
        block_button: discord.ui.Button | None = None
        if self.kind == "hourly_posts":
            block_button = discord.ui.Button(
                label="Blocked users", style=discord.ButtonStyle.secondary
            )
            block_button.callback = self._blocked_users
        actions = [done, change_channel, disable]
        if block_button is not None:
            actions.append(block_button)

        content: list[discord.ui.Item[Any]] = [
            discord.ui.TextDisplay(f"## {_SERVER_CHANNEL_LABELS[self.kind]} media"),
            *([discord.ui.TextDisplay(self.notice)] if self.notice else []),
            discord.ui.TextDisplay(
                "Choose which media types may be sent automatically."
            ),
            discord.ui.ActionRow(*buttons),
        ]
        if self.kind == "hourly_posts":
            interval = _hourly_interval(
                self.parent_view.values.get("hourly_posts_interval")
            )
            interval_select = discord.ui.Select(
                placeholder=(
                    f"Repeat: {_HOURLY_POST_INTERVAL_LABELS.get(interval, 'Every hour')}"
                ),
                min_values=1,
                max_values=1,
                options=[
                    discord.SelectOption(
                        label=label,
                        value=str(minutes),
                        default=minutes == interval,
                    )
                    for minutes, label in HOURLY_POST_INTERVALS
                ],
            )

            async def set_interval(interaction: discord.Interaction) -> None:
                await self.parent_view._set_hourly_interval(
                    int(interval_select.values[0])
                )
                self._render()
                await interaction.response.edit_message(
                    view=self, allowed_mentions=discord.AllowedMentions.none()
                )

            interval_select.callback = set_interval
            content.extend(
                [
                    discord.ui.TextDisplay(
                        f"Repeat: **{_HOURLY_POST_INTERVAL_LABELS.get(interval, 'Every hour')}**"
                    ),
                    discord.ui.ActionRow(interval_select),
                ]
            )
        content.append(discord.ui.ActionRow(*actions))
        self.add_item(
            discord.ui.Container(
                *content,
                accent_color=self.ctx.bot.embedcolor,
            )
        )

    async def _done(self, interaction: discord.Interaction) -> None:
        self.parent_view._render()
        await interaction.response.edit_message(
            view=self.parent_view, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _change_channel(self, interaction: discord.Interaction) -> None:
        await self.parent_view._open_channel_picker(interaction, self.kind)

    async def _disable(self, interaction: discord.Interaction) -> None:
        await self.parent_view._clear_channel(self.kind)
        self.parent_view._render()
        await interaction.response.edit_message(
            view=self.parent_view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _blocked_users(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(_HourlyPostBlocksModal(self.parent_view))


class _ServerSettingsView(AuthorLayoutView):
    """Button-based server settings panel and channel destination manager."""

    CHANNEL_KINDS = tuple(_SERVER_CHANNEL_LABELS)

    def __init__(self, ctx: GuildContext, values: dict[str, object]) -> None:
        super().__init__(ctx, timeout=300)
        if ctx.guild is None:
            raise ValueError("Server settings can only be used in a guild.")
        self.guild: discord.Guild = ctx.guild
        self.values = values
        self._active_channel_view: AuthorLayoutView | None = None
        self._render()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _authorize_server_interaction(interaction, self.ctx, self.guild)

    @classmethod
    async def create(cls, ctx: GuildContext) -> "_ServerSettingsView":
        row = await ctx.bot.pool.fetchrow(
            "SELECT tracking_enabled, history_public, auto_download, auto_upload, "
            "auto_upload_images, auto_upload_gifs, auto_upload_videos, poketwo, "
            "poketwo_channel, auto_reactions, auto_reactions_channel, pinboard "
            "FROM guild_settings WHERE guild_id = $1",
            ctx.guild.id,
        )
        hourly = await ctx.bot.pool.fetchrow(
            "SELECT channel_id, images, gifs, videos, interval_minutes, next_post_at "
            "FROM guild_hourly_posts WHERE guild_id = $1",
            ctx.guild.id,
        )
        try:
            reaction_targets = await ctx.bot.pool.fetch(
                "SELECT channel_id FROM guild_auto_reaction_channels WHERE guild_id = $1",
                ctx.guild.id,
            )
        except Exception:
            reaction_targets = []
        auto_reaction_channels = {
            int(target["channel_id"])
            for target in reaction_targets
            if target.get("channel_id")
        }
        legacy_reaction_channel = row.get("auto_reactions_channel") if row else None
        if not auto_reaction_channels and isinstance(legacy_reaction_channel, int):
            auto_reaction_channels.add(legacy_reaction_channel)
        honeypot_row = await ctx.bot.pool.fetchrow(
            "SELECT channel_id, message_template FROM honeypot_channels "
            "WHERE guild_id = $1",
            ctx.guild.id,
        )
        values: dict[str, object] = {
            "tracking_enabled": bool(row["tracking_enabled"]) if row else True,
            "history_public": bool(row["history_public"]) if row else True,
            "auto_download": row["auto_download"] if row else None,
            "auto_upload": row.get("auto_upload") if row else None,
            "auto_upload_media": {
                key
                for key, enabled in (
                    ("images", row.get("auto_upload_images", True) if row else True),
                    ("gifs", row.get("auto_upload_gifs", True) if row else True),
                    ("videos", row.get("auto_upload_videos", True) if row else True),
                )
                if enabled
            },
            "hourly_posts": hourly["channel_id"] if hourly else None,
            "hourly_posts_media": {
                key
                for key, enabled in (
                    ("images", hourly["images"] if hourly else True),
                    ("gifs", hourly["gifs"] if hourly else True),
                    ("videos", hourly["videos"] if hourly else True),
                )
                if enabled
            },
            "hourly_posts_interval": (
                int(hourly.get("interval_minutes") or 60) if hourly else 60
            ),
            "poketwo": bool(row["poketwo"]) if row else False,
            "poketwo_channel": row.get("poketwo_channel") if row else None,
            "auto_reactions": bool(row["auto_reactions"]) if row else False,
            "auto_reactions_channel": (
                row.get("auto_reactions_channel") if row else None
            ),
            "auto_reaction_channels": auto_reaction_channels,
            "pinboard": row["pinboard"] if row else None,
            "honeypot": honeypot_row["channel_id"] if honeypot_row else None,
            "honeypot_message": (
                honeypot_row.get("message_template")
                if honeypot_row
                else _DEFAULT_HONEYPOT_MESSAGE
            ),
        }
        view = cls(ctx, values)
        await view._cleanup_stale_channels()
        return view

    @staticmethod
    def _channel_mention(channel_id: object) -> str:
        return f"<#{channel_id}>" if channel_id else "Disabled"

    @property
    def content(self) -> str:
        return (
            f"## Server settings · {self.guild.name}\n"
            "Enable/Disable server settings with the buttons below."
        )

    def _render(self) -> None:
        self.clear_items()
        rows: list[list[discord.ui.Button]] = []

        def toggle_button(
            setting: str, enabled_label: str, disabled_label: str
        ) -> discord.ui.Button:
            button = discord.ui.Button(
                label=enabled_label if self.values[setting] else disabled_label,
                style=(
                    discord.ButtonStyle.danger
                    if self.values[setting]
                    else discord.ButtonStyle.secondary
                ),
            )

            async def callback(
                interaction: discord.Interaction, selected: str = setting
            ) -> None:
                await self._set_bool(selected, not bool(self.values[selected]))
                self._render()
                await interaction.response.edit_message(
                    view=self, allowed_mentions=discord.AllowedMentions.none()
                )

            button.callback = callback
            return button

        def channel_button(kind: str) -> discord.ui.Button:
            value_key = _SERVER_CHANNEL_VALUE_KEYS.get(kind, kind)
            value = self.values.get(value_key)
            if kind == "auto_reactions":
                value = bool(self.values.get("auto_reactions"))
            label = _SERVER_CHANNEL_LABELS[kind]
            button = discord.ui.Button(
                label=f"Disable {label}" if value else f"Set {label}",
                style=(
                    discord.ButtonStyle.danger
                    if value
                    else discord.ButtonStyle.secondary
                ),
            )

            async def callback(
                interaction: discord.Interaction, selected: str = kind
            ) -> None:
                selected_key = _SERVER_CHANNEL_VALUE_KEYS.get(selected, selected)
                if self.values.get(selected_key):
                    if selected == "hourly_posts":
                        await self._open_media_editor(interaction, selected)
                        return
                    if selected == "honeypot":
                        await self._open_channel_picker(interaction, selected)
                        return
                    if selected == "auto_reactions":
                        await self._open_auto_reaction_editor(interaction)
                        return
                    await self._clear_channel(selected)
                    self._render()
                    await interaction.response.edit_message(
                        view=self, allowed_mentions=discord.AllowedMentions.none()
                    )
                    return
                await self._open_channel_picker(interaction, selected)

            button.callback = callback
            return button

        rows.append(
            [
                toggle_button(
                    "tracking_enabled", "Disable tracking", "Enable tracking"
                ),
                toggle_button(
                    "history_public", "Make history private", "Make history public"
                ),
            ]
        )
        reaction_controls = [
            toggle_button(
                "auto_reactions", "Disable auto reactions", "Enable auto reactions"
            )
        ]
        if self.values.get("auto_reactions"):
            edit_reactions = discord.ui.Button(
                label="Edit auto-reactions", style=discord.ButtonStyle.secondary
            )
            edit_reactions.callback = self._open_auto_reaction_editor
            reaction_controls.append(edit_reactions)

        sections: list[tuple[str, list[list[discord.ui.Button]]]] = [
            ("### Server tracking/history", [rows[0]]),
            (
                "### Server uploads",
                [[channel_button("auto_upload"), channel_button("hourly_posts")]],
            ),
            ("### Auto reactions", [reaction_controls]),
            ("### Honeypot", [[channel_button("honeypot")]]),
            ("### Auto-downloads", [[channel_button("auto_download")]]),
            ("### Pinboard", [[channel_button("pinboard")]]),
            (
                "### Pokétwo",
                [[toggle_button("poketwo", "Disable Pokétwo", "Enable Pokétwo")]],
            ),
        ]

        content: list[discord.ui.Item[Any]] = [discord.ui.TextDisplay(self.content)]
        for heading, section_rows in sections:
            content.append(discord.ui.Separator())
            content.append(discord.ui.TextDisplay(heading))
            content.extend(discord.ui.ActionRow(*row) for row in section_rows)
        self.add_item(
            discord.ui.Container(
                *content,
                accent_color=self.ctx.bot.embedcolor,
            )
        )

    async def _cleanup_stale_channels(self) -> None:
        for kind in self.CHANNEL_KINDS:
            if kind == "auto_reactions":
                continue
            value_key = _SERVER_CHANNEL_VALUE_KEYS.get(kind, kind)
            channel_id = self.values.get(value_key)
            if not isinstance(channel_id, int) or not channel_id:
                continue
            channel = self.guild.get_channel(channel_id)
            if channel is None:
                try:
                    fetched = await self.ctx.bot.fetch_channel(channel_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    fetched = None
                channel = fetched
            if (
                not isinstance(channel, discord.TextChannel)
                or channel.guild.id != self.guild.id
            ):
                await self._clear_channel(kind)

        targets = self.values.get("auto_reaction_channels", set())
        if isinstance(targets, (set, frozenset, list, tuple)):
            valid_targets = {
                int(channel_id)
                for channel_id in targets
                if isinstance(
                    self.guild.get_channel(int(channel_id)), discord.TextChannel
                )
            }
            if valid_targets != set(targets):
                if self.values.get("auto_reactions"):
                    await self._set_auto_reaction_channels(valid_targets or None)
                else:
                    invalid_targets = set(targets) - valid_targets
                    if invalid_targets:
                        await self.ctx.bot.pool.executemany(
                            "DELETE FROM guild_auto_reaction_channels "
                            "WHERE guild_id=$1 AND channel_id=$2",
                            [
                                (self.guild.id, int(channel_id))
                                for channel_id in invalid_targets
                            ],
                        )
                    self.values["auto_reaction_channels"] = valid_targets
                    self.ctx.bot.db_cache.set_auto_reaction_channels(
                        self.guild.id, valid_targets or None
                    )

    async def _set_bool(self, setting: str, value: bool) -> None:
        await self.ctx.bot.pool.execute(
            f"INSERT INTO guild_settings (guild_id, {setting}) VALUES ($1, $2) "
            f"ON CONFLICT (guild_id) DO UPDATE SET {setting} = EXCLUDED.{setting}",
            self.guild.id,
            value,
        )
        self.values[setting] = value
        cache = self.ctx.bot.db_cache
        if setting == "tracking_enabled":
            (
                cache.guild_tracking_disabled.discard
                if value
                else cache.guild_tracking_disabled.add
            )(self.guild.id)
        elif setting == "history_public":
            cache.set_guild_history_public(self.guild.id, value)
        elif setting == "poketwo":
            (cache.add_poketwo if value else cache.remove_poketwo)(self.guild.id)
        elif setting == "auto_reactions":
            (cache.add_reaction_guilds if value else cache.remove_reaction_guilds)(
                self.guild.id
            )

    async def _open_channel_picker(
        self, interaction: discord.Interaction, kind: str
    ) -> None:
        """Open a fresh ephemeral picker after acknowledging the button click.

        Ephemeral component views do not notify the bot when a user dismisses
        them. Stopping the previous view before creating the next one prevents
        stale picker callbacks from remaining registered after dismissal.
        Deferring first also gives Discord an acknowledgement before sending
        the Components V2 follow-up.
        """
        if self._active_channel_view is not None:
            self._active_channel_view.stop()

        picker = _ServerChannelPicker(self, kind)
        self._active_channel_view = picker
        await interaction.response.defer(ephemeral=True, thinking=True)
        picker.message = await interaction.followup.send(
            view=picker,
            ephemeral=True,
            wait=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _open_auto_reaction_editor(
        self, interaction: discord.Interaction
    ) -> None:
        if self._active_channel_view is not None:
            self._active_channel_view.stop()
        editor = _AutoReactionChannelsView(self)
        self._active_channel_view = editor
        await interaction.response.defer(ephemeral=True, thinking=True)
        editor.message = await interaction.followup.send(
            view=editor,
            ephemeral=True,
            wait=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _open_media_editor(
        self, interaction: discord.Interaction, kind: str
    ) -> None:
        if self._active_channel_view is not None:
            self._active_channel_view.stop()
        media_view = _ServerMediaView(self, kind)
        self._active_channel_view = media_view
        await interaction.response.defer(ephemeral=True, thinking=True)
        media_view.message = await interaction.followup.send(
            view=media_view,
            ephemeral=True,
            wait=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _set_channel(self, kind: str, channel_id: int) -> None:
        guild_id = self.guild.id
        value_key = _SERVER_CHANNEL_VALUE_KEYS.get(kind, kind)
        old = self.values.get(value_key)
        if kind == "hourly_posts":
            media = _media_set(self.values.get("hourly_posts_media"))
            interval = _hourly_interval(self.values.get("hourly_posts_interval"))
            await self.ctx.bot.pool.execute(
                "INSERT INTO guild_hourly_posts "
                "(guild_id, channel_id, images, gifs, videos, interval_minutes, next_post_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, now()) "
                "ON CONFLICT (guild_id) DO UPDATE SET "
                "channel_id=EXCLUDED.channel_id, images=EXCLUDED.images, "
                "gifs=EXCLUDED.gifs, videos=EXCLUDED.videos, "
                "interval_minutes=EXCLUDED.interval_minutes, next_post_at=now()",
                guild_id,
                channel_id,
                "images" in media,
                "gifs" in media,
                "videos" in media,
                interval,
            )
            self.ctx.bot.db_cache.set_hourly_posts(
                guild_id, channel_id, set(media), interval
            )
        elif kind == "honeypot":
            await self.ctx.bot.pool.execute(
                "INSERT INTO honeypot_channels (guild_id, channel_id, message_template) "
                "VALUES ($1, $2, $3) ON CONFLICT (guild_id) DO UPDATE SET "
                "channel_id=EXCLUDED.channel_id",
                guild_id,
                channel_id,
                str(self.values.get("honeypot_message") or _DEFAULT_HONEYPOT_MESSAGE),
            )
            self.ctx.bot.cached_honeypots[guild_id] = channel_id
        elif kind == "auto_upload":
            media = _media_set(self.values.get("auto_upload_media"))
            await self.ctx.bot.pool.execute(
                "INSERT INTO guild_settings (guild_id, auto_upload, auto_upload_images, auto_upload_gifs, auto_upload_videos) VALUES ($1, $2, $3, $4, $5) "
                "ON CONFLICT (guild_id) DO UPDATE SET auto_upload=EXCLUDED.auto_upload, auto_upload_images=EXCLUDED.auto_upload_images, auto_upload_gifs=EXCLUDED.auto_upload_gifs, auto_upload_videos=EXCLUDED.auto_upload_videos",
                guild_id,
                channel_id,
                "images" in media,
                "gifs" in media,
                "videos" in media,
            )
            if isinstance(old, int):
                self.ctx.bot.db_cache.remove_auto_upload(old)
            self.ctx.bot.db_cache.add_auto_upload(channel_id, set(media))
        elif kind == "auto_reactions":
            await self._set_auto_reaction_channels({channel_id})
            return
        elif kind == "poketwo":
            channel_column = _SERVER_CHANNEL_VALUE_KEYS[kind]
            enabled_column = kind
            await self.ctx.bot.pool.execute(
                f"INSERT INTO guild_settings (guild_id, {enabled_column}, {channel_column}) "
                "VALUES ($1, TRUE, $2) ON CONFLICT (guild_id) DO UPDATE SET "
                f"{enabled_column}=TRUE, {channel_column}=EXCLUDED.{channel_column}",
                guild_id,
                channel_id,
            )
            self.ctx.bot.db_cache.add_poketwo(guild_id)
            self.ctx.bot.db_cache.set_poketwo_channel(guild_id, channel_id)
            self.values[kind] = True
        else:
            await self.ctx.bot.pool.execute(
                f"INSERT INTO guild_settings (guild_id, {kind}) VALUES ($1, $2) ON CONFLICT (guild_id) DO UPDATE SET {kind}=EXCLUDED.{kind}",
                guild_id,
                channel_id,
            )
            if kind == "auto_download":
                if isinstance(old, int):
                    self.ctx.bot.db_cache.remove_adl(old)
                self.ctx.bot.db_cache.add_adl(channel_id)
            else:
                self.ctx.bot.db_cache.add_pinboard(guild_id, channel_id)
        self.values[value_key] = channel_id

    async def _set_auto_reaction_channels(self, channel_ids: set[int] | None) -> None:
        """Enable auto-reactions and replace its optional channel scope."""
        guild_id = self.guild.id
        await self.ctx.bot.pool.execute(
            "INSERT INTO guild_settings (guild_id, auto_reactions, auto_reactions_channel) "
            "VALUES ($1, TRUE, NULL) ON CONFLICT (guild_id) DO UPDATE SET "
            "auto_reactions=TRUE, auto_reactions_channel=NULL",
            guild_id,
        )
        await self.ctx.bot.pool.execute(
            "DELETE FROM guild_auto_reaction_channels WHERE guild_id=$1", guild_id
        )
        normalized = {int(channel_id) for channel_id in (channel_ids or set())}
        if normalized:
            await self.ctx.bot.pool.executemany(
                "INSERT INTO guild_auto_reaction_channels (guild_id, channel_id) "
                "VALUES ($1, $2) ON CONFLICT (guild_id, channel_id) DO NOTHING",
                [(guild_id, channel_id) for channel_id in normalized],
            )
        self.values["auto_reactions"] = True
        self.values["auto_reaction_channels"] = normalized
        self.values["auto_reactions_channel"] = next(iter(normalized), None)
        self.ctx.bot.db_cache.add_reaction_guilds(guild_id)
        self.ctx.bot.db_cache.set_auto_reaction_channels(guild_id, normalized or None)

    async def _set_honeypot_message(self, message: str) -> bool:
        """Persist and apply the warning shown in the honeypot channel."""
        await self.ctx.bot.pool.execute(
            "UPDATE honeypot_channels SET message_template=$2 WHERE guild_id=$1",
            self.guild.id,
            message,
        )
        self.values["honeypot_message"] = message
        channel_id = self.values.get("honeypot")
        if not isinstance(channel_id, int):
            return False
        channel = self.guild.get_channel(channel_id)
        if channel is None:
            try:
                fetched = await self.ctx.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                fetched = None
            channel = fetched
        if not isinstance(channel, discord.TextChannel):
            return False
        bot_user = self.ctx.bot.user
        if bot_user is None:
            return False

        async for candidate in channel.history(limit=100):
            if candidate.author.id != bot_user.id or not candidate.embeds:
                continue
            embed = candidate.embeds[0]
            field_index = next(
                (
                    index
                    for index, field in enumerate(embed.fields)
                    if field.name == "Watch your step!"
                ),
                None,
            )
            if field_index is None:
                continue
            updated = embed.copy()
            updated.set_field_at(
                field_index, name="Watch your step!", value=message, inline=False
            )
            await candidate.edit(
                embed=updated,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return True
        return False

    async def _set_media(self, kind: str, media: str, enabled: bool) -> None:
        values = _media_set(self.values.get(f"{kind}_media"))
        values.add(media) if enabled else values.discard(media)
        self.values[f"{kind}_media"] = values
        if kind == "hourly_posts":
            await self.ctx.bot.pool.execute(
                "UPDATE guild_hourly_posts SET images=$2, gifs=$3, videos=$4 WHERE guild_id=$1",
                self.guild.id,
                "images" in values,
                "gifs" in values,
                "videos" in values,
            )
            channel_value = self.values.get(kind)
            if isinstance(channel_value, int):
                self.ctx.bot.db_cache.set_hourly_posts(
                    self.guild.id,
                    channel_value,
                    values,
                    _hourly_interval(self.values.get("hourly_posts_interval")),
                    self.ctx.bot.db_cache.hourly_post_next_at.get(self.guild.id),
                )
        else:
            await self.ctx.bot.pool.execute(
                "INSERT INTO guild_settings (guild_id, auto_upload_images, auto_upload_gifs, auto_upload_videos) VALUES ($1, $2, $3, $4) "
                "ON CONFLICT (guild_id) DO UPDATE SET auto_upload_images=EXCLUDED.auto_upload_images, auto_upload_gifs=EXCLUDED.auto_upload_gifs, auto_upload_videos=EXCLUDED.auto_upload_videos",
                self.guild.id,
                "images" in values,
                "gifs" in values,
                "videos" in values,
            )
            channel_value = self.values.get(kind)
            if isinstance(channel_value, int):
                self.ctx.bot.db_cache.add_auto_upload(channel_value, values)

    async def _set_hourly_interval(self, interval_minutes: int) -> None:
        if interval_minutes not in _HOURLY_POST_INTERVAL_LABELS:
            raise commands.BadArgument("That hourly-post interval is not supported.")
        next_post_at = discord.utils.utcnow() + timedelta(minutes=interval_minutes)
        await self.ctx.bot.pool.execute(
            "UPDATE guild_hourly_posts SET interval_minutes=$2, next_post_at=$3 "
            "WHERE guild_id=$1",
            self.guild.id,
            interval_minutes,
            next_post_at,
        )
        self.values["hourly_posts_interval"] = interval_minutes
        self.ctx.bot.db_cache.hourly_post_intervals[self.guild.id] = interval_minutes
        self.ctx.bot.db_cache.set_hourly_post_next_at(self.guild.id, next_post_at)

    async def _clear_channel(self, kind: str) -> None:
        guild_id = self.guild.id
        value_key = _SERVER_CHANNEL_VALUE_KEYS.get(kind, kind)
        channel_id = self.values.get(value_key)
        if kind == "hourly_posts":
            await self.ctx.bot.pool.execute(
                "DELETE FROM guild_hourly_posts WHERE guild_id=$1", guild_id
            )
            self.ctx.bot.db_cache.set_hourly_posts(guild_id, None)
        elif kind == "honeypot":
            await self.ctx.bot.pool.execute(
                "DELETE FROM honeypot_channels WHERE guild_id=$1", guild_id
            )
            self.ctx.bot.cached_honeypots.pop(guild_id, None)
        elif kind in {"poketwo", "auto_reactions"}:
            channel_column = _SERVER_CHANNEL_VALUE_KEYS[kind]
            if kind == "auto_reactions":
                await self.ctx.bot.pool.execute(
                    "DELETE FROM guild_auto_reaction_channels WHERE guild_id=$1",
                    guild_id,
                )
            await self.ctx.bot.pool.execute(
                f"UPDATE guild_settings SET {kind}=FALSE, {channel_column}=NULL "
                "WHERE guild_id=$1",
                guild_id,
            )
            if kind == "poketwo":
                self.ctx.bot.db_cache.remove_poketwo(guild_id)
                self.ctx.bot.db_cache.set_poketwo_channel(guild_id, None)
            else:
                self.ctx.bot.db_cache.remove_reaction_guilds(guild_id)
                self.ctx.bot.db_cache.set_auto_reaction_channels(guild_id, None)
        else:
            await self.ctx.bot.pool.execute(
                f"UPDATE guild_settings SET {kind}=NULL WHERE guild_id=$1", guild_id
            )
            if kind == "auto_download" and isinstance(channel_id, int):
                self.ctx.bot.db_cache.remove_adl(channel_id)
            elif kind == "auto_upload" and isinstance(channel_id, int):
                self.ctx.bot.db_cache.remove_auto_upload(channel_id)
            elif kind == "pinboard":
                old_channel_id = channel_id if isinstance(channel_id, int) else 0
                self.ctx.bot.db_cache.remove_pinboard(guild_id, old_channel_id)
        self.values[value_key] = None
        if kind == "honeypot":
            self.values["honeypot_message"] = _DEFAULT_HONEYPOT_MESSAGE
        if kind in {"poketwo", "auto_reactions"}:
            self.values[kind] = False
        if kind == "auto_reactions":
            self.values["auto_reaction_channels"] = set()


# Keep the public import used by logging.py stable while switching to the new
# button-first implementation.
ServerSettingsView = _ServerSettingsView


class Server(Cog):
    @commands.hybrid_command(
        name="hourly-posts",
        aliases=("hourly", "hourlyposts", "autoposts"),
    )
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def hourly_posts(self, ctx: GuildContext) -> None:
        """Set up hourly library posts, media filters, and repeat interval."""
        # ``Server`` is mixed into the Settings cog, which provides the shared
        # destination helper and Components V2 editor.
        await self._edit_server_destination(ctx, "hourly_posts")  # type: ignore[attr-defined]

    @commands.group(name="prefix", invoke_without_command=True)
    @commands.guild_only()
    async def prefix(self, ctx: GuildContext):
        """Manage the server prefixes"""
        format_dt = discord.utils.format_dt

        sql = """SELECT * FROM guild_prefixes WHERE guild_id = $1 AND time IS NOT NULL ORDER BY time DESC"""
        records = await ctx.bot.pool.fetch(sql, ctx.guild.id)

        if not bool(records):
            raise commands.BadArgument("This server has no prefixes set.")

        entries = [
            (
                record["prefix"],
                f'{format_dt(record["time"], "R")}  |  {format_dt(record["time"], "d")} | <@{record["author_id"]}>',
            )
            for record in records
        ]

        menu = LayoutPager(PrefixPageSource(entries, str(ctx.guild)), ctx=ctx)
        await menu.start(ctx)

    @prefix.command(name="add", aliases=("set", "a", "+"))
    @commands.has_guild_permissions(manage_guild=True, manage_messages=True)
    @commands.guild_only()
    async def prefix_add(
        self, ctx: GuildContext, *, prefix: str = commands.param(converter=to_lower)
    ):
        """Add a prefix to the server"""
        bot = self.bot
        now = discord.utils.utcnow()
        if len(prefix) > 10:
            raise commands.BadArgument("Prefixes can only be 10 characters long.")

        try:
            if prefix in bot.db_cache.prefixes[ctx.guild.id]:
                raise commands.BadArgument("This prefix is already set.")
        except KeyError:
            pass

        sql = """INSERT INTO guild_prefixes (guild_id, prefix, author_id, time) VALUES ($1, $2, $3, $4)"""
        await bot.pool.execute(sql, ctx.guild.id, prefix, ctx.author.id, now)
        bot.db_cache.add_prefix(ctx.guild.id, prefix)
        await ctx.send(
            view=SettingsMessageView(
                ctx, f"## Prefix settings\nAdded prefix `{prefix}` to the server."
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @prefix.command(name="remove", aliases=("delete", "r", "d", "del", "-"))
    @commands.has_guild_permissions(manage_guild=True, manage_messages=True)
    @commands.guild_only()
    async def prefix_remove(
        self, ctx: GuildContext, *, prefix: str = commands.param(converter=to_lower)
    ):
        """Remove a prefix from the server"""
        bot = self.bot

        if prefix not in bot.db_cache.prefixes[ctx.guild.id]:
            raise commands.BadArgument(
                "This prefix does not exist. Check your spelling and try again."
            )

        sql = """DELETE FROM guild_prefixes WHERE guild_id = $1 AND prefix = $2"""
        await bot.pool.execute(sql, ctx.guild.id, prefix)
        bot.db_cache.remove_prefix(ctx.guild.id, prefix)
        await ctx.send(
            view=SettingsMessageView(
                ctx, f"## Prefix settings\nRemoved prefix `{prefix}` from the server."
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def add_adl_channel(self, channel: discord.TextChannel):
        sql = """
        INSERT INTO guild_settings (guild_id, auto_download) VALUES ($1, $2) 
        ON CONFLICT (guild_id) DO UPDATE
        SET auto_download = $2
        WHERE guild_settings.guild_id = $1
        """

        await self.bot.pool.execute(sql, channel.guild.id, channel.id)
        self.bot.db_cache.add_adl(channel.id)

    async def remove_adl_channel(self, channel: discord.TextChannel):
        sql = """UPDATE guild_settings SET auto_download = NULL WHERE guild_id = $1"""

        await self.bot.pool.execute(sql, channel.guild.id)
        self.bot.db_cache.remove_adl(channel.id)

    @commands.group(
        name="auto-download",
        invoke_without_command=True,
        aliases=("adl", "autodownload", "auto_download"),
    )
    @commands.has_guild_permissions(manage_guild=True)
    @commands.guild_only()
    async def auto_download(
        self, ctx: GuildContext, *, channel: Optional[discord.TextChannel] = None
    ):
        """Add a channel to auto-download videos from"""
        sql = """SELECT auto_download FROM guild_settings WHERE guild_id = $1"""
        channel_id: int = await self.bot.pool.fetchval(sql, ctx.guild.id)
        if channel_id:
            raise commands.BadArgument(
                f"An auto-download channel is already set in this server, if you would like to change or remove it please run the command `{ctx.get_prefix}auto-download remove`"
            )

        if not channel:
            return await ctx.send(
                view=DropdownView(ctx),
                allowed_mentions=discord.AllowedMentions.none(),
            )

        await self.add_adl_channel(channel)

        await ctx.send(
            view=SettingsMessageView(
                ctx,
                f"## Auto-download settings\nSet auto-download channel to {channel.mention}.",
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @auto_download.command(
        name="remove",
        aliases=("r", "delete", "d", "-"),
    )
    @commands.has_guild_permissions(manage_guild=True)
    @commands.guild_only()
    async def auto_download_remove(self, ctx: GuildContext):
        """Add a channel to auto-download videos from"""
        sql = """SELECT auto_download FROM guild_settings WHERE guild_id = $1"""
        channel_id: int = await self.bot.pool.fetchval(sql, ctx.guild.id)

        if not channel_id:
            raise commands.BadArgument(
                f"No auto-download channel found. You may set one with `{ctx.get_prefix}auto-download`"
            )

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                await self.bot.pool.execute(
                    "UPDATE guild_settings SET auto_download = NULL WHERE guild_id = $1",
                    ctx.guild.id,
                )
                self.bot.db_cache.remove_adl(channel_id)
                await ctx.send(
                    view=SettingsMessageView(
                        ctx,
                        "## Auto-download settings\nThe missing channel was removed from the settings.",
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

        if (
            not isinstance(channel, discord.TextChannel)
            or channel.guild.id != ctx.guild.id
        ):
            await self.bot.pool.execute(
                "UPDATE guild_settings SET auto_download = NULL WHERE guild_id = $1",
                ctx.guild.id,
            )
            self.bot.db_cache.remove_adl(channel_id)
            await ctx.send(
                view=SettingsMessageView(
                    ctx,
                    "## Auto-download settings\nThe unavailable channel was removed from the settings.",
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await self.remove_adl_channel(channel)
        await ctx.send(
            view=SettingsMessageView(
                ctx,
                f"## Auto-download settings\nRemoved auto-downloads from {channel.mention}.",
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(
        name="auto-solve",
        aliases=(
            "auto_solve",
            "autosolve",
        ),
    )
    @commands.has_guild_permissions(manage_guild=True)
    async def auto_solve(self, ctx: GuildContext):
        """Toggle auto solving for Pokétwo hint messages"""
        value: Optional[bool] = await self.bot.pool.fetchval(
            "SELECT poketwo FROM guild_settings WHERE guild_id = $1", ctx.guild.id
        )

        value = not value if value else True

        sql = """
        INSERT INTO guild_settings (guild_id, poketwo) VALUES ($1, $2) 
        ON CONFLICT (guild_id) DO UPDATE
        SET poketwo = $2 
        WHERE guild_settings.guild_id = $1
        """

        await self.bot.pool.execute(sql, ctx.guild.id, value)

        func = [self.bot.db_cache.remove_poketwo, self.bot.db_cache.add_poketwo]

        func[value](ctx.guild.id)

        await ctx.send(
            view=SettingsMessageView(
                ctx,
                f"## Server settings\n{['Disabled', 'Enabled'][value]} Pokétwo auto-solving for this server.",
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(
        name="auto-reactions",
        aliases=("auto_reactions", "autoreactions", "areactions"),
    )
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def auto_reactions(self, ctx: GuildContext):
        """Enable, disable, or scope automatic reactions to selected channels."""
        await self._edit_server_destination(ctx, "auto_reactions")  # type: ignore[attr-defined]
