from __future__ import annotations

import datetime, html, re
from urllib.parse import unquote
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from core import Cog
from utils.converters import SteamConverter, SteamGroupConverter

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


STEAM_ICON = "https://cdn.discordapp.com/emojis/1095791386999132231.png"


class SteamUserView(discord.ui.View):
    message: discord.Message

    def __init__(
        self,
        ctx: Context,
        sid: str,
        profile_embed: discord.Embed,
        av: str,
        display_name: str,
    ):
        super().__init__(timeout=600)
        self.ctx = ctx
        self.sid = sid
        self._av = av
        self._display_name = display_name
        self._embeds: dict[str, discord.Embed] = {"profile": profile_embed}
        self._active = "profile"

    @discord.ui.button(label="Recently Played", style=discord.ButtonStyle.blurple)
    async def recent_btn(self, interaction: discord.Interaction, _):
        if self._active == "profile":
            self.recent_btn.label = "Profile"
            self._active = "recent"
            if "recent" not in self._embeds:
                await interaction.response.defer()
                key = self.ctx.bot.config["keys"]["steam"]
                url = (
                    f"https://api.steampowered.com/IPlayerService/GetRecentlyPlayedGames/v1/"
                    f"?key={key}&steamid={self.sid}"
                )
                async with self.ctx.bot.session.get(url) as resp:
                    data = await resp.json()
                    games = data.get("response", {}).get("games", [])
                if not games:
                    await interaction.followup.send(
                        "No recently played games.", ephemeral=True
                    )
                    return
                self._embeds["recent"] = self._build_recent_embed(games)
                await interaction.edit_original_response(
                    embed=self._embeds["recent"], view=self
                )
            else:
                await interaction.response.edit_message(
                    embed=self._embeds["recent"], view=self
                )
        else:
            self.recent_btn.label = "Recently Played"
            self._active = "profile"
            await interaction.response.edit_message(
                embed=self._embeds["profile"], view=self
            )

    def _build_recent_embed(self, games: list) -> discord.Embed:
        embed = discord.Embed(
            color=self.ctx.bot.embedcolor,
            url=f"https://steamcommunity.com/profiles/{self.sid}/",
        )
        embed.set_author(
            name=f"{self._display_name}'s Recently Played Games",
            icon_url=self._av,
            url=f"https://steamcommunity.com/profiles/{self.sid}/",
        )
        if self._av:
            embed.set_thumbnail(url=self._av)
        desc_parts = []
        for g in games[:10]:
            name = discord.utils.escape_markdown(g.get("name", "Unknown"))
            mins = g.get("playtime_2weeks", 0)
            if mins >= 60:
                time_str = f"{mins // 60}h {mins % 60}m"
            else:
                time_str = f"{mins}m"
            desc_parts.append(f"**{name}** ({time_str})")
        embed.description = "\n".join(desc_parts) or "No games played recently."
        return embed

    async def on_timeout(self):
        self.recent_btn.disabled = True
        if self.message:
            await self.message.edit(view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            "This is not your command.", ephemeral=True
        )
        return False


