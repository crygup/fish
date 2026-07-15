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
from discord.ext import commands
from discord import app_commands

from .logging import Logging
from .server import Server
from utils import lastfm_command
from utils.regexes import LBD_URL_RE

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


LASTFM_CALLBACK_URL = "https://crygup.com/fishie"
LASTFM_STATE_TTL = 10 * 60
STEAM_CALLBACK_URL = "https://crygup.com/fishie"
STEAM_STATE_TTL = 10 * 60
SPOTIFY_CALLBACK_URL = "https://crygup.com/fishie"
SPOTIFY_STATE_TTL = 10 * 60
SPOTIFY_SCOPES = (
    "user-read-private user-read-playback-state user-modify-playback-state "
    "user-library-modify user-library-read"
)


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


def _lastfm_link_view(url: str) -> discord.ui.View:
    view = discord.ui.View(timeout=10 * 60)
    view.add_item(
        discord.ui.Button(
            label="Authorize on Last.fm",
            style=discord.ButtonStyle.link,
            url=url,
        )
    )
    return view


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
        "Authorize Fishie on Last.fm to connect your account.",
        view=_lastfm_link_view(url),
        ephemeral=True,
    )


def _steam_link_view(url: str) -> discord.ui.View:
    view = discord.ui.View(timeout=10 * 60)
    view.add_item(
        discord.ui.Button(
            label="Authorize on Steam",
            style=discord.ButtonStyle.link,
            url=url,
        )
    )
    return view


