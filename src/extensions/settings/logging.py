from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional, Union, cast

import discord
from discord import app_commands
from discord.ext import commands

from core import Cog
from utils import AuthorLayoutView, interaction_only, to_image

if TYPE_CHECKING:
    from extensions.context import Context, GuildContext


format_table = {
    "avatars": "user_id",
    "guild_avatars": "member_id",
    "username_logs": "user_id",
    "display_name_logs": "user_id",
    "stag_logs": "user_id",
    "nickname_logs": "user_id",
    "discrim_logs": "user_id",
    "member_join_logs": "member_id",
    "tags": "author_id",
    "emoji_stats": "author_id",
    "download_stats": "user_id",
}


class ReactionTrackingView(discord.ui.LayoutView):
    """Explicit opt-in control for reaction history."""

    def __init__(self, ctx: Context, enabled: bool) -> None:
        super().__init__(timeout=180)
        self.ctx = ctx
        self.enabled = enabled
        self._render()

    def _render(self) -> None:
        self.clear_items()
        state = "Enabled" if self.enabled else "Disabled"
        button = discord.ui.Button(
            label=(
                "Disable reaction tracking"
                if self.enabled
                else "Enable reaction tracking"
            ),
            style=(
                discord.ButtonStyle.danger
                if self.enabled
                else discord.ButtonStyle.success
            ),
        )
        button.callback = self._toggle
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    "## Reaction tracking\n"
                    f"**Status:** {state}\n\n"
                    "Track the reactions you give to others, you own reactions "
                    "to yourself are not tracked."
                ),
                discord.ui.ActionRow(button),
                accent_color=self.ctx.bot.embedcolor,
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            "Only the person who opened this setting can change it.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    async def _toggle(self, interaction: discord.Interaction) -> None:
        self.enabled = not self.enabled
        await self.ctx.bot.pool.execute(
            "INSERT INTO reaction_tracking (user_id, enabled) VALUES ($1, $2) "
            "ON CONFLICT (user_id) DO UPDATE SET enabled = EXCLUDED.enabled, "
            "updated_at = now()",
            self.ctx.author.id,
            self.enabled,
        )
        if self.enabled:
            self.ctx.bot.db_cache.enable_reaction_tracking(self.ctx.author.id)
        else:
            self.ctx.bot.db_cache.disable_reaction_tracking(self.ctx.author.id)
        self._render()
        await interaction.response.edit_message(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class Dropdown(discord.ui.Select):
    def __init__(
        self, ctx: Context, data: Dict[str, List], guild_id: Optional[int] = None
    ):
        options = []
        self.ctx = ctx
        self.data = data
        self.guild_id = guild_id

        for key, items in data.items():
            options.append(
                discord.SelectOption(label=items[0], emoji=items[1], value=key),
            )

        super().__init__(
            placeholder="Choose which tracking to opt out from",
            min_values=1,
            max_values=1,
            options=options,
        )

    def update_options(self):
        self.options.clear()
        for key, items in self.data.items():
            self.options.append(
                discord.SelectOption(label=items[0], emoji=items[1], value=key)
            )

    async def user_opt(self):
        value = self.values[0]
        ctx = self.ctx

        try:
            results = ctx.bot.db_cache.opted_out[ctx.author.id]
        except KeyError:
            results = []

        if value in results:
            sql = """UPDATE opted_out SET items = array_remove(opted_out.items, $1) WHERE user_id = $2"""

            await ctx.bot.pool.execute(sql, value, ctx.author.id)
            ctx.bot.db_cache.remove_opt_out(ctx.author.id, value)
            emoji = "\U0001f7e2"
        else:
            sql = """
            INSERT INTO opted_out (user_id, items) VALUES ($1, ARRAY [$2]) 
            ON CONFLICT (user_id) DO UPDATE
            SET items = array_append(opted_out.items, $2) 
            WHERE opted_out.user_id = $1
            """

            await ctx.bot.pool.execute(sql, ctx.author.id, value)
            ctx.bot.db_cache.add_opt_out(ctx.author.id, value)
            emoji = "\U0001f534"

        self.data.update({value: [self.data[value][0], emoji]})
        self.update_options()

    async def guild_opt(self):
        value = self.values[0]
        ctx: GuildContext = self.ctx  # type: ignore
        if self.guild_id is None:
            raise ValueError("Not in a guild")

        try:
            results = ctx.bot.db_cache.opted_out[self.guild_id]
        except KeyError:
            results = []

        if value in results:
            sql = """UPDATE guild_opted_out SET items = array_remove(guild_opted_out.items, $1) WHERE guild_id = $2"""

            await ctx.bot.pool.execute(sql, value, self.guild_id)
            ctx.bot.db_cache.remove_opt_out(self.guild_id, value)
            emoji = "\U0001f7e2"
        else:
            sql = """
            INSERT INTO guild_opted_out (guild_id, items) VALUES ($1, ARRAY [$2]) 
            ON CONFLICT (guild_id) DO UPDATE
            SET items = array_append(guild_opted_out.items, $2) 
            WHERE guild_opted_out.guild_id = $1
            """

            await ctx.bot.pool.execute(sql, self.guild_id, value)
            ctx.bot.db_cache.add_opt_out(self.guild_id, value)
            emoji = "\U0001f534"

        self.data.update({value: [self.data[value][0], emoji]})
        self.update_options()

    async def callback(self, interaction: discord.Interaction):
        if self.guild_id:
            await self.guild_opt()
        else:
            await self.user_opt()

        await interaction.response.edit_message(
            view=self.view,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class DropdownView(AuthorLayoutView):
    def __init__(
        self,
        ctx: Context,
        data: Dict[str, List],
        guild_id: Optional[int] = None,
        *,
        intro: str | None = None,
    ):
        super().__init__(ctx, timeout=180)
        self.status = discord.ui.TextDisplay(
            intro
            or (
                "## Tracking settings\n"
                "Choose a category to enable or disable its tracking."
            )
        )
        self.dropdown = Dropdown(ctx, data, guild_id=guild_id)
        self.add_item(
            discord.ui.Container(
                self.status,
                discord.ui.ActionRow(self.dropdown),
                accent_color=ctx.bot.embedcolor,
            )
        )


class PrivacySettingsView(AuthorLayoutView):
    def __init__(
        self,
        ctx: Context,
        *,
        tracking_enabled: bool,
        history_public: bool,
    ) -> None:
        super().__init__(ctx, timeout=180)
        self.tracking_enabled = tracking_enabled
        self.history_public = history_public
        self.status = discord.ui.TextDisplay("")
        self.toggle_tracking = discord.ui.Button()
        self.toggle_history = discord.ui.Button()
        self.toggle_tracking.callback = self._toggle_tracking
        self.toggle_history.callback = self._toggle_history
        self._render()

    def _render(self) -> None:
        self.clear_items()
        self._update_buttons()
        self.status.content = "## Settings\n" + self.content
        self.add_item(
            discord.ui.Container(
                self.status,
                discord.ui.ActionRow(self.toggle_tracking, self.toggle_history),
                accent_color=self.ctx.bot.embedcolor,
            )
        )

    @property
    def content(self) -> str:
        tracking = "Enabled" if self.tracking_enabled else "Disabled"
        history = "Public" if self.history_public else "Private"
        return (
            f"**Tracking:** {tracking}\n"
            f"**Saved history:** {history}\n\n"
            "Tracking controls whether Fishie saves new personal activity. "
            "History visibility controls whether other users can look up data "
            "that is already saved. You can change either setting whenever "
            "you want. These settings never delete existing data."
        )

    def _update_buttons(self) -> None:
        self.toggle_tracking.label = (
            "Disable tracking" if self.tracking_enabled else "Enable tracking"
        )
        self.toggle_tracking.style = (
            discord.ButtonStyle.danger
            if self.tracking_enabled
            else discord.ButtonStyle.success
        )
        self.toggle_history.label = (
            "Make history private" if self.history_public else "Make history public"
        )
        self.toggle_history.style = (
            discord.ButtonStyle.secondary
            if self.history_public
            else discord.ButtonStyle.primary
        )

    async def _save(
        self,
        interaction: discord.Interaction,
        *,
        setting: Literal["tracking_enabled", "history_public"],
        value: bool,
    ) -> None:
        await self.ctx.bot.pool.execute(
            f"""
            INSERT INTO user_settings (user_id, {setting})
            VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE
            SET {setting} = EXCLUDED.{setting}
            """,
            self.ctx.author.id,
            value,
        )
        cache = self.ctx.bot.db_cache
        if setting == "tracking_enabled":
            if value:
                cache.tracking_disabled_users.discard(self.ctx.author.id)
            else:
                cache.tracking_disabled_users.add(self.ctx.author.id)
        else:
            cache.set_history_public(self.ctx.author.id, value)
        self._render()
        await interaction.response.edit_message(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _toggle_tracking(
        self,
        interaction: discord.Interaction,
    ) -> None:
        self.tracking_enabled = not self.tracking_enabled
        await self._save(
            interaction,
            setting="tracking_enabled",
            value=self.tracking_enabled,
        )

    async def _toggle_history(
        self,
        interaction: discord.Interaction,
    ) -> None:
        self.history_public = not self.history_public
        await self._save(
            interaction,
            setting="history_public",
            value=self.history_public,
        )


TRACKING_CATEGORY_LABELS: dict[str, str] = {
    "avatar": "Avatars",
    "username": "Usernames",
    "display": "Display names",
    "stag": "Server tags",
    "nickname": "Nicknames",
    "discrim": "Discriminators",
    "joins": "Server joins",
    "xp": "XP and message counts",
    "commands": "Command usage",
    "status": "Presence status",
    "activity": "Activities",
    "pokemon": "Pokémon solves",
    "corn": "Corn reactions",
    "emoji": "Emoji statistics",
    "downloads": "Download statistics",
    "reactions": "Reaction history",
    "snipe": "Snipe history",
    "games": "Game statistics",
    "currency": "Currency history",
}

_TRACKING_DB_ITEMS = tuple(
    key
    for key in TRACKING_CATEGORY_LABELS
    if key not in {"games", "currency", "reactions"}
)

# Whitelisted SQL targets used by ``settings delete``.  Table and column names
# never come from user input; the command only resolves a user-facing alias to
# one of these static entries before constructing a query.
_DELETE_SPECS: dict[str, tuple[tuple[str, str], ...]] = {
    "avatar": (("avatars", "user_id"), ("guild_avatars", "member_id")),
    "username": (("username_logs", "user_id"),),
    "display": (("display_name_logs", "user_id"),),
    "display_name": (("display_name_logs", "user_id"),),
    "nickname": (("nickname_logs", "user_id"),),
    "servertag": (("stag_logs", "user_id"),),
    "stag": (("stag_logs", "user_id"),),
    "discrim": (("discrim_logs", "user_id"),),
    "discriminators": (("discrim_logs", "user_id"),),
    "joins": (("member_join_logs", "member_id"),),
    "status": (("user_status_history", "user_id"), ("user_statuses", "user_id")),
    "commands": (("command_logs", "user_id"),),
    "xp": (("message_xp", "user_id"),),
    "pokemon": (("pokemon_solves", "user_id"), ("pokemon_guesses", "author_id")),
    "corn": (("corn_reacts", "giver_id"), ("corn_reacts", "receiver_id")),
    "emoji": (("emoji_stats", "author_id"),),
    "downloads": (("download_stats", "user_id"), ("download_events", "user_id")),
    "reactions": (("reaction_logs", "giver_id"), ("reaction_logs", "receiver_id")),
    "games": (
        ("game_2048_stats", "user_id"),
        ("game_2048_games", "user_id"),
        ("wordle_games", "user_id"),
        ("wordle_stats", "user_id"),
        ("streak_game_stats", "user_id"),
        ("wordbomb_stats", "user_id"),
        ("tictactoe_games", "player_x_id"),
        ("tictactoe_games", "player_o_id"),
        ("connectfour_games", "player_yellow_id"),
        ("connectfour_games", "player_red_id"),
        ("lightsout_games", "user_id"),
    ),
    "currency": (
        ("currency_transactions", "user_id"),
        ("currency_claims", "user_id"),
        ("currency_daily_rewards", "user_id"),
        ("currency_wagers", "user_id"),
        ("currency_gambling_stats", "user_id"),
        ("currency_wallets", "user_id"),
        ("lottery_tickets", "user_id"),
        ("lottery_rounds", "winner_user_id"),
    ),
}

_DELETE_ALIASES = {
    "avatars": "avatar",
    "display-names": "display_name",
    "display names": "display_name",
    "server-tag": "servertag",
    "server-tags": "servertag",
    "discriminator": "discriminators",
    "join": "joins",
    "game": "games",
    "game-history": "games",
    "reaction": "reactions",
    "download": "downloads",
    "commands": "commands",
}

# Values exposed by the slash-command autocomplete intentionally use the
# shortest stable spelling accepted by ``_delete_category``.  Aliases remain
# valid for text commands and manual slash input as well.
_DELETE_CATEGORY_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Avatars", "avatar"),
    ("Usernames", "username"),
    ("Display names", "display-name"),
    ("Nicknames", "nickname"),
    ("Server tags", "servertag"),
    ("Discriminators", "discriminator"),
    ("Server joins", "joins"),
    ("Status history", "status"),
    ("Command logs", "commands"),
    ("XP history", "xp"),
    ("Pokémon data", "pokemon"),
    ("Corn reactions", "corn"),
    ("Emoji statistics", "emoji"),
    ("Download statistics", "downloads"),
    ("Reaction history", "reactions"),
    ("Game statistics", "games"),
    ("Currency history", "currency"),
)

_SERVER_DELETE_SPECS: dict[str, tuple[tuple[str, str], ...]] = {
    "avatars": (("guild_avatars", "guild_id"),),
    "icons": (("guild_icons", "guild_id"),),
    "names": (("guild_name_logs", "guild_id"),),
    "joins": (("member_join_logs", "guild_id"), ("guild_join_logs", "guild_id")),
    "status": (("user_status_history", "guild_id"), ("user_statuses", "guild_id")),
    "commands": (("command_logs", "guild_id"),),
    "emoji": (("emoji_stats", "guild_id"),),
    "downloads": (("download_events", "guild_id"),),
    "corn": (("corn_reacts", "guild_id"),),
    "reactions": (("reaction_logs", "guild_id"),),
    "tags": (("tags", "guild_id"),),
    "mudae": (
        ("mudae_wishes", "guild_id"),
        ("mudae_timers", "guild_id"),
        ("mudae_series_bundles", "guild_id"),
        ("mudae_series", "source_guild_id"),
    ),
    "all": (
        ("guild_avatars", "guild_id"),
        ("guild_icons", "guild_id"),
        ("guild_name_logs", "guild_id"),
        ("member_join_logs", "guild_id"),
        ("guild_join_logs", "guild_id"),
        ("user_status_history", "guild_id"),
        ("user_statuses", "guild_id"),
        ("command_logs", "guild_id"),
        ("emoji_stats", "guild_id"),
        ("download_events", "guild_id"),
        ("corn_reacts", "guild_id"),
        ("reaction_logs", "guild_id"),
        ("tags", "guild_id"),
        ("mudae_wishes", "guild_id"),
        ("mudae_timers", "guild_id"),
        ("mudae_series_bundles", "guild_id"),
        ("mudae_series", "source_guild_id"),
    ),
}

_SERVER_DELETE_CATEGORY_OPTIONS: tuple[tuple[str, str], ...] = (
    ("All server data", "all"),
    ("Avatars", "avatars"),
    ("Icons", "icons"),
    ("Server names", "names"),
    ("Member joins", "joins"),
    ("Status history", "status"),
    ("Command logs", "commands"),
    ("Emoji statistics", "emoji"),
    ("Download statistics", "downloads"),
    ("Corn reactions", "corn"),
    ("Reaction history", "reactions"),
    ("Server tags", "tags"),
    ("Mudae data", "mudae"),
)


class TrackingCategorySelect(discord.ui.Select):
    """Dropdown for all personal tracking categories.

    The first option is an aggregate switch.  It deliberately updates every
    category in one transaction so a partial request cannot leave the UI with
    a misleading ``All`` state.
    """

    def __init__(self, parent: "TrackingSettingsView") -> None:
        self.parent_view = parent
        options: list[discord.SelectOption] = []
        for key, label in (("all", "All"), *TRACKING_CATEGORY_LABELS.items()):
            state = parent.all_state() if key == "all" else parent.category_enabled(key)
            options.append(
                discord.SelectOption(
                    label=label,
                    value=key,
                    emoji=(
                        "\u26aa"
                        if state is None
                        else "\U0001f7e2" if state else "\U0001f534"
                    ),
                )
            )
        super().__init__(
            placeholder="Choose tracking to enable or disable",
            min_values=1,
            max_values=1,
            options=options,
        )

    def refresh_options(self) -> None:
        for option in self.options:
            key = option.value
            option.emoji = (
                "\u26aa"
                if key == "all" and self.parent_view.all_state() is None
                else (
                    "\U0001f7e2"
                    if self.parent_view.category_enabled(key)
                    else "\U0001f534"
                )
            )

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0] if self.values else ""
        await self.parent_view.toggle_category(value)
        self.refresh_options()
        await interaction.response.edit_message(
            view=self.view,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class TrackingSettingsView(AuthorLayoutView):
    """Unified personal tracking/privacy controls.

    Category tracking is intentionally separate from deletion.  Deletion is
    exposed through ``settings delete`` so an accidental dropdown click can
    never remove stored history.
    """

    def __init__(
        self,
        ctx: Context,
        *,
        tracking_enabled: bool,
        history_public: bool,
        game_tracking_enabled: bool = True,
        game_history_public: bool = True,
        currency_tracking_enabled: bool = True,
        opted_out: list[str] | None = None,
        reaction_tracking_enabled: bool = False,
    ) -> None:
        super().__init__(ctx, timeout=300)
        self.tracking_enabled = tracking_enabled
        self.history_public = history_public
        self.game_tracking_enabled = game_tracking_enabled
        self.game_history_public = game_history_public
        self.currency_tracking_enabled = currency_tracking_enabled
        self.reaction_tracking_enabled = reaction_tracking_enabled
        self.opted_out = set(opted_out or ())
        self.status = discord.ui.TextDisplay("")
        self.dropdown = TrackingCategorySelect(self)
        self._render()

    def category_enabled(self, key: str) -> bool:
        if key == "all":
            all_state = self.all_state()
            return bool(all_state)
        if not self.tracking_enabled:
            return False
        if key == "games":
            return self.game_tracking_enabled
        if key == "currency":
            return self.currency_tracking_enabled
        if key == "reactions":
            return self.reaction_tracking_enabled
        return key not in self.opted_out

    def all_state(self) -> bool | None:
        states = [self.category_enabled(key) for key in TRACKING_CATEGORY_LABELS]
        if all(states):
            return True
        if not any(states):
            return False
        return None

    @property
    def prefix(self) -> str:
        prefix = getattr(self.ctx, "get_prefix", None)
        if isinstance(prefix, str) and prefix:
            return prefix
        return "/" if getattr(self.ctx, "interaction", None) else "fish "

    def _render(self) -> None:
        self.clear_items()
        # The dropdown owns all tracking enable/disable switches, including
        # the aggregate ``All`` and the bundled ``Games`` category.  Keep
        # only the two privacy controls as buttons so the view has one clear
        # place for each kind of setting.
        history_label = (
            "Make tracking history private"
            if self.history_public
            else "Make tracking history public"
        )
        history_style = (
            discord.ButtonStyle.danger
            if self.history_public
            else discord.ButtonStyle.success
        )
        history_button = discord.ui.Button(label=history_label, style=history_style)
        game_history_label = (
            "Make game history private"
            if self.game_history_public
            else "Make game history public"
        )
        game_history_button = discord.ui.Button(
            label=game_history_label,
            style=(
                discord.ButtonStyle.danger
                if self.game_history_public
                else discord.ButtonStyle.success
            ),
        )
        history_button.callback = self._toggle_history
        game_history_button.callback = self._toggle_game_history
        self.dropdown = TrackingCategorySelect(self)
        self.status.content = (
            "## Tracking settings\n\n"
            "Manage your tracking data, privacy or delete your data with the buttons below."
        )
        self.add_item(
            discord.ui.Container(
                self.status,
                discord.ui.ActionRow(history_button, game_history_button),
                discord.ui.ActionRow(self.dropdown),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"-# Want to delete your saved data? Use the `{self.prefix}settings delete` command."
                ),
                accent_color=self.ctx.bot.embedcolor,
            )
        )

    async def _save_settings(self, **values: bool) -> None:
        fields = {
            key: value
            for key, value in values.items()
            if key
            in {
                "tracking_enabled",
                "history_public",
                "game_tracking_enabled",
                "game_history_public",
                "currency_tracking_enabled",
            }
        }
        if not fields:
            return
        columns = ", ".join(fields)
        placeholders = ", ".join(f"${index}" for index in range(1, len(fields) + 2))
        assignments = ", ".join(f"{key} = EXCLUDED.{key}" for key in fields)
        await self.ctx.bot.pool.execute(
            f"INSERT INTO user_settings (user_id, {columns}) VALUES ({placeholders}) "
            f"ON CONFLICT (user_id) DO UPDATE SET {assignments}",
            self.ctx.author.id,
            *fields.values(),
        )

    async def _toggle_tracking(self, interaction: discord.Interaction) -> None:
        self.tracking_enabled = not self.tracking_enabled
        await self._save_settings(tracking_enabled=self.tracking_enabled)
        self.ctx.bot.db_cache.set_tracking_enabled(
            self.ctx.author.id, self.tracking_enabled
        )
        self._render()
        await interaction.response.edit_message(
            view=self, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _toggle_history(self, interaction: discord.Interaction) -> None:
        self.history_public = not self.history_public
        await self._save_settings(history_public=self.history_public)
        cache = self.ctx.bot.db_cache
        cache.set_history_public(self.ctx.author.id, self.history_public)
        if self.history_public:
            await self.ctx.bot.pool.execute(
                "UPDATE user_settings SET tracking_consent = TRUE WHERE user_id = $1",
                self.ctx.author.id,
            )
            cache.set_tracking_consent(self.ctx.author.id)
        self._render()
        await interaction.response.edit_message(
            view=self, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _toggle_game_tracking(self, interaction: discord.Interaction) -> None:
        self.game_tracking_enabled = not self.game_tracking_enabled
        await self._save_settings(game_tracking_enabled=self.game_tracking_enabled)
        self.ctx.bot.db_cache.set_game_tracking_enabled(
            self.ctx.author.id, self.game_tracking_enabled
        )
        self._render()
        await interaction.response.edit_message(
            view=self, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _toggle_game_history(self, interaction: discord.Interaction) -> None:
        self.game_history_public = not self.game_history_public
        await self._save_settings(game_history_public=self.game_history_public)
        self.ctx.bot.db_cache.set_game_history_public(
            self.ctx.author.id, self.game_history_public
        )
        if self.game_history_public:
            await self.ctx.bot.pool.execute(
                "UPDATE user_settings SET tracking_consent = TRUE WHERE user_id = $1",
                self.ctx.author.id,
            )
            self.ctx.bot.db_cache.set_tracking_consent(self.ctx.author.id)
        self._render()
        await interaction.response.edit_message(
            view=self, allowed_mentions=discord.AllowedMentions.none()
        )

    async def toggle_category(self, key: str) -> None:
        if key == "all":
            target = self.all_state() is not True
            self.tracking_enabled = target
            self.game_tracking_enabled = target
            self.currency_tracking_enabled = target
            self.reaction_tracking_enabled = target
            self.opted_out = set() if target else set(_TRACKING_DB_ITEMS)
            await self._save_settings(
                tracking_enabled=target,
                game_tracking_enabled=target,
                currency_tracking_enabled=target,
            )
            await self.ctx.bot.pool.execute(
                "INSERT INTO reaction_tracking (user_id, enabled) VALUES ($1, $2) "
                "ON CONFLICT (user_id) DO UPDATE SET enabled = EXCLUDED.enabled, updated_at = now()",
                self.ctx.author.id,
                target,
            )
            if target:
                await self.ctx.bot.pool.execute(
                    "DELETE FROM opted_out WHERE user_id = $1", self.ctx.author.id
                )
            else:
                await self.ctx.bot.pool.execute(
                    "INSERT INTO opted_out (user_id, items) VALUES ($1, $2) "
                    "ON CONFLICT (user_id) DO UPDATE SET items = EXCLUDED.items",
                    self.ctx.author.id,
                    list(_TRACKING_DB_ITEMS),
                )
        elif key in TRACKING_CATEGORY_LABELS:
            target = not self.category_enabled(key)
            if key == "games":
                self.game_tracking_enabled = target
                await self._save_settings(game_tracking_enabled=target)
            elif key == "currency":
                self.currency_tracking_enabled = target
                await self._save_settings(currency_tracking_enabled=target)
            elif key == "reactions":
                self.reaction_tracking_enabled = target
                await self.ctx.bot.pool.execute(
                    "INSERT INTO reaction_tracking (user_id, enabled) VALUES ($1, $2) "
                    "ON CONFLICT (user_id) DO UPDATE SET enabled = EXCLUDED.enabled, updated_at = now()",
                    self.ctx.author.id,
                    target,
                )
            else:
                if target:
                    self.opted_out.discard(key)
                    await self.ctx.bot.pool.execute(
                        "UPDATE opted_out SET items = array_remove(items, $1) WHERE user_id = $2",
                        key,
                        self.ctx.author.id,
                    )
                else:
                    self.opted_out.add(key)
                    await self.ctx.bot.pool.execute(
                        "INSERT INTO opted_out (user_id, items) VALUES ($1, ARRAY[$2]) "
                        "ON CONFLICT (user_id) DO UPDATE SET items = array_append(opted_out.items, $2)",
                        self.ctx.author.id,
                        key,
                    )

        cache = self.ctx.bot.db_cache
        cache.set_tracking_enabled(self.ctx.author.id, self.tracking_enabled)
        cache.set_game_tracking_enabled(self.ctx.author.id, self.game_tracking_enabled)
        cache.set_currency_tracking_enabled(
            self.ctx.author.id, self.currency_tracking_enabled
        )
        if self.reaction_tracking_enabled:
            cache.enable_reaction_tracking(self.ctx.author.id)
        else:
            cache.disable_reaction_tracking(self.ctx.author.id)
        cache.opted_out[self.ctx.author.id] = sorted(self.opted_out)
        self._render()


SERVER_TRACKING_CATEGORY_LABELS: dict[str, str] = {
    "icon": "Server icon history",
    "name": "Server name history",
    "joins": "Member join history",
    "status": "Member status history",
    "commands": "Server command logs",
    "emoji": "Emoji statistics",
    "downloads": "Download statistics",
    "corn": "Corn reactions",
    "reactions": "Reaction history",
    "tags": "Server tags",
    "mudae": "Mudae wishes and timers",
}


class GuildTrackingCategorySelect(discord.ui.Select):
    """Aggregate and per-category server tracking switch."""

    def __init__(self, parent: "GuildTrackingSettingsView") -> None:
        self.parent_view = parent
        options: list[discord.SelectOption] = []
        for key, label in (("all", "All"), *SERVER_TRACKING_CATEGORY_LABELS.items()):
            state = parent.all_state() if key == "all" else parent.category_enabled(key)
            options.append(
                discord.SelectOption(
                    label=label,
                    value=key,
                    emoji=(
                        "\u26aa"
                        if state is None
                        else "\U0001f7e2" if state else "\U0001f534"
                    ),
                )
            )
        super().__init__(
            placeholder="Choose server tracking to enable or disable",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        key = self.values[0] if self.values else ""
        await self.parent_view.toggle_category(key)
        await interaction.response.edit_message(
            view=self.view,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class GuildTrackingSettingsView(AuthorLayoutView):
    """Server counterpart to :class:`TrackingSettingsView`.

    Guilds expose server-owned history and server-scoped activity categories;
    personal game/currency settings remain intentionally absent.
    """

    def __init__(
        self,
        ctx: GuildContext,
        *,
        tracking_enabled: bool,
        history_public: bool,
        opted_out: set[str],
    ) -> None:
        super().__init__(ctx, timeout=300)
        self.guild_ctx = ctx
        self.tracking_enabled = tracking_enabled
        self.history_public = history_public
        self.opted_out = opted_out
        self.status = discord.ui.TextDisplay("")
        self._render()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        guild = self.guild_ctx.guild
        if interaction.guild_id != guild.id:
            message = "These server tracking settings can only be used in their original server."
        elif interaction.user.id != self.guild_ctx.author.id:
            message = "Only the administrator who opened these settings can use them."
        else:
            member = interaction.user
            if (
                getattr(getattr(member, "guild", None), "id", None) != guild.id
                or not hasattr(member, "guild_permissions")
            ):
                get_member = getattr(guild, "get_member", None)
                member = get_member(interaction.user.id) if callable(get_member) else None
            if member and (
                getattr(guild, "owner_id", None) == member.id
                or bool(
                    getattr(
                        getattr(member, "guild_permissions", None),
                        "manage_guild",
                        False,
                    )
                )
            ):
                return True
            message = "You no longer have Manage Server permission to change server tracking."
        await interaction.response.send_message(
            message,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    def _render(self) -> None:
        self.clear_items()
        disable_label = (
            "Disable tracking" if self.tracking_enabled else "Enable tracking"
        )
        history_label = (
            "Make history private" if self.history_public else "Make history public"
        )
        tracking = discord.ui.Button(
            label=disable_label,
            style=(
                discord.ButtonStyle.danger
                if self.tracking_enabled
                else discord.ButtonStyle.success
            ),
        )
        history = discord.ui.Button(
            label=history_label,
            style=(
                discord.ButtonStyle.danger
                if self.history_public
                else discord.ButtonStyle.success
            ),
        )
        tracking.callback = self._toggle_tracking
        history.callback = self._toggle_history
        dropdown = GuildTrackingCategorySelect(self)
        self.status.content = (
            f"## Server tracking settings · {self.guild_ctx.guild.name}\n\n"
            "Manage server tracking, privacy, or delete saved server data."
        )
        self.add_item(
            discord.ui.Container(
                self.status,
                discord.ui.ActionRow(tracking, history),
                discord.ui.ActionRow(dropdown),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    "-# Want to delete saved server data? Use `settings server delete`."
                ),
                accent_color=self.ctx.bot.embedcolor,
            )
        )

    def category_enabled(self, key: str) -> bool:
        if key == "all":
            state = self.all_state()
            return bool(state)
        if not self.tracking_enabled:
            return False
        return key not in self.opted_out

    def all_state(self) -> bool | None:
        states = [self.category_enabled(key) for key in SERVER_TRACKING_CATEGORY_LABELS]
        if all(states):
            return True
        if not any(states):
            return False
        return None

    async def toggle_category(self, key: str) -> None:
        if key == "all":
            target = self.all_state() is not True
            self.tracking_enabled = target
            self.opted_out = set() if target else set(SERVER_TRACKING_CATEGORY_LABELS)
            await self.guild_ctx.bot.pool.execute(
                "INSERT INTO guild_settings (guild_id, tracking_enabled) VALUES ($1, $2) "
                "ON CONFLICT (guild_id) DO UPDATE SET tracking_enabled = EXCLUDED.tracking_enabled",
                self.guild_ctx.guild.id,
                target,
            )
            await self.guild_ctx.bot.pool.execute(
                "INSERT INTO guild_opted_out (guild_id, items) VALUES ($1, $2) "
                "ON CONFLICT (guild_id) DO UPDATE SET items = EXCLUDED.items",
                self.guild_ctx.guild.id,
                sorted(self.opted_out),
            )
        elif key in SERVER_TRACKING_CATEGORY_LABELS:
            target = not self.category_enabled(key)
            if target:
                self.opted_out.discard(key)
            else:
                self.opted_out.add(key)
            await self.guild_ctx.bot.pool.execute(
                "INSERT INTO guild_opted_out (guild_id, items) VALUES ($1, $2) "
                "ON CONFLICT (guild_id) DO UPDATE SET items = EXCLUDED.items",
                self.guild_ctx.guild.id,
                sorted(self.opted_out),
            )
        self.guild_ctx.bot.db_cache.opted_out[self.guild_ctx.guild.id] = sorted(
            self.opted_out
        )
        self.guild_ctx.bot.db_cache.set_guild_tracking_enabled(
            self.guild_ctx.guild.id, self.tracking_enabled
        )
        self._render()

    async def _toggle_tracking(self, interaction: discord.Interaction) -> None:
        self.tracking_enabled = not self.tracking_enabled
        await self.guild_ctx.bot.pool.execute(
            "INSERT INTO guild_settings (guild_id, tracking_enabled) VALUES ($1, $2) "
            "ON CONFLICT (guild_id) DO UPDATE SET tracking_enabled = EXCLUDED.tracking_enabled",
            self.guild_ctx.guild.id,
            self.tracking_enabled,
        )
        self.guild_ctx.bot.db_cache.set_guild_tracking_enabled(
            self.guild_ctx.guild.id, self.tracking_enabled
        )
        self._render()
        await interaction.response.edit_message(
            view=self, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _toggle_history(self, interaction: discord.Interaction) -> None:
        self.history_public = not self.history_public
        await self.guild_ctx.bot.pool.execute(
            "INSERT INTO guild_settings (guild_id, history_public) VALUES ($1, $2) "
            "ON CONFLICT (guild_id) DO UPDATE SET history_public = EXCLUDED.history_public",
            self.guild_ctx.guild.id,
            self.history_public,
        )
        self.guild_ctx.bot.db_cache.set_guild_history_public(
            self.guild_ctx.guild.id, self.history_public
        )
        self._render()
        await interaction.response.edit_message(
            view=self, allowed_mentions=discord.AllowedMentions.none()
        )


class Logging(Cog):
    async def privacy_settings_view(self, ctx: Context) -> TrackingSettingsView:
        row = await self.bot.pool.fetchrow(
            """
            SELECT tracking_enabled, history_public,
                   game_tracking_enabled, game_history_public,
                   currency_tracking_enabled
            FROM user_settings
            WHERE user_id = $1
            """,
            ctx.author.id,
        )
        opted_out = await self.bot.pool.fetchval(
            "SELECT items FROM opted_out WHERE user_id = $1", ctx.author.id
        )
        reaction_enabled = self.bot.db_cache.reaction_tracking_enabled(ctx.author.id)
        return TrackingSettingsView(
            ctx,
            tracking_enabled=bool(row["tracking_enabled"]) if row else True,
            history_public=bool(row["history_public"]) if row else False,
            game_tracking_enabled=(bool(row["game_tracking_enabled"]) if row else True),
            game_history_public=(bool(row["game_history_public"]) if row else True),
            currency_tracking_enabled=(
                bool(row["currency_tracking_enabled"]) if row else True
            ),
            opted_out=list(opted_out or ()),
            reaction_tracking_enabled=reaction_enabled,
        )

    async def send_privacy_settings_interaction(
        self,
        ctx: Context,
        interaction: discord.Interaction,
    ) -> None:
        view = await self.privacy_settings_view(ctx)
        await interaction.response.send_message(
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @cast(Any, commands.hybrid_group)(
        name="settings", fallback="user", invoke_without_command=True
    )
    async def settings(self, ctx: Context) -> None:
        """Manage your Fishie privacy and tracking settings."""
        view = await self.privacy_settings_view(ctx)
        view.message = await ctx.send(
            view=view,
            ephemeral=ctx.interaction is not None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @settings.command(name="wordle")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def settings_wordle(self, ctx: Context) -> None:
        """Configure your Wordle hard-mode and colourblind preferences."""
        # Import lazily to avoid loading the Fun extension while settings is
        # being imported by the extension loader.
        from extensions.fun.wordle import (
            WordleSettingsView,
            get_wordle_settings,
            save_wordle_settings,
        )

        hard_mode, colourblind_mode = await get_wordle_settings(
            self.bot.pool, ctx.author.id
        )

        async def save(user_id: int, hard: bool, colourblind: bool) -> None:
            await save_wordle_settings(self.bot.pool, user_id, hard, colourblind)

        await ctx.send(
            view=WordleSettingsView(
                ctx.author.id,
                hard_mode,
                colourblind_mode,
                save,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=ctx.interaction is not None,
        )

    @settings.command(name="tracking")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def settings_tracking(self, ctx: Context) -> None:
        """Manage tracking, saved-history, and game-history privacy."""
        view = await self.privacy_settings_view(ctx)
        await ctx.send(
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=ctx.interaction is not None,
        )

    @settings.group(name="delete", fallback="info", invoke_without_command=True)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def settings_delete(self, ctx: Context) -> None:
        """Explain how to permanently delete saved tracking data."""
        text = (
            "## Delete saved data\n\n"
            "Use `settings delete all` to review and delete all saved tracking "
            "data, or choose a category such as `avatar`, `username`, `joins`, "
            "`downloads`, `reactions`, `games`, or `currency`.\n\n"
            "Individual records can be removed with `settings delete avatar <id>` "
            "or the matching category command. Every deletion requires confirmation."
        )
        view = discord.ui.LayoutView(timeout=300)
        view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(text),
                accent_color=ctx.bot.embedcolor,
            )
        )
        await ctx.send(
            view=view,
            ephemeral=ctx.interaction is not None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @staticmethod
    def _delete_category(category: str | None) -> str | None:
        if category is None:
            return None
        normalized = category.strip().casefold().replace("_", "-")
        normalized = _DELETE_ALIASES.get(normalized, normalized)
        return normalized if normalized in _DELETE_SPECS else None

    @staticmethod
    def _category_choices(
        current: str,
        options: tuple[tuple[str, str], ...],
    ) -> list[app_commands.Choice[str]]:
        query = current.casefold().strip()
        return [
            app_commands.Choice(name=label, value=value)
            for label, value in options
            if not query or query in label.casefold() or query in value.casefold()
        ][:25]

    async def _delete_id_choices(
        self,
        interaction: discord.Interaction,
        current: str,
        category: str,
    ) -> list[app_commands.Choice[int]]:
        """Return saved record IDs for a ``settings delete`` item command."""

        query = current.casefold().strip()
        seen: set[int] = set()
        choices: list[app_commands.Choice[int]] = []
        specs = _DELETE_SPECS.get(category, ())
        for table, column in specs:
            try:
                rows = await self.bot.pool.fetch(
                    f"SELECT id FROM {table} WHERE {column} = $1 "
                    "ORDER BY id DESC LIMIT 100",
                    interaction.user.id,
                )
            except Exception:
                # A category can span tables that are not present in older
                # installations.  Do not make autocomplete fail because one
                # optional history table is unavailable.
                continue
            for row in rows:
                try:
                    record_id = int(row["id"])
                except (KeyError, TypeError, ValueError):
                    continue
                if record_id in seen:
                    continue
                label = f"{category.replace('_', ' ').title()} record · ID {record_id}"
                if (
                    query
                    and query not in str(record_id)
                    and query not in label.casefold()
                ):
                    continue
                seen.add(record_id)
                choices.append(app_commands.Choice(name=label[:100], value=record_id))
                if len(choices) >= 25:
                    return choices
        return choices

    async def _delete_counts(
        self, user_id: int, category: str | None
    ) -> dict[str, int]:
        categories = (category,) if category else tuple(_DELETE_SPECS)
        counts: dict[str, int] = {}
        for name in categories:
            if name is None:
                continue
            total = 0
            for table, column in _DELETE_SPECS[name]:
                total += int(
                    await self.bot.pool.fetchval(
                        f"SELECT COUNT(*) FROM {table} WHERE {column} = $1", user_id
                    )
                    or 0
                )
            counts[name] = total
        return counts

    async def _delete_user_category(self, user_id: int, category: str) -> int:
        deleted = 0
        seen: set[tuple[str, str]] = set()
        for table, column in _DELETE_SPECS[category]:
            # Some game/reaction tables identify a user through two columns;
            # deleting each predicate is intentional, but avoid duplicate SQL
            # when an alias appears more than once in a category.
            key = (table, column)
            if key in seen:
                continue
            seen.add(key)
            result = await self.bot.pool.execute(
                f"DELETE FROM {table} WHERE {column} = $1", user_id
            )
            try:
                deleted += int(str(result).rsplit(" ", 1)[-1])
            except (TypeError, ValueError):
                continue
        if category == "reactions":
            await self.bot.pool.execute(
                "DELETE FROM reaction_tracking WHERE user_id = $1", user_id
            )
        return deleted

    async def _confirm_delete_categories(
        self, ctx: Context, categories: dict[str, int], *, scope: str = "your"
    ) -> None:
        nonzero = {key: count for key, count in categories.items() if count}
        if not nonzero:
            await ctx.send(
                "There is no saved data to delete.",
                ephemeral=ctx.interaction is not None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        lines = [
            f"**{TRACKING_CATEGORY_LABELS.get(key, key.title())}:** {count:,}"
            for key, count in nonzero.items()
        ]
        prompt = await ctx.prompt(
            "This permanently deletes the following saved data for "
            f"{scope}. **This cannot be undone.**\n\n" + "\n".join(lines),
            ephemeral=ctx.interaction is not None,
            delete_after=False,
        )
        if not prompt:
            await ctx.send("No data was deleted.", ephemeral=True)
            return
        await prompt.edit(content="Deleting your saved data…", view=None)
        removed = 0
        for category in nonzero:
            removed += await self._delete_user_category(ctx.author.id, category)
        await prompt.edit(content=f"Deleted {removed:,} saved record(s).")

    @settings_delete.command(name="all")
    @app_commands.describe(
        category="Optional category to delete instead of everything."
    )
    async def settings_delete_all(
        self, ctx: Context, category: str | None = None
    ) -> None:
        """Review and delete all saved tracking records, optionally by category."""
        selected = self._delete_category(category)
        if category and selected is None:
            raise commands.BadArgument(
                "Unknown category. Use avatar, username, display-name, nickname, "
                "servertag, discrim, joins, status, commands, xp, pokemon, corn, "
                "emoji, downloads, reactions, games, or currency."
            )
        counts = await self._delete_counts(ctx.author.id, selected)
        await self._confirm_delete_categories(ctx, counts)

    @settings_delete_all.autocomplete("category")
    async def settings_delete_category_autocomplete(
        self, _interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return self._category_choices(current, _DELETE_CATEGORY_OPTIONS)

    async def _delete_single_record(
        self, ctx: Context, category: str, record_id: int
    ) -> None:
        specs = _DELETE_SPECS[category]
        rows: list[Any] = []
        for table, column in specs:
            # Every individual command currently exposed by this group uses a
            # serial/identity ``id`` column.  A missing table/column is handled
            # as an absent record rather than leaking a database error.
            try:
                rows.extend(
                    await self.bot.pool.fetch(
                        f"SELECT * FROM {table} WHERE {column} = $1 AND id = $2",
                        ctx.author.id,
                        record_id,
                    )
                )
            except Exception:
                continue
        if not rows:
            await ctx.send(
                f"No {TRACKING_CATEGORY_LABELS.get(category, category)} record found with ID `{record_id}`.",
                ephemeral=ctx.interaction is not None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        row = rows[0]
        value = next(
            (
                str(row[key])
                for key in (
                    "username",
                    "display_name",
                    "nickname",
                    "tag",
                    "discrim",
                    "avatar",
                    "status",
                    "emoji_id",
                    "site",
                    "command",
                )
                if row.get(key) is not None
            ),
            category.title(),
        )
        created = row.get("created_at") or row.get("time") or row.get("started_at")
        when = discord.utils.format_dt(created, "R") if created else "an unknown time"
        prompt = await ctx.prompt(
            f"Delete this saved {TRACKING_CATEGORY_LABELS.get(category, category)} record?\n"
            f"**Value:** `{value}`\n**Saved:** {when}\n**ID:** `{record_id}`",
            ephemeral=ctx.interaction is not None,
            delete_after=False,
        )
        if not prompt:
            await ctx.send("No data was deleted.", ephemeral=True)
            return
        await prompt.edit(content="Deleting the selected record…", view=None)
        removed = 0
        for table, column in specs:
            try:
                result = await self.bot.pool.execute(
                    f"DELETE FROM {table} WHERE {column} = $1 AND id = $2",
                    ctx.author.id,
                    record_id,
                )
                removed += int(str(result).rsplit(" ", 1)[-1])
            except Exception:
                continue
        await prompt.edit(content=f"Deleted {removed:,} record(s).")

    async def _delete_item_command(
        self, ctx: Context, category: str, record_id: int
    ) -> None:
        await self._delete_single_record(ctx, category, record_id)

    @settings_delete.command(name="avatar")
    @app_commands.describe(id="ID of the saved avatar to remove.")
    async def settings_delete_avatar(self, ctx: Context, id: int) -> None:
        """Delete one saved avatar record after confirmation."""
        await self._delete_item_command(ctx, "avatar", id)

    @settings_delete.command(name="username")
    @app_commands.describe(id="ID of the saved username record to delete.")
    async def settings_delete_username(self, ctx: Context, id: int) -> None:
        """Delete one saved username record after confirmation."""
        await self._delete_item_command(ctx, "username", id)

    @settings_delete.command(name="display-name", aliases=("display",))
    @app_commands.describe(id="ID of the saved display-name record to delete.")
    async def settings_delete_display(self, ctx: Context, id: int) -> None:
        """Delete one saved display-name record after confirmation."""
        await self._delete_item_command(ctx, "display_name", id)

    @settings_delete.command(name="nickname")
    @app_commands.describe(id="ID of the saved nickname record to delete.")
    async def settings_delete_nickname(self, ctx: Context, id: int) -> None:
        """Delete one saved nickname record after confirmation."""
        await self._delete_item_command(ctx, "nickname", id)

    @settings_delete.command(name="servertag", aliases=("stag", "tag"))
    @app_commands.describe(id="ID of the saved server-tag record to delete.")
    async def settings_delete_servertag(self, ctx: Context, id: int) -> None:
        """Delete one saved server-tag record after confirmation."""
        await self._delete_item_command(ctx, "servertag", id)

    @settings_delete.command(name="discriminator", aliases=("discrim",))
    @app_commands.describe(id="ID of the saved discriminator record to delete.")
    async def settings_delete_discriminator(self, ctx: Context, id: int) -> None:
        """Delete one saved discriminator record after confirmation."""
        await self._delete_item_command(ctx, "discriminators", id)

    @settings_delete.command(name="joins", aliases=("join",))
    @app_commands.describe(id="ID of the saved server-join record to delete.")
    async def settings_delete_joins(self, ctx: Context, id: int) -> None:
        """Delete one saved server-join record after confirmation."""
        await self._delete_item_command(ctx, "joins", id)

    @settings_delete.command(name="status")
    @app_commands.describe(id="ID of the saved status record to delete.")
    async def settings_delete_status(self, ctx: Context, id: int) -> None:
        """Delete one saved status record after confirmation."""
        await self._delete_item_command(ctx, "status", id)

    @settings_delete.command(name="pokemon")
    @app_commands.describe(id="ID of the saved Pokémon record to delete.")
    async def settings_delete_pokemon(self, ctx: Context, id: int) -> None:
        """Delete one saved Pokémon record after confirmation."""
        await self._delete_item_command(ctx, "pokemon", id)

    @settings_delete.command(name="corn")
    @app_commands.describe(id="ID of the saved corn-reaction record to delete.")
    async def settings_delete_corn(self, ctx: Context, id: int) -> None:
        """Delete one saved corn-reaction record after confirmation."""
        await self._delete_item_command(ctx, "corn", id)

    @settings_delete.command(name="reaction", aliases=("reactions",))
    @app_commands.describe(id="ID of the saved reaction record to delete.")
    async def settings_delete_reaction(self, ctx: Context, id: int) -> None:
        """Delete one saved reaction record after confirmation."""
        await self._delete_item_command(ctx, "reactions", id)

    @settings_delete.command(name="game", aliases=("games",))
    @app_commands.describe(id="ID of the saved game record to delete.")
    async def settings_delete_game(self, ctx: Context, id: int) -> None:
        """Delete saved game records after confirmation."""
        await self._delete_item_command(ctx, "games", id)

    @settings_delete.command(name="xp")
    @app_commands.describe(id="ID of the saved XP record to delete.")
    async def settings_delete_xp(self, ctx: Context, id: int) -> None:
        """Delete one saved XP/message record after confirmation."""
        await self._delete_item_command(ctx, "xp", id)

    @settings_delete.command(name="commands", aliases=("command",))
    @app_commands.describe(id="ID of the saved command-log record to delete.")
    async def settings_delete_commands(self, ctx: Context, id: int) -> None:
        """Delete one saved command-log record after confirmation."""
        await self._delete_item_command(ctx, "commands", id)

    @settings_delete.command(name="emoji")
    @app_commands.describe(id="ID of the saved emoji-statistics record to delete.")
    async def settings_delete_emoji(self, ctx: Context, id: int) -> None:
        """Delete one saved emoji-statistics record after confirmation."""
        await self._delete_item_command(ctx, "emoji", id)

    @settings_delete.command(name="downloads", aliases=("download",))
    @app_commands.describe(id="ID of the saved download record to delete.")
    async def settings_delete_downloads(self, ctx: Context, id: int) -> None:
        """Delete one saved download record after confirmation."""
        await self._delete_item_command(ctx, "downloads", id)

    @settings_delete.command(name="currency")
    @app_commands.describe(id="ID of the saved currency record to delete.")
    async def settings_delete_currency(self, ctx: Context, id: int) -> None:
        """Delete one saved currency record after confirmation."""
        await self._delete_item_command(ctx, "currency", id)

    @settings_delete_avatar.autocomplete("id")
    async def settings_delete_avatar_id_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        return await self._delete_id_choices(interaction, current, "avatar")

    @settings_delete_username.autocomplete("id")
    async def settings_delete_username_id_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        return await self._delete_id_choices(interaction, current, "username")

    @settings_delete_display.autocomplete("id")
    async def settings_delete_display_id_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        return await self._delete_id_choices(interaction, current, "display_name")

    @settings_delete_nickname.autocomplete("id")
    async def settings_delete_nickname_id_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        return await self._delete_id_choices(interaction, current, "nickname")

    @settings_delete_servertag.autocomplete("id")
    async def settings_delete_servertag_id_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        return await self._delete_id_choices(interaction, current, "servertag")

    @settings_delete_discriminator.autocomplete("id")
    async def settings_delete_discriminator_id_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        return await self._delete_id_choices(interaction, current, "discriminators")

    @settings_delete_joins.autocomplete("id")
    async def settings_delete_joins_id_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        return await self._delete_id_choices(interaction, current, "joins")

    @settings_delete_status.autocomplete("id")
    async def settings_delete_status_id_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        return await self._delete_id_choices(interaction, current, "status")

    @settings_delete_pokemon.autocomplete("id")
    async def settings_delete_pokemon_id_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        return await self._delete_id_choices(interaction, current, "pokemon")

    @settings_delete_corn.autocomplete("id")
    async def settings_delete_corn_id_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        return await self._delete_id_choices(interaction, current, "corn")

    @settings_delete_reaction.autocomplete("id")
    async def settings_delete_reaction_id_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        return await self._delete_id_choices(interaction, current, "reactions")

    @settings_delete_game.autocomplete("id")
    async def settings_delete_game_id_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        return await self._delete_id_choices(interaction, current, "games")

    @settings_delete_xp.autocomplete("id")
    async def settings_delete_xp_id_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        return await self._delete_id_choices(interaction, current, "xp")

    @settings_delete_commands.autocomplete("id")
    async def settings_delete_commands_id_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        return await self._delete_id_choices(interaction, current, "commands")

    @settings_delete_emoji.autocomplete("id")
    async def settings_delete_emoji_id_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        return await self._delete_id_choices(interaction, current, "emoji")

    @settings_delete_downloads.autocomplete("id")
    async def settings_delete_downloads_id_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        return await self._delete_id_choices(interaction, current, "downloads")

    @settings_delete_currency.autocomplete("id")
    async def settings_delete_currency_id_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        return await self._delete_id_choices(interaction, current, "currency")

    @settings.command(name="accounts")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def settings_accounts(self, ctx: Context) -> None:
        """Connect or manage your external accounts."""
        # Keep this view shared with the legacy ``fish accounts`` command so
        # both entry points always expose the same account actions.
        from extensions.settings import ManageAccountsView

        row = await self.bot.pool.fetchrow(
            "SELECT lastfm, steam, roblox, letterboxd, anilist FROM accounts "
            "WHERE user_id = $1",
            ctx.author.id,
        )
        view = ManageAccountsView(
            ctx,
            row=row,
            lastfm_connected=bool(row and row["lastfm"]),
            steam_connected=bool(row and row["steam"]),
            anilist_connected=bool(row and row["anilist"]),
        )
        await ctx.send(
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=ctx.interaction is not None,
        )

    @settings.command(name="anilist")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def settings_anilist(self, ctx: Context) -> None:
        """Choose whether AniList profiles open on anime, manga, or characters."""
        from extensions.anime import AniListSettingsView

        default_media = await self.bot.pool.fetchval(
            "SELECT anilist_default_media FROM user_settings WHERE user_id = $1",
            ctx.author.id,
        )
        if default_media not in {"anime", "manga", "characters"}:
            default_media = "anime"
        await ctx.send(
            view=AniListSettingsView(ctx, default_media),
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=ctx.interaction is not None,
        )

    @settings.group(name="server", invoke_without_command=True)
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def settings_server(self, ctx: GuildContext) -> None:
        """Manage server tracking and automation channels."""
        if ctx.invoked_subcommand is not None:
            return
        from .server import ServerSettingsView

        view = await ServerSettingsView.create(ctx)
        view.message = await ctx.send(
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=ctx.interaction is not None,
        )

    @settings_server.command(name="manage", extras={"usage": "<feature> [arguments]"})
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    @app_commands.describe(
        feature=(
            "Feature to manage (clownboard, pinboard, dehoist, custom-role, "
            "snipe, editsnipe, honeypot, or an automation setting)."
        ),
        arguments="Optional arguments for the selected feature.",
    )
    async def settings_server_manage(
        self,
        ctx: GuildContext,
        feature: str | None = None,
        *,
        arguments: str | None = None,
    ) -> None:
        """Manage server features from one guild-only command surface.

        Discord only permits one nested subcommand group.  ``settings server``
        is therefore a hybrid group and ``manage`` accepts the feature name as
        an argument, yielding ``/settings server manage feature:<name>`` while
        retaining the natural text form ``fish settings server manage <name>``.
        """
        if not feature:
            await self._send_server_settings_panel(ctx)
            return

        kind = feature.casefold().strip().replace("_", "-")
        # These settings already share the channel/media picker implementation
        # used by ``settings server`` and ``settings edit``.
        honeypot_action = (arguments or "").strip().casefold()
        if kind == "honeypot" and honeypot_action in {
            "setup",
            "remove",
            "delete",
            "disable",
        }:
            if not (
                ctx.author.guild_permissions.manage_channels
                and ctx.author.guild_permissions.ban_members
            ):
                raise commands.MissingPermissions(["manage_channels", "ban_members"])
            if ctx.guild.me is None or not (
                ctx.guild.me.guild_permissions.manage_channels
                and ctx.guild.me.guild_permissions.ban_members
            ):
                raise commands.BotMissingPermissions(["manage_channels", "ban_members"])
            moderation = self.bot.get_cog("Moderation")
            if moderation is None:
                raise commands.BadArgument(
                    "Moderation management is unavailable right now."
                )
            command = getattr(moderation, "honeypot", None)
            if honeypot_action in {"remove", "delete", "disable"}:
                command = getattr(moderation, "honeypot_remove", None)
            callback = getattr(command, "callback", None)
            if callback is None:
                raise commands.BadArgument(
                    "Honeypot management is unavailable right now."
                )
            await callback(moderation, ctx)
            return

        destination_kinds = {
            "auto-download": "auto_download",
            "auto-downloads": "auto_download",
            "auto-upload": "auto_upload",
            "auto-uploads": "auto_upload",
            "hourly-posts": "hourly_posts",
            "hourly": "hourly_posts",
            "auto-reactions": "auto_reactions",
            "auto-reaction": "auto_reactions",
            "reactions": "auto_reactions",
            "poketwo": "poketwo",
            "auto-solve": "poketwo",
            "autosolve": "poketwo",
            "honeypot": "honeypot",
        }
        if kind in destination_kinds:
            await self._edit_server_destination(ctx, destination_kinds[kind])
            return

        # The feature cogs expose their existing setup/info helpers.  Calling
        # those helpers keeps the text commands unchanged while providing an
        # app-command entry point under the server management group.
        if kind in {"clownboard", "starboard"}:
            boards = self.bot.get_cog("Boards") or self.bot.get_cog("Discord")
            send_board = getattr(boards, "_send_board", None)
            if send_board is None:
                raise commands.BadArgument("Board management is unavailable right now.")
            await send_board(ctx, kind)
            return

        if kind == "pinboard":
            if not ctx.author.guild_permissions.manage_channels:
                raise commands.MissingPermissions(["manage_channels"])
            pinboard = self.bot.get_cog("Pinboard") or self.bot.get_cog("Discord")
            command = getattr(pinboard, "pinboard", None)
            callback = getattr(command, "callback", None)
            if callback is None:
                raise commands.BadArgument(
                    "Pinboard management is unavailable right now."
                )
            await callback(pinboard, ctx)
            return

        moderation = self.bot.get_cog("Moderation")
        if moderation is None:
            raise commands.BadArgument(
                "Moderation management is unavailable right now."
            )

        if kind == "dehoist":
            callback = getattr(getattr(moderation, "dehoist", None), "callback", None)
            if callback is None:
                raise commands.BadArgument(
                    "Dehoist management is unavailable right now."
                )
            await callback(moderation, ctx)
            return

        if kind == "honeypot":
            if not (
                ctx.author.guild_permissions.manage_channels
                and ctx.author.guild_permissions.ban_members
            ):
                raise commands.MissingPermissions(["manage_channels", "ban_members"])
            action = honeypot_action
            command = getattr(moderation, "honeypot", None)
            if action in {"remove", "delete", "disable"}:
                command = getattr(moderation, "honeypot_remove", None)
            callback = getattr(command, "callback", None)
            if callback is None:
                raise commands.BadArgument(
                    "Honeypot management is unavailable right now."
                )
            await callback(moderation, ctx)
            return

        if kind in {"snipe", "editsnipe", "esnipe"}:
            base_name = kind if kind != "esnipe" else "editsnipe"
            command = getattr(moderation, base_name, None)
            action = (arguments or "").strip().casefold()
            if action in {"enable", "disable"}:
                command = getattr(moderation, f"{base_name}_{action}", None)
            callback = getattr(command, "callback", None)
            if callback is None:
                raise commands.BadArgument("Snipe management is unavailable right now.")
            await callback(moderation, ctx)
            return

        if kind in {
            "custom-role",
            "customrole",
            "booster-role",
            "boosterole",
            "user-role",
            "userrole",
        }:
            if not ctx.author.guild_permissions.manage_roles:
                raise commands.MissingPermissions(["manage_roles"])
            callback = getattr(
                getattr(moderation, "custom_role", None), "callback", None
            )
            if callback is None:
                raise commands.BadArgument(
                    "Custom-role management is unavailable right now."
                )
            # The detailed custom-role actions remain available through the
            # text command.  The app path opens its help/setup response rather
            # than attempting to bypass discord.py's converters.
            await callback(moderation, ctx)
            return

        raise commands.BadArgument(
            "Unknown feature. Choose clownboard, pinboard, dehoist, custom-role, "
            "snipe, editsnipe, honeypot, auto-downloads, auto-upload, "
            "hourly-posts, auto-reactions, or poketwo."
        )

    @settings_server_manage.autocomplete("feature")
    async def settings_server_manage_feature_autocomplete(
        self, _interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        features = (
            ("Clownboard", "clownboard"),
            ("Pinboard", "pinboard"),
            ("Dehoist", "dehoist"),
            ("Custom role", "custom-role"),
            ("Snipe", "snipe"),
            ("Edit snipe", "editsnipe"),
            ("Honeypot", "honeypot"),
            ("Auto-downloads", "auto-downloads"),
            ("Auto-upload", "auto-upload"),
            ("Hourly posts", "hourly-posts"),
            ("Auto-reactions", "auto-reactions"),
            ("Pokétwo", "poketwo"),
        )
        current = current.casefold()
        return [
            app_commands.Choice(name=label, value=value)
            for label, value in features
            if not current or current in label.casefold()
        ][:25]

    @settings_server.command(name="tracking")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def settings_server_tracking(self, ctx: GuildContext) -> None:
        """Manage tracking categories collected for this server."""
        row = await self.bot.pool.fetchrow(
            "SELECT tracking_enabled, history_public FROM guild_settings WHERE guild_id = $1",
            ctx.guild.id,
        )
        records = await self.bot.pool.fetchrow(
            "SELECT items FROM guild_opted_out WHERE guild_id = $1", ctx.guild.id
        )
        opted: set[str] = (
            {str(item) for item in (records["items"] or ())} if records else set()
        )
        await ctx.send(
            view=GuildTrackingSettingsView(
                ctx,
                tracking_enabled=bool(row["tracking_enabled"]) if row else True,
                history_public=bool(row["history_public"]) if row else True,
                opted_out=opted,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=ctx.interaction is not None,
        )

    @settings_server.command(name="delete")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    @app_commands.describe(category="Optional server-data category.")
    async def settings_server_delete(
        self, ctx: GuildContext, category: str | None = None
    ) -> None:
        """Explain how administrators delete saved server data."""
        if category:
            await self.settings_server_delete_all(ctx, category)
            return
        text = (
            "## Delete server data\n\n"
            "Use `settings server delete all` to review all saved server records, "
            "or specify a category such as `avatars`, `icons`, `names`, `joins`, "
            "`status`, `commands`, `emoji`, `downloads`, `corn`, `reactions`, "
            "`tags`, or `mudae`. Every deletion requires confirmation."
        )
        view = discord.ui.LayoutView(timeout=300)
        view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(text),
                accent_color=ctx.bot.embedcolor,
            )
        )
        await ctx.send(
            view=view,
            ephemeral=ctx.interaction is not None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @settings_server_delete.autocomplete("category")
    async def settings_server_delete_category_autocomplete(
        self, _interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return self._category_choices(current, _SERVER_DELETE_CATEGORY_OPTIONS)

    async def _server_delete_counts(
        self, guild_id: int, category: str | None
    ) -> dict[str, int]:
        categories = (
            (category,)
            if category
            else tuple(key for key in _SERVER_DELETE_SPECS if key != "all")
        )
        counts: dict[str, int] = {}
        for name in categories:
            total = 0
            for table, column in _SERVER_DELETE_SPECS[name]:
                total += int(
                    await self.bot.pool.fetchval(
                        f"SELECT COUNT(*) FROM {table} WHERE {column} = $1", guild_id
                    )
                    or 0
                )
            counts[name] = total
        return counts

    async def _delete_server_category(self, guild_id: int, category: str) -> int:
        deleted = 0
        for table, column in _SERVER_DELETE_SPECS[category]:
            result = await self.bot.pool.execute(
                f"DELETE FROM {table} WHERE {column} = $1", guild_id
            )
            try:
                deleted += int(str(result).rsplit(" ", 1)[-1])
            except (TypeError, ValueError):
                continue
        if category == "all":
            # Settings are configuration rather than history, but removing
            # them here leaves the server in a clean state and matches the
            # existing website's guild-data deletion endpoint.
            for table in (
                "guild_settings",
                "guild_opted_out",
                "guild_auto_reaction_channels",
                "guild_hourly_posts",
                "honeypot_channels",
                "guild_log_channels",
            ):
                try:
                    await self.bot.pool.execute(
                        f"DELETE FROM {table} WHERE guild_id = $1", guild_id
                    )
                except Exception:
                    continue
            self.bot.db_cache.set_guild_history_public(guild_id, True)
            self.bot.db_cache.guild_tracking_disabled.discard(guild_id)
        return deleted

    async def settings_server_delete_all(
        self, ctx: GuildContext, category: str | None = None
    ) -> None:
        """Review and delete all saved data for this server."""
        normalized = (category or "all").strip().casefold().replace("_", "-")
        if normalized not in _SERVER_DELETE_SPECS:
            raise commands.BadArgument(
                "Unknown server-data category. Use avatars, icons, names, joins, "
                "status, commands, emoji, downloads, corn, reactions, tags, or mudae."
            )
        if normalized == "all":
            counts = await self._server_delete_counts(ctx.guild.id, None)
        else:
            counts = await self._server_delete_counts(ctx.guild.id, normalized)
        nonzero = {key: count for key, count in counts.items() if count}
        if not nonzero:
            await ctx.send(
                "There is no saved server data to delete.",
                ephemeral=ctx.interaction is not None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        lines = [f"**{key.title()}:** {count:,}" for key, count in nonzero.items()]
        prompt = await ctx.prompt(
            f"This permanently deletes saved data for **{ctx.guild.name}**. "
            "**This cannot be undone.**\n\n" + "\n".join(lines),
            ephemeral=ctx.interaction is not None,
            delete_after=False,
        )
        if not prompt:
            await ctx.send("No server data was deleted.", ephemeral=True)
            return
        # The confirmation view can stay open while the administrator's
        # permissions change. Re-check the live member before any destructive
        # database operation rather than relying only on the command decorator.
        get_member = getattr(ctx.guild, "get_member", None)
        member = get_member(ctx.author.id) if callable(get_member) else None
        if not member or not (
            getattr(ctx.guild, "owner_id", None) == member.id
            or bool(
                getattr(
                    getattr(member, "guild_permissions", None), "manage_guild", False
                )
            )
        ):
            await prompt.edit(
                content="You no longer have Manage Server permission; no data was deleted.",
                view=None,
            )
            return
        await prompt.edit(content="Deleting saved server data…", view=None)
        removed = 0
        if normalized == "all":
            for key in _SERVER_DELETE_SPECS:
                if key != "all":
                    removed += await self._delete_server_category(ctx.guild.id, key)
            removed += await self._delete_server_category(ctx.guild.id, "all")
        else:
            removed = await self._delete_server_category(ctx.guild.id, normalized)
        await prompt.edit(content=f"Deleted {removed:,} saved server record(s).")

    @settings.command(
        name="hourly-posts",
        aliases=("hourly", "hourlyposts", "autoposts"),
        with_app_command=False,
    )
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def settings_hourly_posts(self, ctx: GuildContext) -> None:
        """Set up hourly library posts, media filters, and repeat interval."""
        await self._edit_server_destination(ctx, "hourly_posts")

    @settings.command(name="honeypot", aliases=("honey-pot",), with_app_command=False)
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def settings_honeypot(self, ctx: GuildContext) -> None:
        """Set up or edit the honeypot channel and warning message."""
        await self._edit_server_destination(ctx, "honeypot")

    @settings.command(
        name="auto-reactions",
        aliases=("auto-reaction", "reactions", "autoreactions"),
        with_app_command=False,
    )
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def settings_auto_reactions(self, ctx: GuildContext) -> None:
        """Enable, disable, or scope automatic reactions to selected channels."""
        await self._edit_server_destination(ctx, "auto_reactions")

    @settings.group(name="edit", invoke_without_command=True, with_app_command=False)
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def settings_edit(self, ctx: GuildContext) -> None:
        """Edit a server automation destination or its filters."""
        await self._send_server_settings_panel(ctx)

    async def _send_server_settings_panel(self, ctx: GuildContext) -> None:
        from .server import ServerSettingsView

        view = await ServerSettingsView.create(ctx)
        view.message = await ctx.send(
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=ctx.interaction is not None,
        )

    async def _edit_server_destination(self, ctx: GuildContext, kind: str) -> None:
        from .server import (
            ServerSettingsView,
            _AutoReactionChannelsView,
            _ServerChannelPicker,
            _ServerMediaView,
        )

        view = await ServerSettingsView.create(ctx)
        if kind == "auto_reactions":
            target: AuthorLayoutView = _AutoReactionChannelsView(view)
        elif kind in {"auto_upload", "hourly_posts"} and view.values.get(kind):
            target: AuthorLayoutView = _ServerMediaView(view, kind)
        else:
            target = _ServerChannelPicker(view, kind)
        target.message = await ctx.send(
            view=target,
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=ctx.interaction is not None,
        )

    @settings_edit.command(
        name="auto-downloads",
        aliases=("auto-download", "autodownload"),
        with_app_command=False,
    )
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def settings_edit_auto_downloads(self, ctx: GuildContext) -> None:
        """Set, move, or disable the automatic download channel."""
        await self._edit_server_destination(ctx, "auto_download")

    @settings_edit.command(
        name="auto-upload",
        aliases=("auto-uploads", "autoupload", "uploads"),
        with_app_command=False,
    )
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def settings_edit_auto_upload(self, ctx: GuildContext) -> None:
        """Set, move, or filter the automatic upload channel."""
        await self._edit_server_destination(ctx, "auto_upload")

    @settings_edit.command(
        name="hourly-posts",
        aliases=("hourly", "hourlyposts", "autoposts"),
        with_app_command=False,
    )
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def settings_edit_hourly_posts(self, ctx: GuildContext) -> None:
        """Set, move, or edit the hourly-post interval and media filters."""
        await self._edit_server_destination(ctx, "hourly_posts")

    @settings_edit.command(name="pinboard", with_app_command=False)
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def settings_edit_pinboard(self, ctx: GuildContext) -> None:
        """Set, move, or disable the pinboard channel."""
        await self._edit_server_destination(ctx, "pinboard")

    @settings_edit.command(
        name="honeypot", aliases=("honey-pot",), with_app_command=False
    )
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def settings_edit_honeypot(self, ctx: GuildContext) -> None:
        """Set, move, or disable the honeypot channel."""
        await self._edit_server_destination(ctx, "honeypot")

    @settings_edit.command(
        name="auto-reactions",
        aliases=("auto-reaction", "reactions"),
        with_app_command=False,
    )
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def settings_edit_auto_reactions(self, ctx: GuildContext) -> None:
        """Set, move, or disable the automatic reaction channel."""
        await self._edit_server_destination(ctx, "auto_reactions")

    @settings_edit.command(
        name="poketwo",
        aliases=("auto-solve", "autosolve"),
        with_app_command=False,
    )
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def settings_edit_poketwo(self, ctx: GuildContext) -> None:
        """Set, move, or disable the Pokétwo auto-solving channel."""
        await self._edit_server_destination(ctx, "poketwo")

    @commands.hybrid_group(
        name="tracking",
        aliases=("logging",),
        fallback="user",
    )
    async def logging(self, ctx: Context):
        """Manage your personal tracking settings for the bot."""
        await ctx.send(
            view=await self.privacy_settings_view(ctx),
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=ctx.interaction is not None,
        )

    @logging.command(name="reactions", aliases=("reaction",))
    async def logging_reactions(self, ctx: Context) -> None:
        """Enable or disable opt-in reaction history tracking."""
        enabled = self.bot.db_cache.reaction_tracking_enabled(ctx.author.id)
        await ctx.send(
            view=ReactionTrackingView(ctx, enabled),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @logging.command(name="guild", aliases=("server",))
    @commands.guild_only()
    @commands.has_guild_permissions(administrator=True)
    async def logging_server(self, ctx: GuildContext):
        """Manage tracking for the server."""
        sql = """SELECT * FROM guild_opted_out WHERE guild_id = $1"""
        records = await self.bot.pool.fetchrow(sql, ctx.guild.id)
        data = {
            "name": ["Name tracking", "\U0001f7e2"],
            "icon": ["Icon tracking", "\U0001f7e2"],
            "emoji": ["Emoji statistics tracking", "\U0001f7e2"],
        }

        if bool(records):
            for item in records["items"]:
                if data.get(item):
                    data.update({item: [data[item][0], "\U0001f534"]})

        await ctx.send(
            view=DropdownView(
                ctx,
                data,
                guild_id=ctx.guild.id,
                intro=(
                    "## Server tracking settings\n"
                    "Choose a category to enable or disable its tracking.\n\n"
                    "Manage these settings on the [website](https://crygup.com/discord?tab=settings)."
                ),
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_group(
        name="tracking-delete",
        aliases=("logging-delete",),
        fallback="info",
        hidden=True,
        with_app_command=False,
    )
    async def logging_delete(self, ctx: Context, data: str | None = None):
        """Legacy text-only alias for ``settings delete``.

        The app command was replaced by ``/settings delete``; keeping this
        hidden text group avoids breaking old bookmarks while ensuring there
        is no duplicate top-level slash command.
        """
        await ctx.send(
            "Please use `settings delete` to manage or delete saved tracking data.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @logging_delete.command(name="reactions", aliases=("reaction",), hidden=True)
    @app_commands.describe(emoji="Unicode emoji, custom emoji, or custom emoji ID.")
    async def logging_delete_reactions(self, ctx: Context, emoji: str) -> None:
        """Delete your saved reaction rows for one emoji."""
        emoji_id: int | None = None
        is_unicode = True
        emoji_name = emoji.strip()
        match = re.fullmatch(r"<a?:([A-Za-z0-9_~]+):(\d+)>", emoji_name)
        if match:
            emoji_name = match.group(1)
            emoji_id = int(match.group(2))
            is_unicode = False
        elif emoji_name.isdigit():
            emoji_id = int(emoji_name)
            is_unicode = False
            emoji_name = ""
        elif ctx.guild is not None:
            for candidate in ctx.guild.emojis:
                if (
                    candidate.name
                    and candidate.name.casefold() == emoji_name.casefold()
                ):
                    emoji_name = candidate.name
                    emoji_id = candidate.id
                    is_unicode = False
                    break

        label = emoji_name or str(emoji_id)
        msg = await ctx.prompt(
            f"Delete all of your saved reaction logs for {label}? **THIS CANNOT BE UNDONE**",
            ephemeral=True,
            delete_after=False,
        )
        if not msg:
            await ctx.send("Good choice.", ephemeral=True)
            return

        await msg.edit(content="Okay, deleting those reaction logs.", view=None)
        if emoji_id is None:
            deleted = await self.bot.pool.execute(
                "DELETE FROM reaction_logs WHERE (giver_id = $1 OR receiver_id = $1) "
                "AND emoji_name = $2 AND unicode = $3",
                ctx.author.id,
                emoji_name,
                is_unicode,
            )
        else:
            deleted = await self.bot.pool.execute(
                "DELETE FROM reaction_logs WHERE (giver_id = $1 OR receiver_id = $1) "
                "AND emoji_id = $2 AND unicode = FALSE",
                ctx.author.id,
                emoji_id,
            )
        count = deleted.rsplit(" ", 1)[-1]
        await msg.edit(content=f"Deleted {count} reaction log(s) for {label}.")

    async def easy_delete(
        self,
        message: discord.Message,
        prompt: Union[
            Literal["avatars"],
            Literal["guild_avatars"],
            Literal["username_logs"],
            Literal["display_name_logs"],
            Literal["stag_logs"],
            Literal["nickname_logs"],
            Literal["discrim_logs"],
            Literal["member_join_logs"],
            Literal["tags"],
            Literal["emoji_stats"],
        ],
        id: int,
        author_id: int,
    ):
        await message.edit(content=f"Deleting ID `{id}`", attachments=[], view=None)

        sql = f"""DELETE FROM {prompt} WHERE id = $1 AND {format_table[prompt]} = $2 RETURNING *"""
        results = await self.bot.pool.fetch(sql, id, author_id)

        if not bool(results):
            return await message.edit(
                content="Nothing was deleted. Maybe try a different ID."
            )

        await message.edit(content=f"Deleted ID `{id}`")

    @logging_delete.command(name="avatar", hidden=True)
    @interaction_only()
    async def ldelete_avatar(self, ctx: GuildContext, id: int, guild: bool = False):
        """Delete a saved avatar."""

        table = ["avatars", "guild_avatars"][guild]
        user = ["user_id", "member_id"][guild]
        avatar = await self.bot.pool.fetchval(
            f"SELECT avatar FROM {table} WHERE {user} = $1 AND id = $2",
            ctx.author.id,
            id,
        )

        message = await ctx.prompt(
            f"This is your avatar with the ID `{id}`. Are you sure you want to delete this?",
            ephemeral=True,
            delete_after=False,
            file=discord.File(await to_image(ctx.session, avatar), "avatar.png"),
        )

        if not message:
            return await ctx.send("Good choice.", ephemeral=True)

        await self.easy_delete(
            message, "guild_avatars" if guild else "avatars", id, ctx.author.id
        )

    @logging_delete.command(name="username", hidden=True)
    @interaction_only()
    async def ldelete_username(
        self, ctx: GuildContext, id: int = commands.param(displayed_name="username")
    ):
        """Delete a saved username."""

        name = await self.bot.pool.fetchval(
            "SELECT username FROM username_logs WHERE user_id = $1 AND id = $2",
            ctx.author.id,
            id,
        )

        message = await ctx.prompt(
            f"`{name}` is the named saved with the ID `{id}`. Are you sure you want to delete this?",
            ephemeral=True,
            delete_after=False,
        )

        if not message:
            return await ctx.send("Good choice.", ephemeral=True)

        await self.easy_delete(message, "username_logs", id, ctx.author.id)

    @ldelete_username.autocomplete("id")
    async def ldu_ac(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[int]]:
        usernames = await self.bot.pool.fetch(
            "SELECT * FROM username_logs WHERE user_id = $1", interaction.user.id
        )

        return [
            app_commands.Choice(name=username["username"], value=username["id"])
            for username in usernames
            if current.lower() in str(username["username"]).lower()
        ]

    @logging_delete.command(name="nickname", hidden=True)
    @interaction_only()
    async def ldelete_nickname(
        self, ctx: GuildContext, id: int = commands.param(displayed_name="nickname")
    ):
        """Delete a saved nickname."""

        name = await self.bot.pool.fetchval(
            "SELECT nickname FROM nickname_logs WHERE user_id = $1 AND id = $2",
            ctx.author.id,
            id,
        )

        message = await ctx.prompt(
            f"`{name}` is the nickname saved with the ID `{id}`. Are you sure you want to delete this?",
            ephemeral=True,
            delete_after=False,
        )

        if not message:
            return await ctx.send("Good choice.", ephemeral=True)

        await self.easy_delete(message, "nickname_logs", id, ctx.author.id)

    @ldelete_nickname.autocomplete("id")
    async def ldn_ac(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[int]]:
        nicknames = await self.bot.pool.fetch(
            "SELECT * FROM nickname_logs WHERE user_id = $1", interaction.user.id
        )

        return [
            app_commands.Choice(
                name=f"{nick['nickname']} - {nick['id']}", value=nick["id"]
            )
            for nick in nicknames
            if current.lower() in str(nick["nickname"]).lower()
        ]

    @logging_delete.command(name="display_name", hidden=True)
    @interaction_only()
    async def ldelete_display(
        self, ctx: GuildContext, id: int = commands.param(displayed_name="display_name")
    ):
        """Delete a saved display name."""

        name = await self.bot.pool.fetchval(
            "SELECT display_name FROM display_name_logs WHERE user_id = $1 AND id = $2",
            ctx.author.id,
            id,
        )

        message = await ctx.prompt(
            f"`{name}` is the display name saved with the ID `{id}`. Are you sure you want to delete this?",
            ephemeral=True,
            delete_after=False,
        )

        if not message:
            return await ctx.send("Good choice.", ephemeral=True)

        await self.easy_delete(message, "display_name_logs", id, ctx.author.id)

    @ldelete_display.autocomplete("id")
    async def lddn_ac(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[int]]:
        display_names = await self.bot.pool.fetch(
            "SELECT * FROM display_name_logs WHERE user_id = $1", interaction.user.id
        )

        return [
            app_commands.Choice(
                name=f"{dname['display_name']} - {dname['id']}", value=dname["id"]
            )
            for dname in display_names
            if current.lower() in str(dname["display_name"]).lower()
        ]

    @logging_delete.command(name="servertag", aliases=("stag", "tag"), hidden=True)
    @interaction_only()
    async def ldelete_server_tag(
        self,
        ctx: GuildContext,
        id: int = commands.param(displayed_name="server_tag"),
    ):
        """Delete a saved primary server tag."""

        record = await self.bot.pool.fetchrow(
            "SELECT tag FROM stag_logs WHERE user_id = $1 AND id = $2",
            ctx.author.id,
            id,
        )
        if record is None:
            return await ctx.send(
                "No server tag record found with that ID.", ephemeral=True
            )

        tag = record["tag"] or "No server tag"
        message = await ctx.prompt(
            f"`{tag}` is the server tag saved with the ID `{id}`. "
            "Are you sure you want to delete it?",
            ephemeral=True,
            delete_after=False,
        )

        if not message:
            return await ctx.send("Good choice.", ephemeral=True)

        await self.easy_delete(message, "stag_logs", id, ctx.author.id)

    @ldelete_server_tag.autocomplete("id")
    async def ldst_ac(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[int]]:
        tags = await self.bot.pool.fetch(
            "SELECT id, tag FROM stag_logs "
            "WHERE user_id = $1 ORDER BY created_at DESC LIMIT 25",
            interaction.user.id,
        )

        return [
            app_commands.Choice(
                name=f"{tag['tag'] or 'No server tag'} - {tag['id']}"[:100],
                value=tag["id"],
            )
            for tag in tags
            if current.lower() in str(tag["tag"] or "No server tag").lower()
        ]

    @logging_delete.command(name="discriminators", hidden=True)
    @interaction_only()
    async def ldelete_discrim(
        self,
        ctx: GuildContext,
        id: int = commands.param(displayed_name="discriminator"),
    ):
        """Delete a saved discriminator."""

        discrim = await self.bot.pool.fetchval(
            "SELECT discrim FROM discrim_logs WHERE user_id = $1 AND id = $2",
            ctx.author.id,
            id,
        )

        message = await ctx.prompt(
            f"`{discrim}` is the discriminator saved with the ID `{id}`. Are you sure you want to delete this?",
            ephemeral=True,
            delete_after=False,
        )

        if not message:
            return await ctx.send("Good choice.", ephemeral=True)

        await self.easy_delete(message, "discrim_logs", id, ctx.author.id)

    @ldelete_discrim.autocomplete("id")
    async def ldd_ac(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[int]]:
        discrims = await self.bot.pool.fetch(
            "SELECT * FROM discrim_logs WHERE user_id = $1", interaction.user.id
        )

        return [
            app_commands.Choice(
                name=f"#{discrim['discrim']} | {discrim['id']}", value=discrim["id"]
            )
            for discrim in discrims
            if current.lower() in str(discrim["discrim"]).lower()
        ]

    @logging_delete.command(name="joins", hidden=True)
    @interaction_only()
    async def ldelete_join(
        self,
        ctx: GuildContext,
        id: int = commands.param(displayed_name="join"),
    ):
        """Delete a saved join record."""

        record = await self.bot.pool.fetchrow(
            "SELECT guild_id, time FROM member_join_logs WHERE member_id = $1 AND id = $2",
            ctx.author.id,
            id,
        )

        if not record:
            return await ctx.send("No join record found with that ID.", ephemeral=True)

        guild = self.bot.get_guild(record["guild_id"])
        guild_name = guild.name if guild else f"guild {record['guild_id']}"
        joined = discord.utils.format_dt(record["time"], "R")

        message = await ctx.prompt(
            f"You joined **{guild_name}** {joined}. Are you sure you want to delete this record? (ID `{id}`)",
            ephemeral=True,
            delete_after=False,
        )

        if not message:
            return await ctx.send("Good choice.", ephemeral=True)

        await self.easy_delete(message, "member_join_logs", id, ctx.author.id)

    @ldelete_join.autocomplete("id")
    async def ldj_ac(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[int]]:
        joins = await self.bot.pool.fetch(
            "SELECT * FROM member_join_logs WHERE member_id = $1 ORDER BY time DESC LIMIT 25",
            interaction.user.id,
        )

        choices: List[app_commands.Choice[int]] = []
        for join in joins:
            guild = self.bot.get_guild(join["guild_id"])
            guild_label = guild.name if guild else str(join["guild_id"])
            label = f"{guild_label} • {discord.utils.format_dt(join['time'], 'R')}"
            if current.lower() in label.lower():
                choices.append(app_commands.Choice(name=label[:100], value=join["id"]))

        return choices