class Steam(Cog):
    """Steam profile lookup."""

    emoji = discord.PartialEmoji(name="steam", id=1095791386999132231)

    def __init__(self, bot: Fishie):
        super().__init__()
        self.bot = bot

    @commands.hybrid_group(name="steam", fallback="help")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def steam(self, ctx: Context):
        """Steam lookup commands."""
        await ctx.send_help(ctx.command)

    @steam.command(name="user")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def steam_user(
        self,
        ctx: Context,
        *,
        query: str = commands.param(
            default=commands.Author,
            description="SteamID, profile URL, vanity name, or @user.",
        ),
    ):
        """Look up a Steam user."""
        sid = await SteamConverter().convert(ctx, query)
        data = await self._get_player(ctx, sid)
        if not data:
            raise commands.BadArgument(f"No Steam profile found for **{sid}**.")
        friends = await self._get_friends(ctx, sid)
        embed = self._user_embed(sid, data, friends)

        # Only show toggle button if there are recent games.
        recent = await self._get_recent_games(sid)
        if recent:
            av = data.get("avatarfull", "")
            name = data.get("personaname", "Unknown")
            view = SteamUserView(ctx, sid, embed, av, name)
            view.message = await ctx.send(embed=embed, view=view)
        else:
            await ctx.send(embed=embed)

    @steam.command(name="group")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def steam_group(
        self,
        ctx: Context,
        *,
        query: str = commands.param(description="Group URL or custom name."),
    ):
        """Look up a Steam group."""
        name = await SteamGroupConverter().convert(ctx, query)
        data = await self._get_group(ctx, name)
        if not data:
            raise commands.BadArgument(f"No Steam group found for **{name}**.")
        embed = self._group_embed(data)
        await ctx.send(embed=embed)

    async def _get_player(self, ctx: Context, sid: str) -> dict | None:
        key = self.bot.config["keys"]["steam"]
        url = f"https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/?key={key}&steamids={sid}"
        async with self.bot.session.get(url) as resp:
            data = await resp.json()
            players = data.get("response", {}).get("players", [])
            player = players[0] if players else None
            if not player:
                return None
        # Scrape profile page for summary/bio.
        profile_url = f"https://steamcommunity.com/profiles/{sid}/"
        async with self.bot.session.get(profile_url) as page_resp:
            page_html = await page_resp.text()
            m = re.search(
                r'<div class="profile_summary"[^>]*>(.*?)</div>', page_html, re.DOTALL
            )
            if m:
                raw = m.group(1)
                raw = re.sub(r'<img[^>]+alt="([^"]*)"[^>]*>', r"\1", raw)
                # Replace linkfilter <a> tags with placeholders so
                # escape_markdown doesn't break them.
                links: dict[str, tuple[str, str]] = {}
                _counter = 0

                def _unlink(m: re.Match) -> str:
                    nonlocal _counter
                    encoded = m.group(1)
                    text = m.group(2).strip()
                    url = unquote(encoded)
                    key = f"\x00LINK{_counter}\x00"
                    links[key] = (text, url)
                    _counter += 1
                    return key

                raw = re.sub(
                    r'<a class="bb_link" href="[^"]*\?u=([^"&]+)[^"]*"[^>]*>\s*(.*?)\s*</a>\s*<span class="bb_link_host">\[[^\]]*\]</span>',
                    _unlink,
                    raw,
                )

                raw = re.sub(r"<[^>]+>", "", raw)
                raw = html.unescape(raw).strip()
                player["_summary"] = raw
                player["_links"] = links
            # Steam level.
            lvl_m = re.search(
                r'<span class="friendPlayerLevelNum">(\d+)</span>', page_html
            )
            if lvl_m:
                player["_level"] = int(lvl_m.group(1))
            # Group count.
            player["_groups"] = len(re.findall(r'class="groupBlock"', page_html))
        if player.get("communityvisibilitystate") == 3:
            games_url = f"https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/?key={key}&steamid={sid}&include_appinfo=0"
            async with self.bot.session.get(games_url) as games_resp:
                data = await games_resp.json()
                count = data.get("response", {}).get("game_count")
                if count is not None:
                    player["_game_count"] = count
        return player

    async def _get_friends(self, ctx: Context, sid: str) -> int | None:
        key = self.bot.config["keys"]["steam"]
        url = f"https://api.steampowered.com/ISteamUser/GetFriendList/v1/?key={key}&steamid={sid}"
        async with self.bot.session.get(url) as resp:
            data = await resp.json()
            friends = data.get("friendslist", {}).get("friends", [])
            return len(friends) if data.get("friendslist") else None

    async def _get_recent_games(self, sid: str) -> list:
        key = self.bot.config["keys"]["steam"]
        url = (
            f"https://api.steampowered.com/IPlayerService/GetRecentlyPlayedGames/v1/"
            f"?key={key}&steamid={sid}"
        )
        async with self.bot.session.get(url) as resp:
            data = await resp.json()
            return data.get("response", {}).get("games", [])

    def _user_embed(self, sid: str, data: dict, friends: int | None) -> discord.Embed:
        name = data.get("personaname", "Unknown")
        av = data.get("avatarfull", "")
        summary = (
            discord.utils.escape_markdown(data.get("_summary", "")).strip() or None
        )
        if summary and data.get("_links"):
            for key, (text, url) in data["_links"].items():
                summary = summary.replace(key, f"[{text}]({url})")
        status_text = {
            0: "Offline",
            1: "Online",
            2: "Busy",
            3: "Away",
            4: "Snooze",
            5: "Looking to trade",
            6: "Looking to play",
        }
        state = data.get("personastate", 0)
        status_line = status_text.get(state, "Offline")
        loc = data.get("loccountrycode", "")
        flag = ""
        if loc and len(loc) == 2:
            flag = chr(ord(loc[0]) + 0x1F1A5) + chr(ord(loc[1]) + 0x1F1A5) + " "
        embed = discord.Embed(
            color=self.bot.embedcolor,
            url=f"https://steamcommunity.com/profiles/{sid}/",
            description=summary,
        )
        if av:
            embed.set_thumbnail(url=av)
        embed.set_author(
            name=f"{flag}{name} - {status_line}",
            icon_url=av,
            url=f"https://steamcommunity.com/profiles/{sid}/",
        )
        if gc := data.get("_game_count"):
            embed.add_field(name="Games", value=f"{gc:,}", inline=True)
        if friends is not None:
            embed.add_field(name="Friends", value=f"{friends:,}", inline=True)
        if data.get("gameextrainfo"):
            embed.add_field(name="Playing", value=data["gameextrainfo"], inline=True)
        if lo := data.get("lastlogoff"):
            label = "Online since" if state != 0 else "Last Online"
            embed.add_field(name=label, value=f"<t:{lo}:R>", inline=True)
        if lvl := data.get("_level"):
            embed.add_field(name="Level", value=str(lvl), inline=True)
        if groups := data.get("_groups"):
            embed.add_field(name="Groups", value=str(groups), inline=True)
        created = data.get("timecreated")
        if created:
            embed.timestamp = datetime.datetime.fromtimestamp(
                created, tz=datetime.timezone.utc
            )
        embed.set_footer(
            text=f"ID: {sid} | Created at",
            icon_url=STEAM_ICON,
        )
        return embed

    async def _get_group(self, ctx: Context, name: str) -> dict | None:
        xml_url = f"https://steamcommunity.com/groups/{name}/memberslistxml/?xml=1"
        async with self.bot.session.get(xml_url) as resp:
            if resp.status != 200:
                return None
            text = await resp.text()
        m = re.search(r"<groupID64>(\d+)</groupID64>", text)
        if not m:
            return None
        gid = m.group(1)
        name_m = re.search(r"<groupName><!\[CDATA\[(.*?)\]\]></groupName>", text)
        summary_m = re.search(r"<summary><!\[CDATA\[(.*?)\]\]></summary>", text)
        members_m = re.search(r"<memberCount>(\d+)</memberCount>", text)

        avatar = None
        page_url = f"https://steamcommunity.com/groups/{name}/"
        async with self.bot.session.get(page_url) as page_resp:
            if page_resp.status == 200:
                html = await page_resp.text()
                av_m = re.search(r'<link rel="image_src" href="([^"]+)"', html)
                if av_m:
                    avatar = av_m.group(1)

        return {
            "gid": gid,
            "name": name_m.group(1) if name_m else name,
            "summary": summary_m.group(1) if summary_m else "",
            "members": int(members_m.group(1)) if members_m else 0,
            "avatar": avatar,
        }

    def _group_embed(self, data: dict) -> discord.Embed:
        embed = discord.Embed(
            color=self.bot.embedcolor,
            url=f"https://steamcommunity.com/gid/{data['gid']}/",
            description=data.get("summary") or None,
        )
        av = data.get("avatar") or ""
        embed.set_author(
            name=data["name"],
            icon_url=av,
            url=f"https://steamcommunity.com/gid/{data['gid']}/",
        )
        if av:
            embed.set_thumbnail(url=av)
        embed.add_field(name="Members", value=f"{data['members']:,}", inline=True)
        embed.set_footer(text=f"ID: {data['gid']}", icon_url=STEAM_ICON)
        return embed


async def setup(bot: Fishie):
    await bot.add_cog(Steam(bot))