def _spotify_authorization_url(
    bot: Fishie,
    user_id: int,
    *,
    channel_id: int | None = None,
    message_id: int | None = None,
) -> str:
    now = int(time.time())
    states = getattr(bot, "_spotify_oauth_states", None)
    if states is None:
        states = bot._spotify_oauth_states = {}
    for token, state_data in list(states.items()):
        if int(state_data.get("expires", 0)) < now:
            states.pop(token, None)
    token = secrets.token_urlsafe(24)
    state_data = {
        "user_id": int(user_id),
        "source": "discord",
        "expires": now + SPOTIFY_STATE_TTL,
    }
    if channel_id is not None and message_id is not None:
        state_data["channel_id"] = int(channel_id)
        state_data["message_id"] = int(message_id)
    states[token] = state_data
    return "https://accounts.spotify.com/authorize?" + urlencode(
        {
            "client_id": bot.config["keys"]["spotify_id"],
            "response_type": "code",
            "redirect_uri": SPOTIFY_CALLBACK_URL,
            "scope": SPOTIFY_SCOPES,
            "state": f"spotify_{token}",
            "show_dialog": "true",
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
        "Authorize Fishie on Steam to connect your account.",
        view=_steam_link_view(url),
        ephemeral=True,
    )


def _spotify_link_view(url: str) -> discord.ui.View:
    view = discord.ui.View(timeout=10 * 60)
    view.add_item(
        discord.ui.Button(
            label="Authorize on Spotify",
            style=discord.ButtonStyle.link,
            url=url,
        )
    )
    return view


async def _send_spotify_link(ctx: Context, interaction: discord.Interaction) -> None:
    message = interaction.message
    can_refresh = bool(message and not message.flags.ephemeral)
    url = _spotify_authorization_url(
        ctx.bot,
        interaction.user.id,
        channel_id=message.channel.id if message and can_refresh else None,
        message_id=message.id if message and can_refresh else None,
    )
    await interaction.response.send_message(
        "Authorize Fishie on Spotify to connect your account.",
        view=_spotify_link_view(url),
        ephemeral=True,
    )


async def _send_spotify_link_dm(ctx: Context) -> None:
    url = _spotify_authorization_url(ctx.bot, ctx.author.id)
    try:
        await ctx.author.send("Use this link to authorize Fishie on Spotify:\n" + url)
    except discord.HTTPException:
        await ctx.send(
            "I couldn't DM you, so use this button to connect your Spotify account.",
            view=_spotify_link_view(url),
            ephemeral=True,
        )
        return
    await ctx.send(
        "I sent you a DM with the link to connect your Spotify account.",
        ephemeral=True,
    )


async def _disconnect_lastfm(bot: Fishie, user_id: int) -> None:
    await bot.pool.execute(
        "UPDATE accounts SET lastfm = NULL, lastfm_session_key = NULL WHERE user_id = $1",
        user_id,
    )
    bot.db_cache.lastfm.pop(user_id, None)


async def _disconnect_steam(bot: Fishie, user_id: int) -> None:
    await bot.pool.execute(
        "UPDATE accounts SET steam = NULL WHERE user_id = $1",
        user_id,
    )


async def _disconnect_spotify(bot: Fishie, user_id: int) -> None:
    await bot.pool.execute(
        "UPDATE accounts SET spotify = NULL, spotify_refresh_token = NULL "
        "WHERE user_id = $1",
        user_id,
    )


def _accounts_embed(ctx: Context, row) -> discord.Embed:
    labels = {
        "lastfm": "Last.fm",
        "steam": "Steam",
        "roblox": "Roblox",
        "letterboxd": "Letterboxd",
        "spotify": "Spotify",
    }
    lines = []
    for col, label in labels.items():
        value = row[col] if row else None
        lines.append(f"{'✅' if value else '❌'} **{label}**")
    return discord.Embed(
        color=ctx.bot.embedcolor,
        title="Connected Accounts",
        description="\n".join(lines),
    )


class Settings(Logging, Server):
    """User and server settings"""

    emoji = discord.PartialEmoji(name="\U00002699\U0000fe0f")

    def __init__(self, bot: Fishie):
        super().__init__()
        self.bot = bot

    @commands.hybrid_command(name="accounts")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def accounts(self, ctx: Context):
        row = await self.bot.pool.fetchrow(
            "SELECT lastfm, steam, roblox, letterboxd, spotify FROM accounts "
            "WHERE user_id = $1",
            ctx.author.id,
        )
        lastfm_connected = bool(row and row["lastfm"])
        steam_connected = bool(row and row["steam"])
        spotify_connected = bool(row and row["spotify"])
        embed = _accounts_embed(ctx, row)
        view = ManageAccountsView(
            ctx,
            lastfm_connected=lastfm_connected,
            steam_connected=steam_connected,
            spotify_connected=spotify_connected,
        )
        await ctx.send(embed=embed, view=view)

    @commands.hybrid_group(name="link", fallback="accounts")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def link(self, ctx: Context):
        await self.accounts(ctx)

    @link.command(name="lastfm")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def link_lastfm(self, ctx: Context):
        connected = bool(
            await self.bot.pool.fetchval(
                "SELECT lastfm FROM accounts WHERE user_id = $1",
                ctx.author.id,
            )
        )
        await ctx.send(
            (
                "Use the button below to disconnect your Last.fm account."
                if connected
                else "Use the button below to connect your Last.fm account."
            ),
            view=LastfmConnectView(ctx, connected=connected),
            ephemeral=True,
        )

    @link.command(name="steam")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def link_steam(self, ctx: Context):
        connected = bool(
            await self.bot.pool.fetchval(
                "SELECT steam FROM accounts WHERE user_id = $1",
                ctx.author.id,
            )
        )
        await ctx.send(
            (
                "Use the button below to disconnect your Steam account."
                if connected
                else "Use the button below to connect your Steam account."
            ),
            view=SteamConnectView(ctx, connected=connected),
            ephemeral=True,
        )

    @link.command(name="spotify")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def link_spotify(self, ctx: Context):
        connected = bool(
            await self.bot.pool.fetchval(
                "SELECT spotify FROM accounts WHERE user_id = $1",
                ctx.author.id,
            )
        )
        if not connected:
            await _send_spotify_link_dm(ctx)
            return
        await ctx.send(
            (
                "Use the button below to disconnect your Spotify account."
                if connected
                else "Use the button below to connect your Spotify account."
            ),
            view=SpotifyConnectView(ctx, connected=connected),
            ephemeral=True,
        )

    @link.command(name="letterboxd")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def link_letterboxd(self, ctx: Context, username: str):
        u = username.strip().lower().rstrip("/")
        if m := LBD_URL_RE.match(u):
            u = m.group(1)
        await self._set_account(ctx.author.id, "letterboxd", u)
        await ctx.send(f"Linked Letterboxd: **{u}**", ephemeral=True)

    @commands.hybrid_group(name="unlink", fallback="accounts")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def unlink(self, ctx: Context):
        await self.accounts(ctx)

    @unlink.command(name="lastfm")
    @lastfm_command()
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def unlink_lastfm(self, ctx: Context):
        await _disconnect_lastfm(self.bot, ctx.author.id)
        await ctx.send("Unlinked last.fm.", ephemeral=True)

    @unlink.command(name="steam")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def unlink_steam(self, ctx: Context):
        await _disconnect_steam(self.bot, ctx.author.id)
        await ctx.send("Unlinked Steam.", ephemeral=True)

    @unlink.command(name="spotify")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def unlink_spotify(self, ctx: Context):
        await _disconnect_spotify(self.bot, ctx.author.id)
        await ctx.send("Unlinked Spotify.", ephemeral=True)

    @unlink.command(name="letterboxd")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def unlink_letterboxd(self, ctx: Context):
        await self._clear_account(ctx.author.id, "letterboxd")
        await ctx.send("Unlinked Letterboxd.", ephemeral=True)

    async def _set_account(self, user_id, col, val):
        await self.bot.pool.execute(
            f'INSERT INTO accounts (user_id, "{col}") VALUES ($1, $2) '
            f'ON CONFLICT (user_id) DO UPDATE SET "{col}" = $2',
            user_id,
            val,
        )

    async def _clear_account(self, user_id, col):
        await self.bot.pool.execute(
            f'UPDATE accounts SET "{col}" = NULL WHERE user_id = $1', user_id
        )


class LastfmConnectView(discord.ui.View):
    def __init__(self, ctx: Context, *, connected: bool):
        super().__init__(timeout=120)
        self.ctx = ctx
        self.connected = connected
        if connected:
            self.connect_lastfm.label = "Disconnect Last.fm"
            self.connect_lastfm.style = discord.ButtonStyle.red

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            "Run the command yourself to connect your Last.fm account.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Connect Last.fm", style=discord.ButtonStyle.green)
    async def connect_lastfm(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ):
        if self.connected:
            await _disconnect_lastfm(self.ctx.bot, interaction.user.id)
            self.connected = False
            self.connect_lastfm.label = "Connect Last.fm"
            self.connect_lastfm.style = discord.ButtonStyle.green
            await interaction.response.edit_message(
                content="Your Last.fm account has been disconnected.", view=self
            )
            return
        await _send_lastfm_link(self.ctx, interaction)


