from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import discord
from discord import app_commands
from discord.ext import commands

from utils import lastfm_command
from utils.converters import normalize_letterboxd

from .logging import Logging
from .server import Server

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


LASTFM_CALLBACK_URL = "https://crygup.com/fishie"
LASTFM_STATE_TTL = 10 * 60
STEAM_CALLBACK_URL = "https://crygup.com/fishie"
STEAM_STATE_TTL = 10 * 60
ANILIST_CALLBACK_URL = "https://crygup.com/fishie"
ANILIST_STATE_TTL = 10 * 60


def _oauth_state_secret(bot: Fishie) -> bytes:
    """Use the existing server-side signing secret for account-link states."""
    return bot.config["keys"]["lastfm_secret"].encode()


def _lastfm_authorization_url(
    bot: Fishie,
    user_id: int,
    *,
    channel_id: int | None = None,
    message_id: int | None = None,
) -> str:
    state_data = {
        "user_id": str(user_id),
        "source": "discord",
        "expires": int(time.time()) + LASTFM_STATE_TTL,
    }
    if channel_id is not None and message_id is not None:
        state_data["channel_id"] = str(channel_id)
        state_data["message_id"] = str(message_id)
    payload = json.dumps(
        state_data,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(
        _oauth_state_secret(bot), encoded.encode(), hashlib.sha256
    ).hexdigest()
    state = f"{encoded}.{signature}"
    callback = f"{LASTFM_CALLBACK_URL}?{urlencode({'lastfm_state': state})}"
    return "https://www.last.fm/api/auth/?" + urlencode(
        {"api_key": bot.config["keys"]["lastfm_cb"], "cb": callback}
    )


def _steam_authorization_url(
    bot: Fishie,
    user_id: int,
    *,
    channel_id: int | None = None,
    message_id: int | None = None,
) -> str:
    now = int(time.time())
    states = getattr(bot, "_steam_oauth_states", None)
    if states is None:
        states = bot._steam_oauth_states = {}
    for token, state_data in list(states.items()):
        if int(state_data.get("expires", 0)) < now:
            states.pop(token, None)
    token = secrets.token_urlsafe(24)
    state_data = {
        "user_id": int(user_id),
        "source": "discord",
        "expires": now + STEAM_STATE_TTL,
    }
    if channel_id is not None and message_id is not None:
        state_data["channel_id"] = int(channel_id)
        state_data["message_id"] = int(message_id)
    states[token] = state_data
    callback = f"{STEAM_CALLBACK_URL}?{urlencode({'steam_state': token})}"
    return "https://steamcommunity.com/openid/login?" + urlencode(
        {
            "openid.ns": "http://specs.openid.net/auth/2.0",
            "openid.mode": "checkid_setup",
            "openid.return_to": callback,
            "openid.realm": "https://crygup.com/",
            "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
            "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
        }
    )


class SettingsLinkView(discord.ui.LayoutView):
    """Components V2 authorization prompt used by account settings."""

    def __init__(self, prompt: str, label: str, url: str) -> None:
        super().__init__(timeout=10 * 60)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(f"## Connected accounts\n{prompt}"),
                discord.ui.ActionRow(
                    discord.ui.Button(
                        label=label,
                        style=discord.ButtonStyle.link,
                        url=url,
                    )
                ),
            )
        )


def _lastfm_link_view(url: str) -> SettingsLinkView:
    return SettingsLinkView(
        "Authorize Fishie on Last.fm to connect your account.",
        "Authorize on Last.fm",
        url,
    )


def _steam_link_view(url: str) -> SettingsLinkView:
    return SettingsLinkView(
        "Authorize Fishie on Steam to connect your account.",
        "Authorize on Steam",
        url,
    )


def _anilist_link_view(url: str) -> SettingsLinkView:
    return SettingsLinkView(
        "Authorize Fishie on AniList to connect your account.",
        "Authorize on AniList",
        url,
    )


