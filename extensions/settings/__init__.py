from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import discord
from discord.ext import commands
from discord import app_commands

from .logging import Logging
from .server import Server
from utils import lastfm_command
from utils.regexes import LBD_URL_RE, STEAM_URL_RE, STEAM_ID64_RE

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


LASTFM_CALLBACK_URL = "https://crygup.com/fishie"
LASTFM_STATE_TTL = 10 * 60


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
        bot.config["keys"]["lastfm_secret"].encode(),
        encoded.encode(),
        hashlib.sha256,
    ).hexdigest()
    state = f"{encoded}.{signature}"
    callback = f"{LASTFM_CALLBACK_URL}?{urlencode({'lastfm_state': state})}"
    return "https://www.last.fm/api/auth/?" + urlencode(
        {"api_key": bot.config["keys"]["lastfm_cb"], "cb": callback}
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


async def _disconnect_lastfm(bot: Fishie, user_id: int) -> None:
    await bot.pool.execute(
        "UPDATE accounts SET lastfm = NULL WHERE user_id = $1",
        user_id,
    )
    bot.db_cache.lastfm.pop(user_id, None)


def _accounts_embed(ctx: Context, row) -> discord.Embed:
    labels = {
        "lastfm": "Last.fm",
        "steam": "Steam",
        "roblox": "Roblox",
        "genshin": "Genshin",
        "letterboxd": "Letterboxd",
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
            "SELECT lastfm, steam, roblox, genshin, letterboxd FROM accounts "
            "WHERE user_id = $1",
            ctx.author.id,
        )
        connected = bool(row and row["lastfm"])
        embed = _accounts_embed(ctx, row)
        view = ManageAccountsView(ctx, lastfm_connected=connected)
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
    def __init__(self, ctx: Context, *, lastfm_connected: bool):
        super().__init__(timeout=120)
        self.ctx = ctx
        self.lastfm_connected = lastfm_connected
        if lastfm_connected:
            self.connect_lastfm.label = "Disconnect Last.fm"
            self.connect_lastfm.style = discord.ButtonStyle.red

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
                "SELECT lastfm, steam, roblox, genshin, letterboxd FROM accounts "
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

    @discord.ui.button(label="Manage Accounts", style=discord.ButtonStyle.blurple)
    async def manage(self, interaction, button):
        row = await self.ctx.bot.pool.fetchrow(
            "SELECT steam, roblox, genshin, letterboxd FROM accounts WHERE user_id = $1",
            interaction.user.id,
        )
        current = (
            {col: row[col] for col in ("steam", "roblox", "genshin", "letterboxd")}
            if row
            else {}
        )
        await interaction.response.send_modal(ManageAccountsModal(self.ctx, current))


class ManageAccountsModal(discord.ui.Modal, title="Manage Other Accounts"):
    steam = discord.ui.TextInput(
        label="Steam", required=False, max_length=100, placeholder="Clear to unlink"
    )
    roblox = discord.ui.TextInput(
        label="Roblox", required=False, max_length=100, placeholder="Clear to unlink"
    )
    genshin = discord.ui.TextInput(
        label="Genshin UID",
        required=False,
        max_length=100,
        placeholder="Clear to unlink",
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
        self.steam.default = current.get("steam", "")
        self.roblox.default = current.get("roblox", "")
        self.genshin.default = current.get("genshin", "")
        self.letterboxd.default = current.get("letterboxd", "")

    async def on_submit(self, interaction):
        added = []
        removed = []
        COLS = {
            "steam": self.steam,
            "roblox": self.roblox,
            "genshin": self.genshin,
            "letterboxd": self.letterboxd,
        }
        for col, field in COLS.items():
            v = (field.value or "").strip()
            was_set = bool(self._current.get(col))
            if v:
                if col == "steam":
                    self.ctx.bot.logger.info(f"Steam raw input: {v!r}")
                    try:
                        v = await self._resolve_steam(v)
                    except ValueError as e:
                        await interaction.response.send_message(str(e), ephemeral=True)
                        return
                    self.ctx.bot.logger.info(f"Steam resolved: {v!r}")
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
            "SELECT lastfm, steam, roblox, genshin, letterboxd FROM accounts "
            "WHERE user_id = $1",
            interaction.user.id,
        )
        embed = _accounts_embed(self.ctx, row)
        if interaction.message:
            await interaction.message.edit(embed=embed)

        await interaction.response.send_message(
            "\n".join(msg) if msg else "No changes made.", ephemeral=True
        )

    async def _steam_vanity(self, vanity: str) -> str:
        key = self.ctx.bot.config["keys"]["steam"]
        url = f"https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/?key={key}&vanityurl={vanity}"
        async with self.ctx.bot.session.get(url) as resp:
            data = await resp.json()
            if sid := data.get("response", {}).get("steamid"):
                return sid
            raise ValueError(f"No Steam profile found for **{vanity}**.")

    async def _verify_steamid(self, sid: str) -> str:
        key = self.ctx.bot.config["keys"]["steam"]
        url = f"https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/?key={key}&steamids={sid}"
        async with self.ctx.bot.session.get(url) as resp:
            data = await resp.json()
            if players := data.get("response", {}).get("players", []):
                return sid
            raise ValueError(f"SteamID **{sid}** does not exist.")

    async def _resolve_steam(self, raw: str) -> str:
        raw = raw.strip().rstrip("/")
        if m := STEAM_URL_RE.match(raw):
            if sid := m.group(1):
                return await self._verify_steamid(sid)
            if vanity := m.group(2):
                return await self._steam_vanity(vanity)
        if STEAM_ID64_RE.match(raw):
            return await self._verify_steamid(raw)
        if re.match(r"^[a-zA-Z0-9_\-]{2,32}$", raw):
            return await self._steam_vanity(raw)
        raise ValueError("Invalid SteamID64 (must be 17 digits starting with 7656119).")


async def setup(bot: Fishie):
    await bot.add_cog(Settings(bot))