class ManageAccountsView(discord.ui.View):
    def __init__(
        self,
        ctx: Context,
        *,
        lastfm_connected: bool,
        steam_connected: bool = False,
        spotify_connected: bool = False,
    ):
        super().__init__(timeout=120)
        self.ctx = ctx
        self.lastfm_connected = lastfm_connected
        self.steam_connected = steam_connected
        self.spotify_connected = spotify_connected
        if lastfm_connected:
            self.connect_lastfm.label = "Disconnect Last.fm"
            self.connect_lastfm.style = discord.ButtonStyle.red
        if steam_connected:
            self.connect_steam.label = "Disconnect Steam"
            self.connect_steam.style = discord.ButtonStyle.red
        if spotify_connected:
            self.connect_spotify.label = "Disconnect Spotify"
            self.connect_spotify.style = discord.ButtonStyle.red

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            "Run the accounts command yourself to manage your accounts.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Connect Last.fm", style=discord.ButtonStyle.green)
    async def connect_lastfm(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ):
        if self.lastfm_connected:
            await _disconnect_lastfm(self.ctx.bot, interaction.user.id)
            self.lastfm_connected = False
            self.connect_lastfm.label = "Connect Last.fm"
            self.connect_lastfm.style = discord.ButtonStyle.green
            row = await self.ctx.bot.pool.fetchrow(
                "SELECT lastfm, steam, roblox, letterboxd, spotify FROM accounts "
                "WHERE user_id = $1",
                interaction.user.id,
            )
            if interaction.message:
                await interaction.message.edit(
                    embed=_accounts_embed(self.ctx, row), view=self
                )
            await interaction.response.send_message(
                "Your Last.fm account has been disconnected.", ephemeral=True
            )
            return
        await _send_lastfm_link(self.ctx, interaction)

    @discord.ui.button(label="Connect Steam", style=discord.ButtonStyle.green)
    async def connect_steam(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ):
        if self.steam_connected:
            await _disconnect_steam(self.ctx.bot, interaction.user.id)
            self.steam_connected = False
            self.connect_steam.label = "Connect Steam"
            self.connect_steam.style = discord.ButtonStyle.green
            row = await self.ctx.bot.pool.fetchrow(
                "SELECT lastfm, steam, roblox, letterboxd, spotify FROM accounts "
                "WHERE user_id = $1",
                interaction.user.id,
            )
            if interaction.message:
                await interaction.message.edit(
                    embed=_accounts_embed(self.ctx, row), view=self
                )
            await interaction.response.send_message(
                "Your Steam account has been disconnected.", ephemeral=True
            )
            return
        await _send_steam_link(self.ctx, interaction)

    @discord.ui.button(label="Connect Spotify", style=discord.ButtonStyle.green)
    async def connect_spotify(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ):
        if self.spotify_connected:
            await _disconnect_spotify(self.ctx.bot, interaction.user.id)
            self.spotify_connected = False
            self.connect_spotify.label = "Connect Spotify"
            self.connect_spotify.style = discord.ButtonStyle.green
            row = await self.ctx.bot.pool.fetchrow(
                "SELECT lastfm, steam, roblox, letterboxd, spotify FROM accounts "
                "WHERE user_id = $1",
                interaction.user.id,
            )
            if interaction.message:
                await interaction.message.edit(
                    embed=_accounts_embed(self.ctx, row), view=self
                )
            await interaction.response.send_message(
                "Your Spotify account has been disconnected.", ephemeral=True
            )
            return
        await _send_spotify_link(self.ctx, interaction)

    @discord.ui.button(label="Manage Accounts", style=discord.ButtonStyle.blurple)
    async def manage(self, interaction, button):
        row = await self.ctx.bot.pool.fetchrow(
            "SELECT roblox, letterboxd FROM accounts WHERE user_id = $1",
            interaction.user.id,
        )
        current = {col: row[col] for col in ("roblox", "letterboxd")} if row else {}
        await interaction.response.send_modal(ManageAccountsModal(self.ctx, current))