async def _send_lastfm_link(ctx: Context, interaction: discord.Interaction) -> None:
    message = interaction.message
    can_refresh = bool(message and not message.flags.ephemeral)
    url = _lastfm_authorization_url(
        ctx.bot,
        interaction.user.id,
        channel_id=message.channel.id if message and can_refresh else None,
        message_id=message.id if message and can_refresh else None,
    )
    await interaction.response.send_message(
        view=_lastfm_link_view(url),
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


def _anilist_authorization_url(
    bot: Fishie,
    user_id: int,
    *,
    channel_id: int | None = None,
    message_id: int | None = None,
) -> str:
    now = int(time.time())
    states = getattr(bot, "_anilist_oauth_states", None)
    if states is None:
        states = bot._anilist_oauth_states = {}
    for token, state_data in list(states.items()):
        if int(state_data.get("expires", 0)) < now:
            states.pop(token, None)
    token = secrets.token_urlsafe(24)
    state_data = {
        "user_id": int(user_id),
        "source": "discord",
        "expires": now + ANILIST_STATE_TTL,
    }
    if channel_id is not None and message_id is not None:
        state_data["channel_id"] = int(channel_id)
        state_data["message_id"] = int(message_id)
    states[token] = state_data
    return "https://anilist.co/api/v2/oauth/authorize?" + urlencode(
        {
            "client_id": bot.config["keys"]["anilist_id"],
            "redirect_uri": ANILIST_CALLBACK_URL,
            "response_type": "code",
            "state": f"anilist_{token}",
        }
    )


async def _send_steam_link(ctx: Context, interaction: discord.Interaction) -> None:
    message = interaction.message
    can_refresh = bool(message and not message.flags.ephemeral)
    url = _steam_authorization_url(
        ctx.bot,
        interaction.user.id,
        channel_id=message.channel.id if message and can_refresh else None,
        message_id=message.id if message and can_refresh else None,
    )
    await interaction.response.send_message(
        view=_steam_link_view(url),
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def _send_anilist_link(ctx: Context, interaction: discord.Interaction) -> None:
    message = interaction.message
    can_refresh = bool(message and not message.flags.ephemeral)
    url = _anilist_authorization_url(
        ctx.bot,
        interaction.user.id,
        channel_id=message.channel.id if message and can_refresh else None,
        message_id=message.id if message and can_refresh else None,
    )
    await interaction.response.send_message(
        view=_anilist_link_view(url),
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def _disconnect_lastfm(bot: Fishie, user_id: int) -> None:
    await bot.pool.execute(
        "UPDATE accounts SET lastfm = NULL, lastfm_session_key = NULL WHERE user_id = $1",
        user_id,
    )
    await bot.refresh_account_cache(user_id)


async def _disconnect_steam(bot: Fishie, user_id: int) -> None:
    await bot.pool.execute(
        "UPDATE accounts SET steam = NULL WHERE user_id = $1",
        user_id,
    )


async def _disconnect_anilist(bot: Fishie, user_id: int) -> None:
    await bot.pool.execute(
        "UPDATE accounts SET anilist = NULL, anilist_access_token = NULL "
        "WHERE user_id = $1",
        user_id,
    )
    await bot.refresh_account_cache(user_id)


def _accounts_text(row) -> str:
    labels = {
        "lastfm": "Last.fm",
        "steam": "Steam",
        "roblox": "Roblox",
        "letterboxd": "Letterboxd",
        "anilist": "AniList",
    }
    lines = []
    for col, label in labels.items():
        value = row[col] if row else None
        lines.append(f"{'✅' if value else '❌'} **{label}**")
    return "## Connected Accounts\n\n" + "\n".join(lines)


class Settings(Logging, Server):
    """User and server settings"""

    emoji = discord.PartialEmoji(name="\U00002699\U0000fe0f")

    def __init__(self, bot: Fishie):
        super().__init__()
        self.bot = bot

    @commands.command(name="accounts")
    async def accounts(self, ctx: Context):
        """View and manage your connected accounts"""
        row = await self.bot.pool.fetchrow(
            "SELECT lastfm, steam, roblox, letterboxd, anilist FROM accounts "
            "WHERE user_id = $1",
            ctx.author.id,
        )
        lastfm_connected = bool(row and row["lastfm"])
        steam_connected = bool(row and row["steam"])
        anilist_connected = bool(row and row["anilist"])
        view = ManageAccountsView(
            ctx,
            row=row,
            lastfm_connected=lastfm_connected,
            steam_connected=steam_connected,
            anilist_connected=anilist_connected,
        )
        await ctx.send(
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class ManageAccountsView(discord.ui.LayoutView):
    def __init__(
        self,
        ctx: Context,
        *,
        row=None,
        lastfm_connected: bool,
        steam_connected: bool = False,
        anilist_connected: bool = False,
    ):
        super().__init__(timeout=120)
        self.ctx = ctx
        self.row = row
        self.lastfm_connected = lastfm_connected
        self.steam_connected = steam_connected
        self.anilist_connected = anilist_connected

        self.status = discord.ui.TextDisplay(_accounts_text(row))
        self.connect_lastfm = discord.ui.Button()
        self.connect_steam = discord.ui.Button()
        self.connect_anilist = discord.ui.Button()
        self.manage = discord.ui.Button(label="Manage Other Accounts")
        self.connect_lastfm.callback = self._on_lastfm
        self.connect_steam.callback = self._on_steam
        self.connect_anilist.callback = self._on_anilist
        self.manage.callback = self._on_manage
        self._update_buttons()

        actions = discord.ui.ActionRow(
            self.connect_lastfm,
            self.connect_steam,
            self.connect_anilist,
            self.manage,
        )
        self.container = discord.ui.Container(
            self.status,
            actions,
            accent_color=self.ctx.bot.embedcolor,
        )
        self.add_item(self.container)

    def _update_buttons(self) -> None:
        for button, label, connected in (
            (self.connect_lastfm, "Last.fm", self.lastfm_connected),
            (self.connect_steam, "Steam", self.steam_connected),
            (self.connect_anilist, "AniList", self.anilist_connected),
        ):
            button.label = f"Disconnect {label}" if connected else f"Connect {label}"
            button.style = (
                discord.ButtonStyle.red if connected else discord.ButtonStyle.green
            )

    def update_accounts(self, row) -> None:
        self.row = row
        self.lastfm_connected = bool(row and row["lastfm"])
        self.steam_connected = bool(row and row["steam"])
        self.anilist_connected = bool(row and row["anilist"])
        self.status.content = _accounts_text(row)
        self._update_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            "Run the accounts command yourself to manage your accounts.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    async def _on_lastfm(self, interaction: discord.Interaction):
        if self.lastfm_connected:
            await _disconnect_lastfm(self.ctx.bot, interaction.user.id)
            row = await self._fetch_accounts(interaction.user.id)
            self.update_accounts(row)
            await interaction.response.edit_message(
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await interaction.followup.send(
                "Your Last.fm account has been disconnected.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await _send_lastfm_link(self.ctx, interaction)

    async def _on_steam(self, interaction: discord.Interaction):
        if self.steam_connected:
            await _disconnect_steam(self.ctx.bot, interaction.user.id)
            row = await self._fetch_accounts(interaction.user.id)
            self.update_accounts(row)
            await interaction.response.edit_message(
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await interaction.followup.send(
                "Your Steam account has been disconnected.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await _send_steam_link(self.ctx, interaction)

    async def _on_anilist(self, interaction: discord.Interaction):
        if self.anilist_connected:
            await _disconnect_anilist(self.ctx.bot, interaction.user.id)
            row = await self._fetch_accounts(interaction.user.id)
            self.update_accounts(row)
            await interaction.response.edit_message(
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await interaction.followup.send(
                "Your AniList account has been disconnected.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await _send_anilist_link(self.ctx, interaction)

    async def _fetch_accounts(self, user_id: int):
        return await self.ctx.bot.pool.fetchrow(
            "SELECT lastfm, steam, roblox, letterboxd, anilist FROM accounts WHERE user_id = $1",
            user_id,
        )

    async def _on_manage(self, interaction: discord.Interaction):
        row = await self.ctx.bot.pool.fetchrow(
            "SELECT roblox, letterboxd FROM accounts WHERE user_id = $1",
            interaction.user.id,
        )
        current = {col: row[col] for col in ("roblox", "letterboxd")} if row else {}
        await interaction.response.send_modal(
            ManageAccountsModal(self.ctx, current, self)
        )


class ManageAccountsModal(discord.ui.Modal, title="Manage Other Accounts"):
    roblox = discord.ui.TextInput(
        label="Roblox", required=False, max_length=100, placeholder="Clear to unlink"
    )
    letterboxd = discord.ui.TextInput(
        label="Letterboxd",
        required=False,
        max_length=100,
        placeholder="Username, profile URL, or boxd.it link (clear to unlink)",
    )

    def __init__(self, ctx: Context, current: dict, view: ManageAccountsView):
        super().__init__()
        self.ctx = ctx
        self._current = current
        self._view = view
        self.roblox.default = current.get("roblox", "")
        self.letterboxd.default = current.get("letterboxd", "")

    async def on_submit(self, interaction):
        # Resolving a boxd.it link may require a network request, so acknowledge
        # the modal before validating the supplied account values.
        await interaction.response.defer(ephemeral=True)
        added = []
        removed = []
        COLS = {
            "roblox": self.roblox,
            "letterboxd": self.letterboxd,
        }
        for col, field in COLS.items():
            v = (field.value or "").strip()
            was_set = bool(self._current.get(col))
            if v:
                if col == "letterboxd":
                    try:
                        v = await normalize_letterboxd(self.ctx, v)
                    except commands.BadArgument as error:
                        await interaction.followup.send(str(error), ephemeral=True)
                        return
                await self.ctx.bot.pool.execute(
                    f'INSERT INTO accounts (user_id, "{col}") VALUES ($1, $2) '
                    f'ON CONFLICT (user_id) DO UPDATE SET "{col}" = $2',
                    interaction.user.id,
                    v,
                )
                added.append(f"{col}: {v}")
            elif was_set:
                await self.ctx.bot.pool.execute(
                    f'UPDATE accounts SET "{col}" = NULL WHERE user_id = $1',
                    interaction.user.id,
                )
                removed.append(col)
        msg = []
        if added:
            msg.append("Linked: " + ", ".join(added))
        if removed:
            msg.append("Unlinked: " + ", ".join(removed))

        # Update the original Components V2 account panel.
        row = await self.ctx.bot.pool.fetchrow(
            "SELECT lastfm, steam, roblox, letterboxd, anilist FROM accounts "
            "WHERE user_id = $1",
            interaction.user.id,
        )
        self._view.update_accounts(row)
        if interaction.message:
            await interaction.message.edit(
                view=self._view,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        await interaction.followup.send(
            "\n".join(msg) if msg else "No changes made.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: Fishie):
    await bot.add_cog(Settings(bot))
