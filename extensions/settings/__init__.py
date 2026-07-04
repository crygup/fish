from __future__ import annotations

import re
from typing import TYPE_CHECKING

import discord
from discord.ext import commands
from discord import app_commands

from .logging import Logging
from .server import Server
from utils import LASTFM_USERNAME, lastfm_command
from utils.regexes import LBD_URL_RE, STEAM_URL_RE, STEAM_ID64_RE

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


class Settings(Logging, Server):
    """User and server settings"""
    emoji = discord.PartialEmoji(name="\U00002699\U0000fe0f")

    def __init__(self, bot: Fishie):
        super().__init__(); self.bot = bot

    @commands.hybrid_command(name="accounts")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def accounts(self, ctx: Context):
        row = await self.bot.pool.fetchrow(
            "SELECT lastfm, steam, roblox, genshin, letterboxd FROM accounts WHERE user_id = $1",
            ctx.author.id)
        lines = []
        for col in ("lastfm", "steam", "roblox", "genshin", "letterboxd"):
            val = row[col] if row else None
            lines.append(f"{'✅' if val else '❌'} **{col}**")
        embed = discord.Embed(color=ctx.bot.embedcolor, title="Connected Accounts",
                              description="\n".join(lines))
        view = ManageAccountsView(ctx)
        await ctx.send(embed=embed, view=view)

    @commands.hybrid_group(name="link", fallback="accounts")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def link(self, ctx: Context): await self.accounts(ctx)

    @link.command(name="lastfm")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def link_lastfm(self, ctx: Context, username: str):
        if not LASTFM_USERNAME.search(username): raise commands.BadArgument("Invalid last.fm username.")
        await self._set_account(ctx.author.id, "lastfm", username)
        self.bot.db_cache.add_account(ctx.author.id, username)
        await ctx.send(f"Linked last.fm: **{username}**", ephemeral=True)

    @link.command(name="letterboxd")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def link_letterboxd(self, ctx: Context, username: str):
        u = username.strip().lower().rstrip("/")
        if m := LBD_URL_RE.match(u): u = m.group(1)
        await self._set_account(ctx.author.id, "letterboxd", u)
        await ctx.send(f"Linked Letterboxd: **{u}**", ephemeral=True)

    @commands.hybrid_group(name="unlink", fallback="accounts")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def unlink(self, ctx: Context): await self.accounts(ctx)

    @unlink.command(name="lastfm")
    @lastfm_command()
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def unlink_lastfm(self, ctx: Context):
        await self._clear_account(ctx.author.id, "lastfm")
        self.bot.db_cache.remove_account(ctx.author.id)
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
            f'ON CONFLICT (user_id) DO UPDATE SET "{col}" = $2', user_id, val)

    async def _clear_account(self, user_id, col):
        await self.bot.pool.execute(
            f'UPDATE accounts SET "{col}" = NULL WHERE user_id = $1', user_id)


class ManageAccountsView(discord.ui.View):
    def __init__(self, ctx: Context):
        super().__init__(timeout=120); self.ctx = ctx

    @discord.ui.button(label="Manage Accounts", style=discord.ButtonStyle.blurple)
    async def manage(self, interaction, button):
        row = await self.ctx.bot.pool.fetchrow(
            "SELECT lastfm, steam, roblox, genshin, letterboxd FROM accounts WHERE user_id = $1",
            interaction.user.id)
        current = {col: row[col] for col in ("lastfm", "steam", "roblox", "genshin", "letterboxd")} if row else {}
        await interaction.response.send_modal(ManageAccountsModal(self.ctx, current))


class ManageAccountsModal(discord.ui.Modal, title="Manage Accounts"):
    lastfm = discord.ui.TextInput(label="Last.fm", required=False, max_length=100, placeholder="Clear to unlink")
    steam = discord.ui.TextInput(label="Steam", required=False, max_length=100, placeholder="Clear to unlink")
    roblox = discord.ui.TextInput(label="Roblox", required=False, max_length=100, placeholder="Clear to unlink")
    genshin = discord.ui.TextInput(label="Genshin UID", required=False, max_length=100, placeholder="Clear to unlink")
    letterboxd = discord.ui.TextInput(label="Letterboxd", required=False, max_length=100, placeholder="Clear to unlink")

    def __init__(self, ctx: Context, current: dict):
        super().__init__(); self.ctx = ctx; self._current = current
        self.lastfm.default = current.get("lastfm", "")
        self.steam.default = current.get("steam", "")
        self.roblox.default = current.get("roblox", "")
        self.genshin.default = current.get("genshin", "")
        self.letterboxd.default = current.get("letterboxd", "")

    async def on_submit(self, interaction):
        added = []; removed = []
        COLS = {"lastfm": self.lastfm, "steam": self.steam, "roblox": self.roblox,
                "genshin": self.genshin, "letterboxd": self.letterboxd}
        for col, field in COLS.items():
            v = (field.value or "").strip()
            was_set = bool(self._current.get(col))
            if v:
                if col == "steam":
                    self.ctx.bot.logger.info(f"Steam raw input: {v!r}")
                    try: v = await self._resolve_steam(v)
                    except ValueError as e:
                        await interaction.response.send_message(str(e), ephemeral=True); return
                    self.ctx.bot.logger.info(f"Steam resolved: {v!r}")
                if col == "letterboxd":
                    v = v.lower().rstrip("/")
                    if m := LBD_URL_RE.match(v): v = m.group(1)
                if col == "lastfm" and not LASTFM_USERNAME.search(v): continue
                await self.ctx.bot.pool.execute(
                    f'INSERT INTO accounts (user_id, "{col}") VALUES ($1, $2) '
                    f'ON CONFLICT (user_id) DO UPDATE SET "{col}" = $2', interaction.user.id, v)
                if col == "lastfm": self.ctx.bot.db_cache.add_account(interaction.user.id, v)
                added.append(f"{col}: {v}")
            elif was_set:
                await self.ctx.bot.pool.execute(
                    f'UPDATE accounts SET "{col}" = NULL WHERE user_id = $1', interaction.user.id)
                if col == "lastfm": self.ctx.bot.db_cache.remove_account(interaction.user.id)
                removed.append(col)
        msg = []
        if added: msg.append("Linked: " + ", ".join(added))
        if removed: msg.append("Unlinked: " + ", ".join(removed))

        # Update the original message embed.
        row = await self.ctx.bot.pool.fetchrow(
            "SELECT lastfm, steam, roblox, genshin, letterboxd FROM accounts WHERE user_id = $1",
            interaction.user.id)
        lines = []
        for col in ("lastfm", "steam", "roblox", "genshin", "letterboxd"):
            val = row[col] if row else None
            lines.append(f"{'✅' if val else '❌'} **{col}**")
        embed = discord.Embed(color=self.ctx.bot.embedcolor, title="Connected Accounts",
                              description="\n".join(lines))
        if interaction.message:
            await interaction.message.edit(embed=embed)

        await interaction.response.send_message(
            "\n".join(msg) if msg else "No changes made.", ephemeral=True)

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
            if sid := m.group(1): return await self._verify_steamid(sid)
            if vanity := m.group(2): return await self._steam_vanity(vanity)
        if STEAM_ID64_RE.match(raw): return await self._verify_steamid(raw)
        if re.match(r"^[a-zA-Z0-9_\-]{2,32}$", raw):
            return await self._steam_vanity(raw)
        raise ValueError("Invalid SteamID64 (must be 17 digits starting with 7656119).")
async def setup(bot: Fishie):
    await bot.add_cog(Settings(bot))