class ManageAccountsModal(discord.ui.Modal, title="Manage Other Accounts"):
    roblox = discord.ui.TextInput(
        label="Roblox", required=False, max_length=100, placeholder="Clear to unlink"
    )
    letterboxd = discord.ui.TextInput(
        label="Letterboxd",
        required=False,
        max_length=100,
        placeholder="Clear to unlink",
    )

    def __init__(self, ctx: Context, current: dict):
        super().__init__()
        self.ctx = ctx
        self._current = current
        self.roblox.default = current.get("roblox", "")
        self.letterboxd.default = current.get("letterboxd", "")

    async def on_submit(self, interaction):
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
                    v = v.lower().rstrip("/")
                    if m := LBD_URL_RE.match(v):
                        v = m.group(1)
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

        # Update the original message embed.
        row = await self.ctx.bot.pool.fetchrow(
            "SELECT lastfm, steam, roblox, letterboxd, spotify FROM accounts "
            "WHERE user_id = $1",
            interaction.user.id,
        )
        embed = _accounts_embed(self.ctx, row)
        if interaction.message:
            await interaction.message.edit(embed=embed)

        await interaction.response.send_message(
            "\n".join(msg) if msg else "No changes made.", ephemeral=True
        )


class SteamConnectView(discord.ui.View):
    def __init__(self, ctx: Context, *, connected: bool):
        super().__init__(timeout=120)
        self.ctx = ctx
        self.connected = connected
        if connected:
            self.connect_steam.label = "Disconnect Steam"
            self.connect_steam.style = discord.ButtonStyle.red

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            "Run the command yourself to connect your Steam account.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Connect Steam", style=discord.ButtonStyle.green)
    async def connect_steam(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ):
        if self.connected:
            await _disconnect_steam(self.ctx.bot, interaction.user.id)
            self.connected = False
            self.connect_steam.label = "Connect Steam"
            self.connect_steam.style = discord.ButtonStyle.green
            await interaction.response.edit_message(
                content="Your Steam account has been disconnected.", view=self
            )
            return
        await _send_steam_link(self.ctx, interaction)


class SpotifyConnectView(discord.ui.View):
    def __init__(self, ctx: Context, *, connected: bool):
        super().__init__(timeout=120)
        self.ctx = ctx
        self.connected = connected
        if connected:
            self.connect_spotify.label = "Disconnect Spotify"
            self.connect_spotify.style = discord.ButtonStyle.red

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            "Run the command yourself to connect your Spotify account.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Connect Spotify", style=discord.ButtonStyle.green)
    async def connect_spotify(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ):
        if self.connected:
            await _disconnect_spotify(self.ctx.bot, interaction.user.id)
            self.connected = False
            self.connect_spotify.label = "Connect Spotify"
            self.connect_spotify.style = discord.ButtonStyle.green
            await interaction.response.edit_message(
                content="Your Spotify account has been disconnected.", view=self
            )
            return
        await _send_spotify_link(self.ctx, interaction)


async def setup(bot: Fishie):
    await bot.add_cog(Settings(bot))
